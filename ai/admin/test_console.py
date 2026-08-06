"""Тестовая консоль админки: страница + start/status trio.

Зеркало ai/admin/prompt_regression.py: page view рендерит шаблон с initial-снимком
(по ?run_id=), start (POST) запускает прогон, status (GET) отдаёт снимок для
поллинга фронтенда. Доступ — can_access_test_console (staff/superuser ИЛИ
prompt_developer). Тесты выполняются сабпроцессом в изолированном test-settings
окружении — см. ai/test_console_runner.py.
"""

from django.http import HttpResponseForbidden, HttpResponseNotAllowed, JsonResponse
from django.template.response import TemplateResponse

from ..test_console_runner import (
    CLASS_TITLES_RU,
    get_test_run_snapshot,
    start_test_run,
)
from .permissions import can_access_test_console
from .site import ai_admin_site


def admin_test_console_view(request):
    """Страница тестовой консоли (с preloaded-снимком по ?run_id=)."""
    if not can_access_test_console(request):
        return HttpResponseForbidden("Access denied")
    active_run_id = (request.GET.get("run_id") or "").strip()
    active_run_snapshot = None
    error_message = ""
    if active_run_id:
        active_run_snapshot = get_test_run_snapshot(active_run_id)
        if not active_run_snapshot:
            error_message = "Прогон не найден или уже удалён"
    context = {
        **ai_admin_site.each_context(request),
        "title": "Тестовая консоль",
        "active_run_id": active_run_id,
        "active_run_snapshot": active_run_snapshot or {},
        "error_message": error_message,
        "class_count": len(CLASS_TITLES_RU),
        "test_console_start_url": "/ai/admin/test-console/start/",
        "test_console_status_url": "/ai/admin/test-console/status/",
    }
    return TemplateResponse(request, "admin/ai/test_console.html", context)


def admin_test_console_start_view(request):
    """Запуск прогона (POST). Returns {ok, run_id, run} или {ok:false, message}."""
    if not can_access_test_console(request):
        return HttpResponseForbidden("Access denied")
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    run_id, start_error = start_test_run()
    if not run_id:
        return JsonResponse(
            {"ok": False, "message": start_error or "Не удалось запустить прогон"},
            status=400,
        )
    return JsonResponse({"ok": True, "run_id": run_id, "run": get_test_run_snapshot(run_id)})


def admin_test_console_status_view(request):
    """Снимок прогона (GET) для поллинга. Returns {ok, run} | 404."""
    if not can_access_test_console(request):
        return HttpResponseForbidden("Access denied")
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    run_id = (request.GET.get("run_id") or "").strip()
    if not run_id:
        return JsonResponse({"ok": False, "message": "run_id is required"}, status=400)
    run_snapshot = get_test_run_snapshot(run_id)
    if not run_snapshot:
        return JsonResponse(
            {"ok": False, "message": "Прогон не найден или уже удалён"}, status=404,
        )
    return JsonResponse({"ok": True, "run": run_snapshot})