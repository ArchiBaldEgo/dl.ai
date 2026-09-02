"""Глобальная накопленная статистика моделей (среднее время, % решённых).

Источник данных — пакетные прогоны /arm/solve/, запущенные суперюзером с
включённым чекбоксом «заносить результаты в БД» (см. ``arm_runner``).
Semantics агрегации совпадают с ``arm_runner._per_bucket``: в счётчики
решённости идут только пары с вердиктом solved/failed, длительности —
только не-skipped измерения.

Потребитель — ``model_health.get_available_model_options``: обогащает опции
селектора моделей чата полями ``avg_seconds``/``percent_solved``.
"""

from django.db.models import F

from ..models import AIModelStats

# Вердикты, попадающие в счётчик решённости (пропущенные пары не считаются —
# как в ``_per_bucket``).
_VERDICT_SOLVED = "solved"
_VERDICT_FAILED = "failed"


def record_batch_solve_stats(results):
    """Инкрементирует ``AIModelStats`` по результатам завершённого batch-прогона.

    ``results`` — список result-item словарей ``arm_runner._run_batch_job_worker``
    (ключи ``model_key``, ``model_title``, ``verdict``, ``duration``).
    Race-safe upsert: инкремент через ``F``-выражения, при отсутствии строки —
    ``create``. Никогда не читает-модифицирует-пишет (конкурентные прогоны).
    """
    if not results:
        return

    per_model = {}
    for r in results:
        key = r.get("model_key") or ""
        if not key:
            continue
        bucket = per_model.setdefault(key, {"title": r.get("model_title") or key, "solved": 0, "total": 0, "dur_n": 0, "dur_sum": 0.0})
        verdict = r.get("verdict") or ""
        if verdict in (_VERDICT_SOLVED, _VERDICT_FAILED):
            bucket["total"] += 1
            if verdict == _VERDICT_SOLVED:
                bucket["solved"] += 1
        duration = r.get("duration")
        if duration is not None and verdict != "skipped":
            bucket["dur_n"] += 1
            bucket["dur_sum"] += float(duration or 0.0)

    for key, bucket in per_model.items():
        updated = AIModelStats.objects.filter(model_key=key).update(
            model_title=bucket["title"],
            solved_count=F("solved_count") + bucket["solved"],
            total_count=F("total_count") + bucket["total"],
            duration_count=F("duration_count") + bucket["dur_n"],
            duration_sum=F("duration_sum") + bucket["dur_sum"],
        )
        if not updated:
            AIModelStats.objects.create(
                model_key=key,
                model_title=bucket["title"],
                solved_count=bucket["solved"],
                total_count=bucket["total"],
                duration_count=bucket["dur_n"],
                duration_sum=bucket["dur_sum"],
            )


def get_model_stats_map(model_keys):
    """``{model_key: {"avg_seconds": float|None, "percent_solved": float|None}}``.

    Модели без накопленной статистики в словаре отсутствуют.
    """
    keys = [k for k in model_keys if k]
    if not keys:
        return {}
    stats_map = {}
    for row in AIModelStats.objects.filter(model_key__in=keys):
        avg_seconds = None
        if row.duration_count:
            avg_seconds = round(row.duration_sum / row.duration_count, 1)
        percent_solved = None
        if row.total_count:
            percent_solved = round(row.solved_count * 100.0 / row.total_count, 1)
        stats_map[row.model_key] = {
            "avg_seconds": avg_seconds,
            "percent_solved": percent_solved,
        }
    return stats_map