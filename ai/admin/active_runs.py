"""Глобальный бейдж активных прогонов для суперпользователя.

Прогон (batch-solve, find-error, регрессия препромптов, тестовая консоль,
обновление состояния моделей) живёт в фоновом потоке сервера и не
прерывается навигацией по админке. Этот endpoint даёт бейджу в
``base_site.html`` единый лёгкий список всех running-прогонов (без
results/report) — прогресс и ссылка на страницу. Только superuser; путь под
``/ai/admin/`` — RateLimitMiddleware его не считает.
"""

from django.http import HttpResponseForbidden, JsonResponse

from .. import arm_runner, model_health, prompt_test_runner, test_console_runner
from ..models import AIModelTestRun, PromptTestRun


def _annotate_started_by(runs):
    """Одним bulk-запросом на каждую БД-модель добавляет имя запустившего.

    У User нет поля full_name — собираем из first_name/last_name с fallback
    на username (как в services/auth.py). Тест-консоль in-memory, без БД —
    у её прогонов ``started_by`` пустой.
    """
    run_ids = [r["run_id"] for r in runs if r["run_id"]]
    started_by = {}
    for qs in (
        AIModelTestRun.objects.filter(run_id__in=run_ids),
        PromptTestRun.objects.filter(run_id__in=run_ids),
    ):
        for run_id, first, last, username in qs.exclude(user__isnull=True).values_list(
            "run_id", "user__first_name", "user__last_name", "user__username"
        ):
            started_by[run_id] = f"{first} {last}".strip() or username
    for run in runs:
        run["started_by"] = started_by.get(run["run_id"], "")
    return runs


def admin_active_runs_view(request):
    if not request.user.is_authenticated or not request.user.is_superuser:
        return HttpResponseForbidden("Access denied")

    runs = (
        arm_runner.list_running_runs()
        + prompt_test_runner.list_running_runs()
        + test_console_runner.list_running_runs()
        + model_health.list_running_runs()
    )
    return JsonResponse({"ok": True, "runs": _annotate_started_by(runs)})