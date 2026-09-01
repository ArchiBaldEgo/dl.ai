"""Личное меню «мои процессы» в шапке админки.

Прогон (batch-solve, find-error, регрессия препромптов, тестовая консоль)
живёт в фоновом потоке сервера и не прерывается навигацией. Этот endpoint
даёт дропдауну из ``base_site.html`` (ai_processes.js) единый лёгкий список
прогонов **текущего пользователя**: running — всегда, завершённые — только
в течение последних минут (окно уведомления о завершении). Каждый админ
видит только свои прогоны, включая суперпользователя. Единственное
исключение — обновление состояния моделей (model_health): sweep общий и
без владельца (ручной запуск, планировщик 04:00, self-heal), поэтому его
запись глобальная и видна каждому админу. Путь под ``/ai/admin/`` —
RateLimitMiddleware его не считает.
"""

import time

from django.http import HttpResponseForbidden, JsonResponse

from .. import arm_runner, model_health, prompt_test_runner, test_console_runner
from .permissions import can_access_admin

# Сколько секунд после завершения прогон виден в меню (окно уведомления).
_RECENT_FINISHED_TTL_SECONDS = 600


def admin_active_runs_view(request):
    if not request.user.is_authenticated or not can_access_admin(request.user):
        return HttpResponseForbidden("Access denied")

    user_id = request.user.id
    cutoff = time.time() - _RECENT_FINISHED_TTL_SECONDS
    runs = (
        arm_runner.list_user_runs(user_id, cutoff)
        + prompt_test_runner.list_user_runs(user_id, cutoff)
        + test_console_runner.list_user_runs(user_id, cutoff)
        + model_health.list_model_refresh_runs(cutoff)
    )
    # Активные — первыми, дальше — от свежих к старым.
    runs.sort(key=lambda r: (r.get("status") != "running", -(r.get("created_at_ts") or 0.0)))
    return JsonResponse({"ok": True, "runs": runs})