"""XLSX-экспорт результатов batch-прогона ARM (матрица задача×модель).

Единый серверный генератор для журнала запросов (запись batch-лога) и для
страницы /arm/solve/ (живой/завершённый прогон по run_id) — раньше формат
существовал в двух зеркалах (серверный CSV в logs.py и клиентский
downloadResultsCsv в _ai_batch_results.html). Ширина каждой колонки
подбирается по максимальной длине содержимого (заголовок учитывается) —
столбцы «растягиваются» сами, вручную ничего подгонять не нужно.
"""

from io import BytesIO

from openpyxl import Workbook
from openpyxl.utils import get_column_letter

# Границы автоширины (в символах Excel): короткие колонки не схлопываются,
# длинные названия задач/моделей не растягивают лист без предела.
_MIN_COL_WIDTH = 8
_MAX_COL_WIDTH = 60

XLSX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)


def _verdict_mark(verdict):
    """'+' solved / '-' failed / '?' прочее — прежняя CSV-нотация."""
    if verdict == "solved":
        return "+"
    if verdict == "failed":
        return "-"
    return "?"


def _arm_results_matrix(results):
    """Матрица задача×модель: (header, rows).

    header = ["Node ID", "Задача", <названия моделей>]; в ячейках '+/-/?'.
    Порядок задач/моделей — как в списке результатов (первое появление).
    """
    task_map, task_order = {}, []
    model_map, model_order = {}, []
    matrix = {}
    for r in results:
        tkey = str(r.get("task_node_id") or "")
        mkey = str(r.get("model_key") or r.get("model_title") or "")
        if tkey not in task_map:
            task_map[tkey] = r.get("task_name") or ""
            task_order.append(tkey)
        if mkey not in model_map:
            model_map[mkey] = r.get("model_title") or mkey
            model_order.append(mkey)
        matrix.setdefault(tkey, {})[mkey] = _verdict_mark(r.get("verdict"))

    header = ["Node ID", "Задача"] + [model_map[m] for m in model_order]
    rows = []
    for tkey in task_order:
        row = [tkey, task_map[tkey]]
        row.extend(matrix.get(tkey, {}).get(m, "") for m in model_order)
        rows.append(row)
    return header, rows


def build_arm_results_xlsx(results) -> bytes:
    """Собрать .xlsx с автошириной колонок из списка результатов прогона."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Результаты"

    header, rows = _arm_results_matrix(results)
    all_rows = [header] + rows
    for row in all_rows:
        ws.append(row)

    # Автоширина: максимум по длине значений колонки (заголовок входит),
    # с разумными границами.
    for col_idx in range(1, len(header) + 1):
        width = max((len(str(row[col_idx - 1])) for row in all_rows), default=0)
        ws.column_dimensions[get_column_letter(col_idx)].width = min(
            max(width + 2, _MIN_COL_WIDTH), _MAX_COL_WIDTH,
        )

    bio = BytesIO()
    wb.save(bio)
    return bio.getvalue()