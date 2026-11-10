"""Имитация записей о решённых задачах для локального тестирования.

Записи помечаются префиксом ``__sim__`` в ``run_id`` (и ``model_key``), их
удаляет ``delete_simulated()``. Только для DEBUG / ALLOW_DB_SIMULATION.
"""
from __future__ import annotations

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from ai.models import AIModelTestResult, AIModelTestRun, Task

SIM_PREFIX = "__sim__"
SIM_MODEL_KEY = f"{SIM_PREFIX}model"
SIM_MODEL_TITLE = "Simulated model"


class SimulationForbidden(RuntimeError):
    """Попытка запуска имитации в проде."""


def _ensure_local() -> None:
    if not settings.DEBUG and not getattr(settings, "ALLOW_DB_SIMULATION", False):
        raise SimulationForbidden(
            "Имитация БД разрешена только при DEBUG=True "
            "или ALLOW_DB_SIMULATION=True."
        )


@transaction.atomic
def imitate_solved(
    course_id: int,
    node_ids: list[int],
    *,
    verdict: str = AIModelTestResult.VERDICT_SOLVED,
    model_key: str = SIM_MODEL_KEY,
    model_title: str = SIM_MODEL_TITLE,
    run_name: str = "Simulated run",
    created_by=None,
) -> tuple[AIModelTestRun | None, int]:
    """Создать/обновить batch-прогон и строки results с заданным verdict.

    Для API ``get_task_status``:
      * verdict=VERDICT_SOLVED  → статус 1 (решено);
      * verdict=VERDICT_FAILED / VERDICT_SKIPPED → статус 2 (посылки были, solved нет);
      * ни одного вызова с данным node_id      → статус 0.

    Returns: (run, сколько строк создано в этом вызове).
    """
    _ensure_local()
    if not node_ids:
        return None, 0

    run, _ = AIModelTestRun.objects.get_or_create(
        run_id=f"{SIM_PREFIX}course_{course_id}",
        defaults={
            "run_type": AIModelTestRun.RUN_TYPE_BATCH,
            "status": AIModelTestRun.STATUS_COMPLETED,
            "course_id": course_id,
            "run_name": run_name,
            "user": created_by if getattr(created_by, "is_authenticated", False) else None,
            "finished_at": timezone.now(),
        },
    )

    created = 0
    for node_id in node_ids:
        # node_id уникален → гонки нет; имя помечаем, только если создаём.
        task, task_created = Task.objects.get_or_create(
            node_id=node_id,
            defaults={"name": f"[sim] node {node_id}"},
        )
        if task_created:
            task.file_extension = ".py"  # для отображения; не обязательно

        failure_reason = (
            "" if verdict == AIModelTestResult.VERDICT_SOLVED
            else AIModelTestResult.FR_UNDEFINED
        )
        _, made = AIModelTestResult.objects.update_or_create(
            run=run,
            model_key=model_key,
            task=task,
            defaults={
                "model_title": model_title,
                "status": AIModelTestResult.STATUS_OK,
                "verdict": verdict,
                "failure_reason": failure_reason,
                "updated_at": timezone.now(),
            },
        )
        created += int(made)
    return run, created


def delete_imitated(course_id: int | None = None) -> int:
    """Удалить все имитационные прогоны (results уедут каскадом).

    Возвращает количество удалённых объектов (runs + results).
    """
    _ensure_local()
    qs = AIModelTestRun.objects.filter(run_id__startswith=SIM_PREFIX)
    if course_id is not None:
        qs = qs.filter(course_id=course_id)
    _, per_model = qs.delete()
    return sum(per_model.values())