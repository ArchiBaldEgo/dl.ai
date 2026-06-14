"""Model health status admin views."""

from django.contrib import admin
from django.http import HttpResponseForbidden, HttpResponseNotAllowed, JsonResponse
from django.template.response import TemplateResponse
from django.utils import timezone

from ..constants import MOSCOW_TZ
from ..model_health import (
    get_available_model_options,
    get_health_window_date,
    get_model_status_rows,
    is_model_health_refresh_running,
    trigger_model_health_refresh_async,
)
from .permissions import can_access_arm, can_access_model_status


def _serialize_model_status_rows_for_api(rows):
    serialized = []
    for row in rows:
        checked_at = row.get("checked_at")
        checked_at_msk = ""
        if checked_at:
            checked_at_msk = timezone.localtime(checked_at, MOSCOW_TZ).strftime("%d.%m.%Y %H:%M:%S")
        window_date = row.get("window_date")
        serialized.append({
            "key": row.get("key") or "",
            "title": row.get("title") or "",
            "is_active": bool(row.get("is_active")),
            "status_label": row.get("status_label") or "",
            "window_date": window_date.isoformat() if window_date else "",
            "checked_at_msk": checked_at_msk,
            "is_current_window": bool(row.get("is_current_window")),
        })
    return serialized


def admin_model_status_view(request):
    if not can_access_model_status(request):
        return HttpResponseForbidden("Access denied")

    refresh_message = ""
    refresh_error = ""

    if request.method == "POST" and request.POST.get("action") == "refresh_models":
        try:
            if trigger_model_health_refresh_async():
                refresh_message = "Обновление моделей запущено в фоне. Окно 04:00 МСК не изменяется."
            else:
                refresh_message = "Обновление уже выполняется. Дождитесь завершения."
        except Exception as exc:
            refresh_error = f"Не удалось запустить обновление моделей: {exc}"

    context = {
        **admin.site.each_context(request),
        "title": "AI: Состояние моделей",
        "health_window_date": get_health_window_date().strftime("%d.%m.%Y"),
        "model_status_rows": get_model_status_rows(),
        "refresh_message": refresh_message,
        "refresh_error": refresh_error,
        "refresh_in_progress": is_model_health_refresh_running(),
        "arm_find_error_url": "/ai/admin/arm/find-error/",
        "arm_model_status_refresh_url": "/ai/admin/arm/models/refresh/",
        "arm_model_status_state_url": "/ai/admin/arm/models/state/",
    }
    return TemplateResponse(request, "admin/ai/model_status.html", context)


def admin_model_status_state_view(request):
    if not can_access_model_status(request):
        return HttpResponseForbidden("Access denied")

    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])

    rows = get_model_status_rows()
    return JsonResponse({
        "ok": True,
        "health_window_date": get_health_window_date().strftime("%d.%m.%Y"),
        "refresh_in_progress": is_model_health_refresh_running(),
        "model_status_rows": _serialize_model_status_rows_for_api(rows),
    })


def admin_model_status_refresh_view(request):
    if not can_access_arm(request):
        return HttpResponseForbidden("Access denied")

    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    try:
        started = trigger_model_health_refresh_async()
    except Exception as exc:
        return JsonResponse(
            {"ok": False, "message": f"Не удалось запустить обновление моделей: {exc}"},
            status=500,
        )

    if started:
        message = "Обновление моделей запущено в фоне. Окно 04:00 МСК не изменяется."
    else:
        message = "Обновление уже выполняется. Дождитесь завершения."

    return JsonResponse({
        "ok": True,
        "message": message,
        "refresh_in_progress": is_model_health_refresh_running(),
    })
