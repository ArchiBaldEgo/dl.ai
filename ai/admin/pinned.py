"""Закреплённые завершённые пакетные прогоны (персональные закладки журнала).

По ТЗ: ссылка в навигации под «Настройкой ИИ-приложения» — страница
«Закреплённые пакетные решения», где хранятся закреплённые завершённые
пакетные прогоны. Закрепление (★) выполняется из журнала запросов и из
блока «Последние пакетные решения» на «Настройке ИИ-приложения».

Права — как у журнала (can_access_logs + скоуп «только свои» для
prompt_developer): закрепить можно только доступную самому пользователю
запись журнала, поэтому чужие прогоны в закладки не попадают.

Страница «Закреплённые...» при этом ОБЩАЯ: у всех с доступом один список
закреплений всех участников (с авторами); открепить можно только своё.
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
from .permissions import can_access_logs, logs_scope_is_own_user
from .site import ai_admin_site

PINNED_RUNS_URL = "/ai/admin/pinned-runs/"


def admin_pinned_runs_view(request):
    """Страница «Закреплённые пакетные решения» (навигация под «Настройкой»).

    Общий вид: у каждого, кто имеет доступ к странице (стафф/суперюзеры/
    разработчики промтов — тот же guard, что у журнала), список закреплений
    ОДИН на всех — видны закрепления всех участников с авторами. Открепить
    (★) можно только своё закрепление; чужое показывается меткой «★ автор».
    """
    if not can_access_logs(request):
        return HttpResponseForbidden("Access denied")

    pins = list(
        AIPinnedBatchRun.objects.select_related("log", "user")
        .order_by("-created_at", "-id")
    )
    logs = [pin.log for pin in pins]
    rows = build_batch_rows_for_logs(request, logs)
    # Порядок страницы — по дате закрепления (AIPinnedBatchRun.ordering),
    # а не по sent_at строк.
    rows_by_log_id = {row["id"]: row for row in rows}

    # Один и тот же лог могут закрепить несколько человек — показываем один раз
    # со списком авторов. Открепление — только своё; раскрывать чужие результаты
    # разработчик промтов не может (скуп журнала) — такому ряду не отдаём
    # snapshot_url, он не разворачивается кликом.
    scope_own = logs_scope_is_own_user(request.user)
    ordered_rows = []
    for pin in pins:
        row = rows_by_log_id.pop(pin.log_id, None)
        if row is None:
            continue
        row_pins = [p for p in pins if p.log_id == pin.log_id]
        row["is_own"] = any(p.user_id == request.user.pk for p in row_pins)
        row["pin_authors"] = ", ".join(_pin_author_display(p.user) for p in row_pins)
        if scope_own and pin.log.user_id != request.user.pk:
            row["snapshot_url"] = ""
        ordered_rows.append(row)

    context = {
        **ai_admin_site.each_context(request),
        "title": "Закреплённые пакетные решения",
        "rows": ordered_rows,
        "moscow_tz": MOSCOW_TZ,
    }
    return TemplateResponse(request, "admin/ai/pinned_runs.html", context)


def _pin_author_display(user):
    """Читаемое имя автора закрепления (ФИО или логин)."""
    name = user.get_full_name() if hasattr(user, "get_full_name") else ""
    return name.strip() or user.username


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