"""Тестовая консоль админки: страница + start/status trio + история логов.

Зеркало ai/admin/prompt_regression.py: page view рендерит шаблон с initial-снимком
(по ?run_id=; без него — последний прогон пользователя не старше 10 минут),
start (POST) запускает прогон, status (GET) отдаёт снимок для поллинга
фронтенда. Полный raw-вывод каждого прогона лежит на диске (последние 10):
logs/ — список (logs/), просмотр/скачивание (logs/view/). Доступ —
can_access_test_console (только superuser). Тесты выполняются сабпроцессом
в изолированном test-settings окружении — см. ai/test_console_runner.py.
"""

from django.http import (
    HttpResponse,
    HttpResponseForbidden,
    HttpResponseNotAllowed,
    JsonResponse,
)
from django.template.response import TemplateResponse

from ..test_console_runner import (
    CLASS_TITLES_RU,
    get_latest_run_snapshot,
    get_test_run_snapshot,
    list_disk_logs,
    read_disk_log,
    start_test_run,
)
from .permissions import can_access_test_console
from .site import ai_admin_site


def admin_test_console_view(request):
    """Страница тестовой консоли (с preloaded-снимком).

    С ?run_id= — указанный прогон. Без — последний прогон текущего
    пользователя, если он завершился менее 10 минут назад (или ещё
    выполняется): возврат на страницу сразу после прогона показывает итог
    без необходимости сохранять ссылку.
    """
    if not can_access_test_console(request):
        return HttpResponseForbidden("Access denied")
    active_run_id = (request.GET.get("run_id") or "").strip()
    active_run_snapshot = None
    error_message = ""
    if active_run_id:
        active_run_snapshot = get_test_run_snapshot(active_run_id)
        if not active_run_snapshot:
            error_message = "Прогон не найден или уже удалён"
    else:
        active_run_snapshot = get_latest_run_snapshot(request.user.id)
        if active_run_snapshot:
            active_run_id = active_run_snapshot.get("run_id", "")
    context = {
        **ai_admin_site.each_context(request),
        "title": "Тестовая консоль",
        "active_run_id": active_run_id,
        "active_run_snapshot": active_run_snapshot or {},
        "error_message": error_message,
        "class_count": len(CLASS_TITLES_RU),
        "test_console_start_url": "/ai/admin/test-console/start/",
        "test_console_status_url": "/ai/admin/test-console/status/",
        "test_console_logs_url": "/ai/admin/test-console/logs/",
        "test_console_log_view_url": "/ai/admin/test-console/logs/view/",
    }
    return TemplateResponse(request, "admin/ai/test_console.html", context)


def admin_test_console_start_view(request):
    """Запуск прогона (POST). Returns {ok, run_id, run} или {ok:false, message}."""
    if not can_access_test_console(request):
        return HttpResponseForbidden("Access denied")
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    run_id, start_error = start_test_run(request.user.id)
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


def admin_test_console_logs_view(request):
    """Список дисковых логов прогонов (GET) для «Истории прогонов».

    Returns {ok, logs: [{filename, run_id, started_at, status, summary, size_bytes}]}.
    """
    if not can_access_test_console(request):
        return HttpResponseForbidden("Access denied")
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    return JsonResponse({"ok": True, "logs": list_disk_logs()})


def admin_test_console_log_view(request):
    """Просмотр/скачивание дискового лога прогона (GET ?filename=...).

    По умолчанию — JSON для модалки; ?download=1 — text/plain с
    Content-Disposition: attachment (как у result download в arm.py).
    Имя валидируется строгим regex в read_disk_log (path-traversal барьер):
    неподходящее имя — 404 без обращения к диску.
    """
    if not can_access_test_console(request):
        return HttpResponseForbidden("Access denied")
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    filename = (request.GET.get("filename") or "").strip()
    content = read_disk_log(filename)
    if content is None:
        return JsonResponse({"ok": False, "message": "Лог не найден"}, status=404)
    if request.GET.get("download") == "1":
        response = HttpResponse(content, content_type="text/plain; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response
    return JsonResponse({"ok": True, "filename": filename, "content": content})