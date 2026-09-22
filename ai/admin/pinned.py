"""Закреплённые завершённые пакетные прогоны (персональные закладки журнала).

По ТЗ: ссылка в навигации под «Настройкой ИИ-приложения» — страница
«Закреплённые пакетные решения», где хранятся закреплённые завершённые
пакетные прогоны. Закрепление (★) выполняется из журнала запросов и из
блока «Последние пакетные решения» на «Настройке ИИ-приложения».

Права — как у журнала (can_access_logs + скоуп «только свои» для
prompt_developer): закрепить можно только доступную самому пользователю
запись журнала, поэтому чужие прогоны в закладки не попадают.
"""

from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.template.response import TemplateResponse

from ..models import AIModelTestRun, AIPinnedBatchRun
from .logs import (
    MOSCOW_TZ,
    _batch_run_id_from_log,
    _get_scoped_log,
    _is_batch_solve_log,
    build_batch_rows_for_logs,
)
from .permissions import can_access_logs
from .site import ai_admin_site

PINNED_RUNS_URL = "/ai/admin/pinned-runs/"


def admin_pinned_runs_view(request):
    """Страница «Закреплённые пакетные решения» (навигация под «Настройкой»)."""
    if not can_access_logs(request):
        return HttpResponseForbidden("Access denied")

    pins = (
        AIPinnedBatchRun.objects.filter(user=request.user)
        .select_related("log")
        .order_by("-created_at", "-id")
    )
    logs = [pin.log for pin in pins]
    rows = build_batch_rows_for_logs(request, logs)
    # Порядок страницы — по дате закрепления (AIPinnedBatchRun.ordering),
    # а не по sent_at строк.
    rows_by_log_id = {row["id"]: row for row in rows}
    ordered_rows = [rows_by_log_id[pin.log_id] for pin in pins if pin.log_id in rows_by_log_id]

    context = {
        **ai_admin_site.each_context(request),
        "title": "Закреплённые пакетные решения",
        "rows": ordered_rows,
        "moscow_tz": MOSCOW_TZ,
    }
    return TemplateResponse(request, "admin/ai/pinned_runs.html", context)


def admin_pinned_run_toggle_view(request, log_id):
    """Закрепить / открепить batch-прогон журнала (★, AJAX POST).

    Закреплять можно только: доступную запись журнала, пакетный прогон
    (source=arm), у которого прогон есть в БД и уже завершён (running
    закреплять нельзя — «закреплённые ЗАВЕРШЁННЫЕ пакетные решения»).
    Повторный вызов снимает закрепление.
    """
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "Метод не поддерживается"}, status=405)
    if not can_access_logs(request):
        return HttpResponseForbidden("Access denied")

    log, error = _get_scoped_log(request, log_id)
    if log is None:
        return error

    if not _is_batch_solve_log(log):
        return JsonResponse(
            {"ok": False, "message": "Закреплять можно только пакетные прогоны"},
            status=400,
        )

    existing = AIPinnedBatchRun.objects.filter(user=request.user, log=log).first()
    if existing:
        existing.delete()
        return JsonResponse({"ok": True, "pinned": False})

    run_hex = _batch_run_id_from_log(log)
    run = AIModelTestRun.objects.filter(run_id=run_hex).first() if run_hex else None
    if run is None:
        return JsonResponse(
            {"ok": False, "message": "Прогон стёрт из БД — закреплять нечего"},
            status=400,
        )
    if run.status == AIModelTestRun.STATUS_RUNNING:
        return JsonResponse(
            {"ok": False, "message": "Прогон ещё выполняется — закреплять можно только завершённые"},
            status=400,
        )
    AIPinnedBatchRun.objects.create(user=request.user, log=log)
    return JsonResponse({"ok": True, "pinned": True})