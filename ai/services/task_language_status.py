"""Флаги решённости задач DL по языку для дерева ARM.

Единственный владелец логики:
  * чтение флагов из локальной БД (дерево никогда не ходит в DL при отрисовке);
  * прогон обновления флагов по DL ``POST /restapi/get-solutions``.

Семантика статусов — см. ``ai.models.TaskLanguageStatus``:
0 (нет посылок), 1 (есть верное решение), 2 (посылки были, верных нет).

``/get-solutions`` фильтруется по расширению без точки; соответствие
«язык → расширение» берётся из ``ai/services/task_registry.py`` (DRY), никаких
собственных копий маппинга здесь нет.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from django.db import transaction
from django.utils import timezone

from ..dl_api_client import (
    DLApiError,
    DLApiUnavailable,
    DLForbiddenError,
    DLUnauthorizedError,
    ensure_course_session,
    get_solutions_from_dl,
)
from ..models import ProgrammingLanguage, TaskLanguageStatus
from .task_registry import language_extension

logger = logging.getLogger(__name__)

# Сколько узлов обновляем за один HTTP-запрос страницы ARM. Клиент режет
# дерево на пачки, чтобы показывать прогресс и не держать один долгий запрос.
REFRESH_MAX_NODES = 100
# Параллельные запросы к DL: только HTTP в потоках, запись в БД — в основном.
# 10 = ровно размер клиентской пачки: вся пачка уходит ОДНИМ кругом, поэтому
# время ответа не умножается на число кругов (см. ниже про 60-секундный
# таймаут внешнего прокси).
REFRESH_WORKERS = 10
# Таймаут одного запроса /get-solutions: зависший DL не должен растягивать пакет.
# Держим весь HTTP-запрос страницы ARM заведомо короче 60 с: внешний nginx на
# dl.gsu.by не задаёт proxy_read_timeout (дефолт 60 с) и на более долгий запрос
# отвечает HTML 504, который клиент покажет как ошибку, а не статусы.
REFRESH_TIMEOUT = 10

_FATAL_ERRORS = (DLUnauthorizedError, DLForbiddenError, DLApiUnavailable)


class TaskStatusRefreshError(RuntimeError):
    """Прогон обновления флагов прерван по фатальной ошибке DL."""


def extension_for_language(programming_language_id: int) -> str:
    """Расширение DL (без точки) для локального языка; "" если язык не найден."""
    language = (
        ProgrammingLanguage.objects
        .filter(pk=programming_language_id)
        .only("language_name")
        .first()
    )
    if language is None:
        return ""
    return language_extension(language.language_name).lstrip(".")


def statuses_for_nodes(
    *, course_id: int, programming_language_id: int, node_ids: list[int],
) -> dict[int, int]:
    """Статусы узлов из БД; отсутствующая строка = «не решалась» (0)."""
    if not node_ids:
        return {}
    rows = (
        TaskLanguageStatus.objects
        .filter(
            course_id=course_id,
            programming_language_id=programming_language_id,
            node_id__in=node_ids,
        )
        .values_list("node_id", "status")
    )
    by_node = {int(node_id): int(status) for node_id, status in rows}
    return {
        int(node_id): by_node.get(int(node_id), TaskLanguageStatus.STATUS_NOT_ATTEMPTED)
        for node_id in node_ids
    }


def latest_refreshed_at(
    *, course_id: int, programming_language_id: int, node_ids: list[int],
):
    """Максимальный ``refreshed_at`` по узлам (None — флаги ещё не заполнялись)."""
    if not node_ids:
        return None
    return (
        TaskLanguageStatus.objects
        .filter(
            course_id=course_id,
            programming_language_id=programming_language_id,
            node_id__in=node_ids,
        )
        .order_by("-refreshed_at")
        .values_list("refreshed_at", flat=True)
        .first()
    )


def _is_correct(solution: dict) -> bool:
    """DL может отдать bool или строку — приводим к bool без ложных срабатываний."""
    value = solution.get("isCorrect")
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


def _classify(solutions: list[dict]) -> tuple[int, int, int]:
    """(status, solutions_total, solutions_correct) по списку посылок DL."""
    total = len(solutions)
    correct = sum(1 for item in solutions if _is_correct(item))
    if correct:
        status = TaskLanguageStatus.STATUS_SOLVED
    elif total:
        status = TaskLanguageStatus.STATUS_ATTEMPTED
    else:
        status = TaskLanguageStatus.STATUS_NOT_ATTEMPTED
    return status, total, correct


def _fetch_status(session_id: str, course_id: int, node_id: int, extension: str):
    """Один запрос к DL → (status, total, correct). Пробрасывает DLApiError."""
    response = get_solutions_from_dl(
        session_id,
        course_id,
        node_id,
        extension=extension,
        timeout=REFRESH_TIMEOUT,
    )
    solutions = response.get("solutions") if isinstance(response, dict) else None
    return _classify(solutions or [])


def _probe_first_node(session_id: str, course_id: int, node_id: int, extension: str):
    """Первый узел отдельно: 403 чинится активацией курса, 401/сеть — фатальны.

    ``ensure_course_session`` — тот же приём, что перед send-solution: DL мог
    не «войти» в курс в этой DLSID-сессии. После активации повторяем один раз.
    """
    try:
        return _fetch_status(session_id, course_id, node_id, extension)
    except DLForbiddenError:
        ensure_course_session(session_id, course_id, node_id)
        return _fetch_status(session_id, course_id, node_id, extension)


def refresh_statuses(
    *,
    session_id: str,
    course_id: int,
    programming_language_id: int,
    node_ids: list[int],
) -> dict:
    """Обновить флаги для узлов по DL ``/get-solutions``.

    Возвращает::

        {
          "statuses": {node_id: 0|1|2},
          "summary": {"solved": N, "attempted": N, "none": N},
          "errors": {"<node_id>": "текст"},
          "extension": "mpc",
          "refreshed_at": datetime | None,
        }

    Фатальные ошибки DL (401/403 после активации курса, недоступность) поднимают
    ``TaskStatusRefreshError`` — частично записанные флаги при этом остаются
    (они корректны для уже обработанных узлов). Ошибки конкретных задач
    (404, 5xx) попадают в ``errors`` и не затирают ранее сохранённый флаг.
    """
    extension = extension_for_language(programming_language_id)
    if not extension:
        raise ValueError("Для выбранного языка не найдено расширение файла DL.")

    unique_nodes = []
    seen = set()
    for node_id in node_ids:
        node_id = int(node_id)
        if node_id not in seen:
            seen.add(node_id)
            unique_nodes.append(node_id)
    if not unique_nodes:
        return {
            "statuses": {},
            "summary": {"solved": 0, "attempted": 0, "none": 0},
            "errors": {},
            "extension": extension,
            "refreshed_at": None,
        }

    results: dict[int, tuple[int, int, int]] = {}
    errors: dict[str, str] = {}

    # Первый узел — проба на фатальные ошибки (и починка «курс не активирован»).
    first_node = unique_nodes[0]
    try:
        results[first_node] = _probe_first_node(
            session_id, course_id, first_node, extension,
        )
    except _FATAL_ERRORS as exc:
        raise TaskStatusRefreshError(f"Ошибка DL API: {exc}") from exc
    except DLApiError as exc:
        # Не фатально (например, 404 у конкретной задачи) — идём дальше.
        errors[str(first_node)] = str(exc)

    rest = unique_nodes[1:]
    if rest:
        executor = ThreadPoolExecutor(max_workers=REFRESH_WORKERS)
        try:
            futures = {
                executor.submit(
                    _fetch_status, session_id, course_id, node_id, extension,
                ): node_id
                for node_id in rest
            }
            for future in as_completed(futures):
                node_id = futures[future]
                try:
                    results[node_id] = future.result()
                except _FATAL_ERRORS as exc:
                    executor.shutdown(wait=False, cancel_futures=True)
                    raise TaskStatusRefreshError(f"Ошибка DL API: {exc}") from exc
                except DLApiError as exc:
                    errors[str(node_id)] = str(exc)
                except Exception as exc:  # noqa: BLE001 — один узел не роняет пакет
                    logger.warning(
                        "Task status refresh failed for node %s: %s", node_id, exc,
                    )
                    errors[str(node_id)] = str(exc)
        finally:
            executor.shutdown(wait=True)

    refreshed_at = timezone.now()
    with transaction.atomic():
        for node_id, (status, total, correct) in results.items():
            TaskLanguageStatus.objects.update_or_create(
                course_id=course_id,
                node_id=node_id,
                programming_language_id=programming_language_id,
                defaults={
                    "file_extension": f".{extension}",
                    "status": status,
                    "solutions_total": total,
                    "solutions_correct": correct,
                    "refreshed_at": refreshed_at,
                },
            )

    summary = {"solved": 0, "attempted": 0, "none": 0}
    for status, _total, _correct in results.values():
        if status == TaskLanguageStatus.STATUS_SOLVED:
            summary["solved"] += 1
        elif status == TaskLanguageStatus.STATUS_ATTEMPTED:
            summary["attempted"] += 1
        else:
            summary["none"] += 1

    statuses = statuses_for_nodes(
        course_id=course_id,
        programming_language_id=programming_language_id,
        node_ids=unique_nodes,
    )
    return {
        "statuses": statuses,
        "summary": summary,
        "errors": errors,
        "extension": extension,
        "refreshed_at": latest_refreshed_at(
            course_id=course_id,
            programming_language_id=programming_language_id,
            node_ids=unique_nodes,
        ),
    }
