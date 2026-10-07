"""Логи запросов к AI-моделям: admin view и кастомная страница списка."""

import logging
import re
from datetime import datetime, timezone as dt_timezone

from django.contrib import admin
from django.core.paginator import Paginator
from django.db.models import Count, Q
from .site import ai_admin_site
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.template.response import TemplateResponse
from django.utils import timezone
from django.utils.html import strip_tags
from django.utils.http import urlencode
from django.views.decorators.http import require_POST

from ..constants import DL_DEFAULT_COURSE_ID, MOSCOW_TZ
from ..dl_api_client import (
    DLApiError,
    DLApiUnavailable,
    DLForbiddenError,
    DLServerError,
    DLTaskNotFoundError,
    DLUnauthorizedError,
    dl_error_response,
    fetch_task_info,
)
from ..http_utils import resolve_dl_session_id
from ..models import AIModelTestResult, AIModelTestRun, AIRequestLog, AIPinnedBatchRun, Task
from ..model_health import get_runtime_model_handlers
from .permissions import can_access_logs, is_staff_or_superuser, logs_scope_is_own_user

logger = logging.getLogger(__name__)

# Run id из message batch-лога ("Batch solve run <uuid4 hex>"). Единственная
# копия паттерна: отсюда его читают _batch_run_id_from_log и все потребители.
_BATCH_RUN_ID_RE = re.compile(r"Batch solve run ([0-9a-f]{32})")


def _batch_run_id_from_log(log):
    """Run id batch-прогона из записи журнала или None."""
    m = _BATCH_RUN_ID_RE.search(log.message or "")
    return m.group(1) if m else None


def dl_task_url(node_id, course_id=None):
    """Пользовательская ссылка на задачу в DL (не admin-вьювер).

    ``/task.jsp?nid=<узел>&cid=<курс>`` (nid первым, cid вторым). cid —
    это дерево задач: приписка ``&cid=`` ставится ВСЕГДА — если курс записи
    не известен (старые записи кэша/журнала), подставляем единственное
    дерево DL (DL_DEFAULT_COURSE_ID), иначе DL откроет задачу без контекста
    дерева. Просмотр условия от имени админа DL (fullTaskviewer) не
    используется.
    """
    if not node_id:
        return None
    cid = course_id or DL_DEFAULT_COURSE_ID
    return f"https://dl.gsu.by/task.jsp?nid={node_id}&cid={cid}"


# Отображение статуса batch-прогона (AIModelTestRun.status) в журналах:
# по требованиям только «Выполнен» / «Прерван»; running — переходное
# «Выполняется…». AIRequestLog.status (Success/Error) в БД не меняется —
# это только слой отображения.
_BATCH_RUN_STATUS_DISPLAY = {
    "completed": "Выполнен",
    "cancelled": "Прерван",
    "failed": "Прерван",
    "running": "Выполняется…",
}


def batch_run_status_display(status):
    """«Выполнен» / «Прерван» (failed тоже прерван — не доведён до конца)."""
    return _BATCH_RUN_STATUS_DISPLAY.get(status, "")


def run_name_for(run):
    """Название прогона из AIModelTestRun.run_name (фолбэк — старый run_params).

    Нет прогона / пустое название → "—".
    """
    if not run:
        return "—"
    return run.run_name or (run.run_params or {}).get("run_name") or "—"


def run_finish_meta(run):
    """«Окончание» и «Общее время» завершённого прогона — для инлайн-блоков
    «Прогон» в списках журнала и «Настройки ИИ-приложения».

    run is None / не завершён (finished_at пуст) → пустые строки: шаблон
    покажет «—» (default/|| срабатывают и на пустой строке). Длительность
    в человекочитаемый вид — зеркало fmtDuration из _ai_batch_results.html
    («1 ч 27 мин» / «12 мин 3 с» / «45 с»).
    """
    finished_at = run.finished_at if run else None
    if not finished_at:
        return {"run_finished_at": "", "run_duration_display": ""}
    duration = (finished_at - run.started_at).total_seconds() if run.started_at else None
    if duration is not None and duration > 0:
        h, rem = divmod(int(duration), 3600)
        m, s = divmod(rem, 60)
        if h:
            duration_display = f"{h} ч {m} мин"
        elif m:
            duration_display = f"{m} мин" + (f" {s} с" if s else "")
        else:
            duration_display = f"{s} с"
    else:
        duration_display = ""
    return {
        "run_finished_at": timezone.localtime(finished_at, MOSCOW_TZ).strftime("%d.%m.%Y %H:%M:%S"),
        "run_duration_display": duration_display,
    }


def _parse_date(value: str) -> str:
    """Return ``value`` only if it is a real ``YYYY-MM-DD`` date, else "".

    Defensive: malformed inputs (including the ``"['']"`` string that the old
    ``urlencode`` without ``doseq`` leaked into pagination links) never reach the
    ORM ``__date__gte`` lookup, which would otherwise raise ``ValidationError``
    and 500 the page.
    """
    value = (value or "").strip()
    if not value:
        return ""
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return ""
    return value


def _scope_logs_qs(qs, user):
    """Журнал с учётом строгого фильтра «только свои» для prompt_developer."""
    if logs_scope_is_own_user(user):
        return qs.filter(user=user)
    return qs


def _get_scoped_log(request, log_id, json_errors=True):
    """Запись журнала с учётом «только свои» (prompt_developer).

    Возвращает ``(log, error_response)``: ровно одно из двух не ``None``.
    404 — записи нет; 403 — разработчику чужую запись смотреть нельзя.
    ``json_errors=True`` — JsonResponse (AJAX-эндпоинты), иначе
    HttpResponseForbidden (HTML-страница деталей).
    """
    log = AIRequestLog.objects.filter(pk=log_id).first()
    if log is None:
        error = JsonResponse({"error": "Запись журнала не найдена"}, status=404)
        return None, error if json_errors else HttpResponseForbidden("Запись журнала не найдена")
    if logs_scope_is_own_user(request.user) and log.user_id != request.user.pk:
        message = "Доступны только собственные запросы"
        error = JsonResponse({"error": message}, status=403)
        return None, error if json_errors else HttpResponseForbidden(message)
    return log, None


class AIRequestLogAdmin(admin.ModelAdmin):
    list_display = (
        "sent_at_display",
        "received_at_display",
        "sender_display",
        "programming_language_name",
        "topic_name_display",
        "task_display",
        "prompt_name",
        "model_names_display",
        "status",
        "mode_display",
        "duration_seconds_display",
    )
    list_filter = ("status", "mode", "programming_language_name", "sent_at")
    search_fields = (
        "external_user_id",
        "username",
        "user_full_name",
        "message",
        "programming_language_name",
        "topic_name",
        "prompt_name",
        "task_name",
    )
    date_hierarchy = "sent_at"
    ordering = ("-sent_at",)
    readonly_fields = [f.name for f in AIRequestLog._meta.fields]

    def get_queryset(self, request):
        # Оборона в глубину: ModelAdmin-URL и так закрыт для не-staff
        # (has_module_permission ниже), но queryset всё равно ограничиваем.
        return _scope_logs_qs(super().get_queryset(request), request.user)

    def has_module_permission(self, request):
        return can_access_logs(request) and is_staff_or_superuser(request.user)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        # Разработчикам препромтов журнал доступен только на чтение,
        # и только свои записи — удаление только staff/superuser.
        return is_staff_or_superuser(request.user)

    def sent_at_display(self, obj):
        return _format_moscow_datetime(obj.sent_at)
    sent_at_display.short_description = "Отправлен"

    def received_at_display(self, obj):
        return _format_moscow_datetime(obj.received_at)
    received_at_display.short_description = "Получен"

    def sender_display(self, obj):
        """Return sender full name with DL ID as a clickable link."""
        name = obj.user_full_name or obj.username or ""
        ext_id = obj.external_user_id or ""
        if name and ext_id:
            return f'{name} <a href="https://dl.gsu.by/report.asp?id={ext_id}" target="_blank" rel="noopener">[{ext_id}]</a>'
        if ext_id:
            return f'<a href="https://dl.gsu.by/report.asp?id={ext_id}" target="_blank" rel="noopener">[{ext_id}]</a>'
        return name or "—"
    sender_display.short_description = "Кто отправлял"
    sender_display.allow_tags = True

    def model_names_display(self, obj):
        return ", ".join(obj.model_names or []) or "—"
    model_names_display.short_description = "Модель"

    def programming_language_name(self, obj):
        return obj.programming_language_name or "—"
    programming_language_name.short_description = "Язык программирования"

    def topic_name_display(self, obj):
        return obj.topic_name or "—"
    topic_name_display.short_description = "Тема"

    def task_display(self, obj):
        """Имя задачи; пользовательская ссылка в DL — всегда с cid (дерево
        задач: курс записи или единственное дерево DL, см. dl_task_url)."""
        if obj.task_node_id:
            name = obj.task_name or str(obj.task_node_id)
            url = dl_task_url(obj.task_node_id, getattr(obj, "course_id", None))
            if url:
                return f'<a href="{url}" target="_blank" rel="noopener">{name}</a>'
            return name
        return obj.task_name or "—"
    task_display.short_description = "Задача"
    task_display.allow_tags = True

    def prompt_name(self, obj):
        return obj.prompt_name or "—"
    prompt_name.short_description = "Препромпт"

    def duration_seconds_display(self, obj):
        if obj.duration_seconds is None:
            return "—"
        return str(round(obj.duration_seconds))
    duration_seconds_display.short_description = "Время ответа, с"

    def mode_display(self, obj):
        return obj.get_mode_display() or "—"
    mode_display.short_description = "Режим"


def _format_moscow_datetime(value):
    if not value:
        return "—"
    local = timezone.localtime(value, MOSCOW_TZ)
    return local.strftime("%d.%m.%Y:%H:%M:%S")


def build_recent_log_rows(request, limit=5):
    """Контекст-хелпер: последние N записей журнала для встраивания в другую
    страницу (напр. «Настройки ИИ-приложения»).

    Отдаёт те же данные, что и колонки «Журнала запросов» (ID, отправлен,
    кто, язык, тема, модель, статус, режим, время), но без пагинации и
    фильтров, + ссылка на поиск по всему журналу. Учитывает ограничение
    видимости (``logs_scope_is_own_user`` → только свои записи).

    Возвращает словарь для ``extra_context``: ``recent_logs`` (список строк)
    и ``logs_search_url`` (полный журнал). Если у пользователя нет доступа к
    журналу — ``recent_logs`` пуст и ``can_view_logs`` False.
    """
    can_view = can_access_logs(request)
    rows = []
    if can_view:
        qs = _scope_logs_qs(AIRequestLog.objects.all(), request.user).order_by("-sent_at")
        logs = list(qs[: max(0, int(limit))])
        # Batch-записи: статус показываем по статусу прогона (Выполнен/Прерван),
        # а не по Success/Error записи журнала. Один запрос по ≤5 run_id.
        batch_run_by_log = {}
        batch_run_ids = {
            log.pk: _batch_run_id_from_log(log)
            for log in logs
            if _is_batch_solve_log(log)
        }
        if batch_run_ids:
            runs_by_hex = {
                r.run_id: r
                for r in AIModelTestRun.objects.filter(run_id__in=set(batch_run_ids.values()))
            }
            batch_run_by_log = {
                log_pk: runs_by_hex[run_hex]
                for log_pk, run_hex in batch_run_ids.items()
                if run_hex in runs_by_hex
            }
        for log in logs:
            run = batch_run_by_log.get(log.pk)
            status_ok = (run.status == "completed") if run else (log.status == "success")
            rows.append({
                "id": log.id,
                "sent_at": log.sent_at,
                "sender": log.user_full_name or log.username or "—",
                "external_user_id": log.external_user_id or "",
                "programming_language_name": log.programming_language_name or "—",
                "topic_name": log.topic_name or "—",
                "model_names": ", ".join(log.model_names or []) or "—",
                "status": log.status,
                "status_display": (
                    batch_run_status_display(run.status) if run else log.get_status_display()
                ),
                "status_class": "ok" if status_ok else "err",
                "run_name": run_name_for(run),
                "mode_display": log.get_mode_display() or "—",
                "duration_seconds": log.duration_seconds,
                "detail_url": f"/ai/admin/ai/airequestlog/{log.id}/",
            })
    return {
        "recent_logs": rows,
        "recent_logs_limit": limit,
        "can_view_logs": can_view,
        # Полный журнал: там есть поиск/фильтры по всему журналу. Ссылка ведёт
        # на страницу журнала с фокусом на поле поиска (см. шаблон настроек).
        "logs_search_url": "/ai/admin/ai/airequestlog/?focus=1",
        # Для {% timezone moscow_tz %} в aiappsettings_change_form.html —
        # тот же контракт, что у admin_request_logs_view.
        "moscow_tz": MOSCOW_TZ,
    }


def build_batch_rows_for_logs(request, logs):
    """Лёгкие строки batch-прогонов для списка записей журнала.

    Общий хелпер «Последних пакетных решений» на «Настройке ИИ-приложения» и
    страницы «Закреплённые пакетные решения»: для каждой batch-записи журнала
    — шапка прогона (AIModelTestRun) + count-агрегат результатов, без текстов;
    развёртка результатов — лениво, AJAX по snapshot_url. Для каждого прогона
    проставляется флаг ``pinned`` (закреплён ли текущим пользователем).
    """
    run_ids = {}
    for log in logs:
        run_hex = _batch_run_id_from_log(log)
        if run_hex:
            run_ids[log.pk] = run_hex
    runs = {
        r.run_id: r
        for r in AIModelTestRun.objects.filter(run_id__in=set(run_ids.values()))
    }
    pinned_log_ids = set(
        AIPinnedBatchRun.objects.filter(user=request.user, log_id__in=[l.pk for l in logs])
        .values_list("log_id", flat=True)
    ) if logs else set()
    # Сводка по результатам прогонов — count-запросом, без текстов;
    # семантика как в _build_batch_report (total/solved/failed).
    counters = {}
    first_ext = {}
    if run_ids:
        for row in (
            AIModelTestResult.objects
            .filter(run__run_id__in=set(run_ids.values()))
            .values("run__run_id")
            .annotate(
                total=Count("pk"),
                solved=Count("pk", filter=Q(verdict=AIModelTestResult.VERDICT_SOLVED)),
                failed=Count("pk", filter=Q(verdict=AIModelTestResult.VERDICT_FAILED)),
            )
        ):
            counters[row["run__run_id"]] = row
        # file_extension строки — первый непустой снимок результата прогона
        # (тот же приоритет, что в _build_batch_log_snapshot).
        for row in (
            AIModelTestResult.objects
            .filter(run__run_id__in=set(run_ids.values()))
            .exclude(file_extension_snapshot="")
            .order_by("pk")
            .values("run__run_id", "file_extension_snapshot")
        ):
            first_ext.setdefault(row["run__run_id"], row["file_extension_snapshot"])
    rows = []
    for log in logs:
        run_hex = run_ids.get(log.pk)
        run = runs.get(run_hex) if run_hex else None
        counter = counters.get(run_hex, {})
        run_status = run.status if run else ""
        rows.append({
            "id": log.id,
            "sent_at": log.sent_at,
            "status": log.status,
            # Статус прогона («Выполнен»/«Прерван»); прогон стёрт из БД →
            # старое отображение по записи журнала.
            "run_status_display": batch_run_status_display(run_status) or log.get_status_display(),
            # Сырой статус прогона — для класса цвета в шаблоне
            # (прогон стёрт из БД → старый статус записи журнала).
            "run_status": run_status or log.status,
            "run_name": run_name_for(run),
            "run_id": run_hex or "",
            "course_id": run.course_id if run else None,
            "file_extension": first_ext.get(run_hex, ""),
            "total_pairs": counter.get("total"),
            "solved": counter.get("solved"),
            "failed": counter.get("failed"),
            "detail_url": f"/ai/admin/ai/airequestlog/{log.id}/",
            # Полные результаты — лениво, AJAX-ом по клику: страница
            # настроек больше не встраивает снапшоты в HTML.
            "snapshot_url": f"/ai/admin/ai/airequestlog/{log.id}/batch-snapshot/",
            # Закреплён ли прогон текущим пользователем (★ кнопки на страницах)
            # и адрес переключения (POST, см. pinned.py).
            "pinned": log.pk in pinned_log_ids,
            "pin_url": f"/ai/admin/ai/airequestlog/{log.id}/pin/",
        })
    return rows


def admin_request_log_detail_json_view(request, log_id):
    """JSON-детали записи журнала для ленивой развёртки на «Настройке ИИ».

    Клик по строке блока «Последние запросы» на странице «Настройка
    ИИ-приложения» подтягивает полный текст запроса/ответа/ошибки (в HTML
    страницы они не вшиты — как и batch-снапшоты, они могут быть большими).
    Права — те же, что у журнала: can_access_logs + «только свои» для
    prompt_developer (_get_scoped_log).
    """
    if request.method != "GET":
        return JsonResponse({"ok": False, "error": "Метод не поддерживается"}, status=405)
    if not can_access_logs(request):
        return HttpResponseForbidden("Access denied")

    log, error = _get_scoped_log(request, log_id)
    if log is None:
        return error

    is_batch = _is_batch_solve_log(log)
    run = None
    if is_batch:
        run_hex = _batch_run_id_from_log(log)
        run = AIModelTestRun.objects.filter(run_id=run_hex).first() if run_hex else None

    return JsonResponse({
        "ok": True,
        "log": {
            "id": log.id,
            "source_display": log.get_source_display() or "—",
            "mode_display": log.get_mode_display() or "—",
            "status_display": log.get_status_display(),
            "model_names": ", ".join(log.model_names or []) or "—",
            "tokens": log.tokens,
            "duration_seconds": log.duration_seconds,
            "sent_at": timezone.localtime(log.sent_at).strftime("%d.%m.%Y %H:%M:%S"),
            "received_at": (
                timezone.localtime(log.received_at).strftime("%d.%m.%Y %H:%M:%S")
                if log.received_at else "—"
            ),
            "sender": (
                log.user_full_name or log.username or "—"
            ),
            "external_user_id": log.external_user_id or "",
            "programming_language_name": log.programming_language_name or "—",
            "topic_name": log.topic_name or "—",
            "prompt_name": log.prompt_name or "—",
            "prompt_id": log.prompt_id,
            "task_node_id": log.task_node_id,
            "task_name": log.task_name or "",
            # Для batch-записей тексты не отдаём (могут быть десятки МБ;
            # результаты прогона — через batch-snapshot в блоке выше).
            "message": log.message or "",
            "response_text": "" if is_batch else (log.response_text or ""),
            "is_batch": is_batch,
            "error_message": log.error_message or "",
            "detail_url": f"/ai/admin/ai/airequestlog/{log.id}/",
            "run_name": run_name_for(run) if run else "",
            "run_status_display": batch_run_status_display(run.status) if run else "",
            # Время окончания и общее время завершённого прогона (инлайн-блок
            # «Прогон» в развёртке «Последних запросов»; не завершён → «—»).
            **run_finish_meta(run),
        },
    })


def build_recent_batch_rows(request, limit=5):
    """Контекст-хелпер: последние N batch-solve прогонов (для страницы
    «Настройки ИИ-приложения»).

    Отбирает записи журнала пакетного решения (source="arm",
    mode=batch_solve|solve, sentinel "Batch solve run " в message) с учётом
    ограничения видимости («только свои» для prompt_developer).

    Строка — ЛЁГКАЯ: шапка прогона (один запрос AIModelTestRun) и
    count-агрегат результатов (AIModelTestResult) — без текстов решений:
    раньше полные снапшоты (results + report) встраивались в HTML страницы
    через {% json_script %} и большой прогон раздувал её до ~6 МБ. Теперь
    развёртка грузится ЛЕНИВО по клику — AJAX GET на ``snapshot_url`` →
    ``admin_batch_snapshot_view`` (тот же формат снапшота, что потребляет
    window.ArmBatchResults на /arm/solve/).

    Возвращает словарь для ``extra_context``: ``recent_batch_runs`` (список
    лёгких строк), ``recent_batch_limit`` и ``moscow_tz``.
    """
    can_view = can_access_logs(request)
    rows = []
    if can_view:
        qs = (
            _scope_logs_qs(AIRequestLog.objects.all(), request.user)
            .filter(source="arm", mode__in=("batch_solve", "solve"))
            .filter(message__icontains="Batch solve run ")
            .order_by("-sent_at")
        )
        logs = list(qs[: max(0, int(limit))])
        rows = build_batch_rows_for_logs(request, logs)
    return {
        "recent_batch_runs": rows,
        "recent_batch_limit": limit,
        "moscow_tz": MOSCOW_TZ,
    }


def admin_batch_snapshot_view(request, log_id):
    """AJAX-снапшот результатов пакетного прогона по записи журнала.

    Ленивая загрузка для страницы «Настройки ИИ-приложения»: полные
    результаты (results + report) не встраиваются в HTML страницы, а
    подтягиваются по клику на строку. Права — как у журнала
    (can_access_logs + скоуп «только свои»); формат — тот же
    ``_build_batch_log_snapshot``, что потребляет window.ArmBatchResults.
    """
    if not can_access_logs(request):
        return HttpResponseForbidden("Access denied")
    log = _scope_logs_qs(AIRequestLog.objects.filter(pk=int(log_id)), request.user).first()
    if log is None or not _is_batch_solve_log(log):
        return JsonResponse({"error": "not-found"}, status=404)
    snapshot = _build_batch_log_snapshot(log)
    if snapshot is None:
        # Прогон стёрт из БД — развёртка недоступна (как и раньше).
        return JsonResponse({"error": "run-not-found"}, status=404)
    return JsonResponse(snapshot)


def admin_daily_report_view(request):
    """Дневной отчёт журнала: студенты × сегодняшняя активность (МСК).

    День — календарный по МСК (00:00–23:59). Строка — студент (внешний
    dl-аккаунт или локальный юзер): фамилия, время последней отправки (без
    даты), последняя тема и препромпт, последний режим («какой чат») и
    сколько всего запросов за сегодня. Клик по колонке «Запросов»
    разворачивает свёрнутый список всех его записей дня — данные уже в
    HTML страницы, второго запроса не нужно.

    Права — как у журнала (can_access_logs + скоуп «только свои» для
    prompt_developer). ARM-прогоны (source=arm) — операторские пакетные
    запуски, а не активность студентов, в отчёт не попадают.
    """
    if not can_access_logs(request):
        return HttpResponseForbidden("Access denied")

    now_msk = timezone.localtime(timezone.now(), MOSCOW_TZ)
    day_start = now_msk.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = now_msk.replace(hour=23, minute=59, second=59, microsecond=999_000)
    # .only() — без message/response_text: тексты тяжёлые, отчёту не нужны.
    logs = list(
        _scope_logs_qs(AIRequestLog.objects.all(), request.user)
        .exclude(source=AIRequestLog.SOURCE_ARM)
        .filter(
            sent_at__gte=day_start.astimezone(dt_timezone.utc),
            sent_at__lte=day_end.astimezone(dt_timezone.utc),
        )
        .only(
            "id", "sent_at", "external_user_id", "username", "user_full_name",
            "topic_id", "topic_name", "prompt_id", "prompt_name", "mode",
            "status", "model_names",
        )
        .order_by("sent_at")
    )

    students = {}
    for log in logs:
        key = log.external_user_id or f"local:{log.username or '?'}"
        rec = students.get(key)
        if rec is None:
            rec = students[key] = {
                "display_name": log.user_full_name or log.username or key,
                "external_user_id": log.external_user_id or "",
                "count": 0,
                "last": None,
                "requests": [],
            }
        rec["count"] += 1
        rec["last"] = log
        rec["requests"].append(log)

    def _request_row(log):
        return {
            "time": timezone.localtime(log.sent_at, MOSCOW_TZ).strftime("%H:%M:%S"),
            "mode": log.get_mode_display() or "—",
            "topic": log.topic_name or "—",
            "prompt": log.prompt_name or "—",
            "model": ", ".join(log.model_names or []) or "—",
            "status": log.get_status_display() or log.status or "—",
            "status_class": "ok" if log.status == AIRequestLog.STATUS_SUCCESS else "err",
        }

    rows = []
    for rec in students.values():
        last = rec["last"]
        rows.append({
            "display_name": rec["display_name"],
            "external_user_id": rec["external_user_id"],
            "last_sent": timezone.localtime(last.sent_at, MOSCOW_TZ).strftime("%H:%M"),
            "last_topic": last.topic_name or "—",
            "last_prompt": last.prompt_name or "—",
            "last_mode": last.get_mode_display() or "—",
            "count": rec["count"],
            # Все записи дня, новые сверху (журнал тоже читается сверху вниз).
            "requests": [_request_row(l) for l in reversed(rec["requests"])],
        })
    # По фамилии — алфавит, без учёта регистра.
    rows.sort(key=lambda r: (r["display_name"].casefold(), r["last_sent"]))

    context = {
        **ai_admin_site.each_context(request),
        "title": "DL.AI: Дневной отчёт",
        "date_label": now_msk.strftime("%d.%m.%Y"),
        "rows": rows,
        "total_count": len(logs),
    }
    return TemplateResponse(request, "admin/ai/daily_report.html", context)


def admin_request_logs_view(request):
    if not can_access_logs(request):
        return HttpResponseForbidden("Access denied")

    qs = _scope_logs_qs(AIRequestLog.objects.all(), request.user)

    status = request.GET.get("status", "").strip()
    source = request.GET.get("source", "").strip()
    mode = request.GET.get("mode", "").strip()
    model = request.GET.get("model", "").strip()
    user_q = request.GET.get("user", "").strip()
    task_q = request.GET.get("task", "").strip()
    log_id = request.GET.get("id", "").strip()
    text_q = request.GET.get("q", "").strip()
    date_from = _parse_date(request.GET.get("date_from", ""))
    date_to = _parse_date(request.GET.get("date_to", ""))

    status_values = dict(AIRequestLog.STATUS_CHOICES)
    source_values = dict(AIRequestLog.SOURCE_CHOICES)
    mode_values = dict(AIRequestLog.MODE_CHOICES)

    if status in status_values:
        qs = qs.filter(status=status)
    if source in source_values:
        qs = qs.filter(source=source)
    if mode in mode_values:
        qs = qs.filter(mode=mode)
    if model:
        qs = qs.filter(model_names__contains=[model])
    if user_q:
        qs = qs.filter(
            Q(user_full_name__icontains=user_q)
            | Q(username__icontains=user_q)
            | Q(external_user_id__icontains=user_q)
        )
    if log_id.isdigit():
        qs = qs.filter(pk=int(log_id))
    if text_q:
        # Поиск по всему журналу: текст запроса/ответа, модель, язык, тема,
        # препромпт, название задачи.
        qs = qs.filter(
            Q(message__icontains=text_q)
            | Q(response_text__icontains=text_q)
            | Q(prompt_name__icontains=text_q)
            | Q(programming_language_name__icontains=text_q)
            | Q(topic_name__icontains=text_q)
            | Q(task_name__icontains=text_q)
        )
    if task_q:
        if task_q.isdigit():
            qs = qs.filter(task_node_id=int(task_q))
        else:
            qs = qs.filter(task_name__icontains=task_q)
    if date_from:
        qs = qs.filter(sent_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(sent_at__date__lte=date_to)

    qs = qs.order_by("-sent_at")

    paginator = Paginator(qs, 50)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    # Строки страницы парами (log, batch-контекст) — batch-контекст None для
    # обычных записей. Так шаблон читает его без dict-lookup по pk.
    batch_contexts = _batch_log_row_contexts(page_obj.object_list, user=request.user)
    rows = [(log, batch_contexts.get(log.pk)) for log in page_obj.object_list]

    # Build the query string carried by pagination links. ``urlencode`` over a
    # QueryDict *without* doseq stringifies each value-list (``str(['x'])`` =
    # "['x']"), which is how the pagination URL used to carry ``date_from=['']``
    # and 500 page 2. Build a plain dict of non-empty, non-garbage values and
    # urlencode with doseq=True so list values expand to real parameters.
    cleaned = {}
    for key in request.GET:
        if key == "page":
            continue
        for val in request.GET.getlist(key):
            val = (val or "").strip()
            if not val:
                continue
            # Drop leftover stringified-list garbage ("['']", "['x']") from
            # stale bookmarks/links; doseq above stops us from generating it.
            if val.startswith("[") and val.endswith("]"):
                continue
            cleaned.setdefault(key, []).append(val)
    filters_query_str = urlencode(cleaned, doseq=True)
    if filters_query_str:
        filters_query_str += "&"

    # Build model choices from runtime handlers (all registered models).
    _handlers = get_runtime_model_handlers()
    model_choices = [
        {"key": key, "title": info["title"]}
        for key, info in _handlers.items()
    ]

    # Build task choices from distinct task_node_id/task_name pairs in logs.
    task_choices = [
        {"node_id": t["task_node_id"], "name": t["task_name"] or str(t["task_node_id"])}
        for t in AIRequestLog.objects.exclude(task_node_id__isnull=True)
            .values("task_node_id", "task_name").distinct().order_by("-task_node_id")
    ]

    context = {
        **ai_admin_site.each_context(request),
        "title": "DL.AI: Логи запросов",
        "page_obj": page_obj,
        "rows": rows,
        "status_choices": AIRequestLog.STATUS_CHOICES,
        "source_choices": AIRequestLog.SOURCE_CHOICES,
        "mode_choices": AIRequestLog.MODE_CHOICES,
        "model_choices": model_choices,
        "task_choices": task_choices,
        "filters_query": filters_query_str,
        "own_logs_only": logs_scope_is_own_user(request.user),
        "moscow_tz": MOSCOW_TZ,
        "filters": {
            "status": status,
            "source": source,
            "mode": mode,
            "model": model,
            "user": user_q,
            "task": task_q,
            "id": log_id,
            "q": text_q,
            "date_from": date_from,
            "date_to": date_to,
        },
    }
    return TemplateResponse(request, "admin/ai/request_logs.html", context)


def _is_batch_solve_log(log):
    """True для записей batch-solve ARM-прогона (старых mode=solve+sentinel и
    новых mode=batch_solve). У таких логов вместо текста запроса в деталях
    рисуется мини-таблица результатов из /arm/solve/."""
    return (
        log.source == "arm"
        and log.mode in ("batch_solve", "solve")
        and "Batch solve run " in (log.message or "")
    )


def _build_batch_log_snapshot(log):
    """Собрать snapshot {run_id, course_id, file_extension, node_ids, results,
    report, run_status, run_name} для детали batch-solve лога — тот же формат,
    что потребляет JS мини-таблицы в /arm/solve/. Возвращает None, если
    прогон/результаты не найдены (тогда детал отрисуется как обычный
    текстовый лог)."""
    from ..arm_runner import _batch_results_from_db, _build_batch_report

    run_id_hex = _batch_run_id_from_log(log)
    if not run_id_hex:
        return None
    try:
        test_run = AIModelTestRun.objects.get(run_id=run_id_hex)
    except AIModelTestRun.DoesNotExist:
        return None

    # Лёгкие результаты (lazy_body): деталь журнала не встраивает мегабайты
    # текста в HTML — тело результата подтягивается по AJAX при раскрытии
    # строки (тот же эндпоинт, что у /arm/solve/).
    results = _batch_results_from_db(test_run, light=True)
    report = _build_batch_report(results)
    # Карточка «Прогон» (время) — та же, что на /arm/solve/.
    from ..arm_runner import _attach_run_meta
    _attach_run_meta(
        report,
        test_run.started_at.timestamp() if test_run.started_at else None,
        test_run.finished_at.timestamp() if test_run.finished_at else None,
        now_ts=time.time() if test_run.status == AIModelTestRun.STATUS_RUNNING else None,
    )

    # file_extension — первый непустой снимок из результатов.
    file_extension = ""
    node_ids = []
    seen_nodes = set()
    for r in results:
        ext = r.get("file_extension") or ""
        if ext and not file_extension:
            file_extension = ext
        nid = r.get("task_node_id")
        if nid and nid not in seen_nodes:
            seen_nodes.add(nid)
            node_ids.append(nid)

    return {
        "run_id": run_id_hex,
        "course_id": test_run.course_id,
        "file_extension": file_extension,
        "node_ids": node_ids,
        "results": results,
        "report": report,
        # Статус прогона и ручное название — для шапки деталей журнала.
        "run_status": test_run.status,
        "run_name": run_name_for(test_run),
    }


def _batch_log_row_contexts(logs, user=None):
    """Bulk-контекст batch-строк для страницы списка журнала.

    Для каждой batch-solve записи страницы (одним запросом AIModelTestRun и
    одним запросом AIModelTestResult с select_related('task')) собирает
    {log.pk: {run_id, course_id, file_extension, tasks: [{node_id, name}],
    run_status_display, run_name, pinned}}.
    В tasks попадают только НЕрешённые задачи прогона (ни одна модель не дала
    solved) — решённые в списке журнала не показываются.
    Отличие от _build_batch_log_snapshot: НЕ тянет raw_response/results —
    списку нужен только курс/задачи, без N+1 по 50 строкам.

    ``user`` — для флага ``pinned`` (закреплён ли прогон этим пользователем,
    ★ кнопка закрепления в списке журнала); None — флаг не проставляется.
    """
    from ..models import AIModelTestResult

    run_ids = {}
    for log in logs:
        if not _is_batch_solve_log(log):
            continue
        run_id = _batch_run_id_from_log(log)
        if run_id:
            run_ids[log.pk] = run_id
    if not run_ids:
        return {}

    # Закрепления пользователя — одним запросом (★ в строках журнала).
    pinned_log_pks = set()
    if user is not None and getattr(user, "is_authenticated", False):
        pinned_log_pks = set(
            AIPinnedBatchRun.objects.filter(
                user=user, log_id__in=set(run_ids)
            ).values_list("log_id", flat=True)
        )

    contexts = {}
    runs = {
        r.run_id: r
        for r in AIModelTestRun.objects.filter(run_id__in=set(run_ids.values()))
    }

    # Задачи и расширение — по прогону (run_id уникален), затем раскладываем
    # по строкам журнала: одна запись журнала = один прогон. Решённые задачи
    # (задача×модель с verdict='solved' есть хотя бы для одной модели) в списке
    # журнала не показываем — интерес представляют только нерешённые.
    tasks_by_run = {}
    solved_nodes_by_run = {}
    ext_by_run = {}
    for r in (
        AIModelTestResult.objects.select_related("task", "run")
        .filter(run__run_id__in=set(run_ids.values()))
        .order_by("id")
    ):
        # r.run_id — это pk FK-поля run; строковый идентификатор прогона —
        # r.run.run_id (тот же, что в AIModelTestRun.run_id).
        run_key = r.run.run_id
        ext = r.file_extension_snapshot or ""
        if ext and run_key not in ext_by_run:
            ext_by_run[run_key] = ext
        node_id = r.task.node_id if r.task else None
        if node_id:
            if r.verdict == "solved":
                solved_nodes_by_run.setdefault(run_key, set()).add(node_id)
            bucket = tasks_by_run.setdefault(run_key, [])
            if not any(t["node_id"] == node_id for t in bucket):
                bucket.append({
                    "node_id": node_id,
                    "name": r.task.name if r.task else "",
                })

    for log_pk, run_id in run_ids.items():
        run_obj = runs.get(run_id)
        if run_obj is None:
            continue
        solved_nodes = solved_nodes_by_run.get(run_id, set())
        tasks = [
            t for t in tasks_by_run.get(run_id, [])
            if t["node_id"] not in solved_nodes
        ]
        contexts[log_pk] = {
            "run_id": run_id,
            "course_id": run_obj.course_id,
            "file_extension": ext_by_run.get(run_id, ""),
            "tasks": tasks,
            # Статус прогона (Выполнен/Прерван) и ручное название —
            # отображаются в строке журнала вместо статуса записи.
            "run_status": run_obj.status,
            "run_status_display": batch_run_status_display(run_obj.status),
            "run_name": run_name_for(run_obj),
            # Время окончания и общее время завершённого прогона (инлайн-блок
            # «Прогон» в развёртке строки; не завершён → пустые строки → «—»).
            **run_finish_meta(run_obj),
            # Свёрнутые строки для ячейки таблицы (первые 3 + «+N ещё»)
            # и полный список для title-подсказки.
            "tasks_preview": _tasks_preview(tasks),
            "tasks_title": "; ".join(f"{t['name']} ({t['node_id']})" for t in tasks),
            # Закреплён ли прогон этим пользователем (★ кнопка закрепления).
            "pinned": log_pk in pinned_log_pks,
            "pin_url": f"/ai/admin/ai/airequestlog/{log_pk}/pin/",
        }
    return contexts


def _tasks_preview(tasks, limit=3):
    """«Задача (id), Задача (id), Задача (id) +N ещё» — для ячейки списка."""
    if not tasks:
        return "—"
    parts = [f"{t['name']} ({t['node_id']})" for t in tasks[:limit]]
    if len(tasks) > limit:
        parts.append(f"+{len(tasks) - limit} ещё")
    return ", ".join(parts)


def _arm_results_xlsx_response(results, run_id):
    """Общий HTTP-ответ с .xlsx-матрицей задача×модель (см. export.py)."""
    from .export import XLSX_CONTENT_TYPE, build_arm_results_xlsx

    payload = build_arm_results_xlsx(results)
    response = HttpResponse(
        payload, content_type=XLSX_CONTENT_TYPE,
    )
    response["Content-Disposition"] = (
        f'attachment; filename="arm_solve_results_{run_id}.xlsx"'
    )
    return response


def admin_request_log_xlsx_view(request, log_id):
    """XLSX-выгрузка результатов batch-solve прогона из записи журнала.

    Тот же файл, что даёт кнопка «Скачать результаты» на /arm/solve/:
    матрица задача×модель (см. export.build_arm_results_xlsx).
    """
    if not can_access_logs(request):
        return HttpResponseForbidden("Access denied")

    log, error = _get_scoped_log(request, log_id)
    if error is not None:
        return error
    if not _is_batch_solve_log(log):
        return JsonResponse({"error": "Выгрузка доступна только для записей пакетного решения"}, status=404)

    snapshot = _build_batch_log_snapshot(log)
    if snapshot is None:
        return JsonResponse({"error": "Прогон ARM не найден в БД"}, status=404)
    if not snapshot["results"]:
        return JsonResponse({"error": "В прогоне нет результатов"}, status=404)

    return _arm_results_xlsx_response(snapshot["results"], snapshot["run_id"])


def admin_request_log_detail_view(request, log_id):
    if not can_access_logs(request):
        return HttpResponseForbidden("Access denied")

    log, error = _get_scoped_log(request, log_id, json_errors=False)
    if error is not None:
        return error

    context = {
        **ai_admin_site.each_context(request),
        "title": "DL.AI: Детали запроса",
        "log": log,
        "moscow_tz": MOSCOW_TZ,
        "is_batch_log": False,
        "batch_snapshot": None,
    }

    if _is_batch_solve_log(log):
        snapshot = _build_batch_log_snapshot(log)
        if snapshot is not None:
            context["is_batch_log"] = True
            context["batch_snapshot"] = snapshot

    return TemplateResponse(request, "admin/ai/airequestlog_detail.html", context)


@require_POST
def resend_request_view(request, log_id):
    """Повторная отправка запроса к AI-модели из записи лога.

    Берёт message, model_key из лога и отправляет на модель через
    async_to_sync(handler)(message, client_id). Создаёт новую запись лога.
    """
    from asgiref.sync import async_to_sync
    from django.utils import timezone

    if not can_access_logs(request):
        return JsonResponse({"error": "Access denied"}, status=403)

    log, error = _get_scoped_log(request, log_id)
    if error is not None:
        return error

    # ARM batch-solve logs have message="Batch solve run <run_id>" and must be
    # rerun as a full batch (same models, tasks, prompt, language) under the
    # caller's own DLSID — a plain resend to one model would be meaningless.
    # Старые batch-логи использовали mode="solve" + sentinel; новые —
    # mode="batch_solve". Принимаем оба.
    if (
        log.source == "arm"
        and log.mode in ("batch_solve", "solve")
        and "Batch solve run " in (log.message or "")
    ):
        return _rerun_arm_batch(request, log)

    # Resolve the model handler.
    handlers = get_runtime_model_handlers()
    model_names = log.model_names or []
    if not model_names:
        return JsonResponse({"error": "В логе не указана модель"}, status=400)

    # Try to find a matching handler by model name (could be key or title).
    model_key = None
    for name in model_names:
        if name in handlers:
            model_key = name
            break
    if model_key is None:
        # Try matching by title.
        for key, info in handlers.items():
            if info["title"] in model_names:
                model_key = key
                break
    if model_key is None:
        return JsonResponse(
            {"error": f"Модель '{model_names[0]}' недоступна. Доступные: {', '.join(handlers.keys())}"},
            status=400,
        )

    handler_info = handlers[model_key]
    handler = handler_info["handler"]
    model_title = handler_info["title"]

    message = log.message or ""
    if not message.strip():
        return JsonResponse({"error": "В логе пустое сообщение"}, status=400)

    client_id = f"resend-{log.pk}-{model_key}"

    start_time = timezone.now()
    new_log = AIRequestLog.objects.create(
        user=log.user,
        username=log.username,
        external_user_id=log.external_user_id,
        user_full_name=log.user_full_name,
        client_id=client_id,
        source=log.source,
        mode=log.mode,
        sent_at=start_time,
        model_names=[model_title],
        message=message,
        programming_language_id=log.programming_language_id,
        programming_language_name=log.programming_language_name,
        topic_id=log.topic_id,
        topic_name=log.topic_name,
        prompt_id=log.prompt_id,
        prompt_name=log.prompt_name,
        task_node_id=log.task_node_id,
        task_name=log.task_name,
    )

    try:
        response = async_to_sync(handler)(message, client_id)
        end_time = timezone.now()

        if isinstance(response, tuple):
            response_text = str(response[0] or "") if len(response) > 0 else ""
            tokens = response[1] if len(response) > 1 else 0
        else:
            response_text = str(response or "")
            tokens = 0

        response_text = response_text[:5000]

        if not response_text.strip():
            new_log.received_at = end_time
            new_log.duration_seconds = (end_time - start_time).total_seconds()
            new_log.response_text = response_text
            new_log.tokens = tokens or 0
            new_log.status = AIRequestLog.STATUS_ERROR
            new_log.error_message = "Модель вернула пустой ответ"
            new_log.save()
            return JsonResponse({
                "success": False,
                "error": "Модель вернула пустой ответ",
                "new_log_id": new_log.pk,
            })

        new_log.received_at = end_time
        new_log.duration_seconds = (end_time - start_time).total_seconds()
        new_log.response_text = response_text
        new_log.tokens = tokens or 0
        new_log.status = AIRequestLog.STATUS_SUCCESS
        new_log.save()

        return JsonResponse({
            "success": True,
            "new_log_id": new_log.pk,
            "response_preview": response_text[:200],
        })
    except Exception as exc:
        end_time = timezone.now()
        new_log.received_at = end_time
        new_log.duration_seconds = (end_time - start_time).total_seconds()
        new_log.status = AIRequestLog.STATUS_ERROR
        new_log.error_message = str(exc)[:2000]
        new_log.save()
        logger.exception("Resend request failed for log %s", log_id)
        return JsonResponse({
            "success": False,
            "error": str(exc)[:500],
            "new_log_id": new_log.pk,
        }, status=500)


def _resolve_dl_session_id(request) -> str:
    """Resolve the caller's DL session id from the session or DLSID cookie.

    Mirrors ``TaskAdmin.refresh_from_dl`` / ``get_task_info_view`` so the logs
    page can fetch a task statement from DL when no local ``Task`` cache exists.
    """
    session_id = (request.session.get("external_session_id") or "").strip()
    if not session_id:
        session_id = resolve_dl_session_id(request)
    return session_id


def admin_request_log_task_text_view(request):
    """Return the text of a task for the in-page «click task → show text» modal.

    GET ``node_id`` (int, required). Prefers the locally cached ``Task.statement``
    (populated by ``TaskAdmin.refresh_from_dl`` / ``ensure_task``); falls back to a
    live DL fetch via ``fetch_task_info`` using the caller's DLSID session. Never
    500s — DL errors map to JSON error responses.
    """
    if not can_access_logs(request):
        return JsonResponse({"ok": False, "error": "Access denied"}, status=403)

    try:
        node_id = int(request.GET.get("node_id", ""))
    except (ValueError, TypeError):
        return JsonResponse(
            {"ok": False, "error": "node_id обязателен и должен быть числом"},
            status=400,
        )

    # 1) Local cache first — fast, no DL dependency.
    task = Task.objects.filter(node_id=node_id).first()
    if task and (task.statement or "").strip():
        return JsonResponse({
            "ok": True,
            "name": task.name or str(node_id),
            "statement": task.statement,
            "source": "cache",
        })

    # 2) DL fallback — live fetch using the caller's session.
    session_id = _resolve_dl_session_id(request)
    if not session_id:
        return JsonResponse(
            {"ok": False, "error": "Нет DLSID — получить условие из DL невозможно"},
            status=503,
        )

    try:
        data = fetch_task_info(node_id, session_id=session_id, remove_html_tags=True)
    except (DLUnauthorizedError, DLForbiddenError, DLTaskNotFoundError,
            DLApiUnavailable, DLServerError) as exc:
        return dl_error_response(exc, extra={"ok": False})

    statement = (data.get("statement") or data.get("currentStatement") or "").strip()

    # DL's own HTML stripping sometimes yields an empty statement for tasks whose
    # condition is visible on the site (DL's stripper fails on the markup). Re-fetch
    # the raw response and strip HTML server-side. Mirrors get_task_info_view.
    if not statement:
        try:
            raw = fetch_task_info(node_id, session_id=session_id, remove_html_tags=None)
            raw_statement = (
                raw.get("statement") or raw.get("currentStatement") or ""
            ).strip()
            if raw_statement:
                statement = strip_tags(raw_statement).strip()
        except DLApiError:
            pass  # keep the original (empty) statement

    name = (data.get("name") or "").strip() or str(node_id)
    if not statement:
        return JsonResponse(
            {"ok": False, "error": "У задачи нет текста условия"},
            status=404,
        )
    return JsonResponse({
        "ok": True,
        "name": name,
        "statement": statement,
        "source": "dl",
    })


def _rerun_arm_batch(request, log):
    """Перезапуск batch-solve ARM-прогона под DLSID текущего пользователя.

    Извлекает run_id из ``log.message`` ("Batch solve run <uuid>"), находит
    ``AIModelTestRun``, восстанавливает параметры (node_ids, model_keys,
    file_extension, prompt_id, language) из результатов прогона и запускает
    новый batch через ``start_batch_solve_run`` с session_id текущего юзера.
    """
    from ..models import AIModelTestRun, AIModelTestResult, Prompt
    from ..arm_runner import start_batch_solve_run
    from ..services.task_registry import EXTENSION_TO_LANG, extension_to_language_ids

    if not can_access_logs(request):
        return JsonResponse({"error": "Access denied"}, status=403)

    # 1. Extract run_id from message.
    run_id_hex = _batch_run_id_from_log(log)
    if not run_id_hex:
        return JsonResponse({"error": "Не удалось извлечь run_id из лога"}, status=400)

    try:
        test_run = AIModelTestRun.objects.get(run_id=run_id_hex)
    except AIModelTestRun.DoesNotExist:
        return JsonResponse({"error": "Прогон ARM не найден в БД"}, status=404)

    # 2. Collect node_ids and model_keys from AIModelTestResult rows.
    results_qs = AIModelTestResult.objects.select_related("task").filter(run=test_run)
    if not results_qs.exists():
        return JsonResponse({"error": "В прогоне нет результатов — нечего перезапускать"}, status=400)

    node_ids = []
    seen_nodes = set()
    for r in results_qs:
        if r.task and r.task.node_id and r.task.node_id not in seen_nodes:
            seen_nodes.add(r.task.node_id)
            node_ids.append(r.task.node_id)

    model_keys = list(
        results_qs.values_list("model_key", flat=True).distinct()
    )

    if not node_ids:
        return JsonResponse({"error": "Не найдено задач в результатах прогона"}, status=400)
    if not model_keys:
        return JsonResponse({"error": "Не найдено моделей в результатах прогона"}, status=400)

    # 3. File extension from first result snapshot.
    file_extension = (results_qs.first().file_extension_snapshot or "").strip()
    if not file_extension:
        return JsonResponse({"error": "Не найдено расширение файла в результатах прогона"}, status=400)

    # 4. Prompt id from the run (or from the log).
    prompt_id = test_run.prompt_id or log.prompt_id

    # 5. Language name from extension.
    prog_lang_name = EXTENSION_TO_LANG.get(file_extension, "")
    prog_lang_ids = extension_to_language_ids(file_extension) if file_extension else set()
    prog_lang_id = next(iter(prog_lang_ids), None) if prog_lang_ids else None

    # 6. Prompt name + topic from Prompt.
    prompt_name = ""
    topic_id_log = None
    topic_name_log = ""
    if prompt_id:
        try:
            prompt_obj = Prompt.objects.select_related("topic").get(id=int(prompt_id))
            prompt_name = prompt_obj.prompt_name_ru or ""
            if prompt_obj.topic:
                topic_id_log = prompt_obj.topic_id
                topic_name_log = prompt_obj.topic.topic_name_ru or ""
        except (Prompt.DoesNotExist, ValueError):
            pass

    # 7. Session id from the caller (NOT from the original log).
    session_id = _resolve_dl_session_id(request)
    if not session_id:
        return JsonResponse(
            {"error": "Нет DLSID — требуется авторизация на dl.gsu.by."},
            status=400,
        )

    # 8. Launch the new batch.
    new_run_id, start_error = start_batch_solve_run(
        node_ids,
        model_keys,
        request.user.id,
        session_id,
        ui_language="Русский",
        dl_test=True,
        prompt_id=prompt_id,
        course_id=test_run.course_id,
        solve_file_extension=file_extension,
        solve_prog_lang_name=prog_lang_name,
        programming_language_id=prog_lang_id,
        programming_language_name=prog_lang_name,
        prompt_name=prompt_name,
        topic_id=topic_id_log,
        topic_name=topic_name_log,
    )
    if not new_run_id:
        return JsonResponse(
            {"error": start_error or "Не удалось запустить batch solve"},
            status=400,
        )

    from ..arm_runner import get_arm_run_snapshot
    return JsonResponse({
        "success": True,
        "run_id": new_run_id,
        "run": get_arm_run_snapshot(new_run_id),
        "message": f"Перезапущен: {len(node_ids)} задач × {len(model_keys)} моделей (расширение {file_extension})",
    })


@require_POST
def rerun_arm_batch_view(request, log_id):
    """Public entry point for rerun-arm URL — loads the log and delegates."""
    if not can_access_logs(request):
        return JsonResponse({"error": "Access denied"}, status=403)
    log, error = _get_scoped_log(request, log_id)
    if error is not None:
        return error
    return _rerun_arm_batch(request, log)
