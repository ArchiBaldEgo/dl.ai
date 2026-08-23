"""Логи запросов к AI-моделям: admin view и кастомная страница списка."""

import logging
import os
from datetime import datetime

from django.contrib import admin
from django.core.paginator import Paginator
from django.db.models import Q
from .site import ai_admin_site
from django.http import HttpResponseForbidden, JsonResponse
from django.template.response import TemplateResponse
from django.utils.html import strip_tags
from django.utils.http import urlencode
from django.views.decorators.http import require_POST

from ..constants import MOSCOW_TZ
from ..dl_api_client import (
    DLApiError,
    DLApiUnavailable,
    DLForbiddenError,
    DLServerError,
    DLTaskNotFoundError,
    DLUnauthorizedError,
    fetch_task_info,
)
from ..models import AIRequestLog, Task
from ..model_health import get_runtime_model_handlers
from .permissions import can_access_logs

logger = logging.getLogger(__name__)


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

    def has_module_permission(self, request):
        return can_access_logs(request)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return can_access_logs(request)

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
        """Return task name as a link to DL fullTaskviewer."""
        if obj.task_node_id:
            name = obj.task_name or str(obj.task_node_id)
            return f'<a href="https://dl.gsu.by/admin/fullTaskviewer.asp?nid={obj.task_node_id}" target="_blank" rel="noopener">{name}</a>'
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
    from django.utils import timezone
    from ..constants import MOSCOW_TZ

    local = timezone.localtime(value, MOSCOW_TZ)
    return local.strftime("%d.%m.%Y:%H:%M:%S")


def admin_request_logs_view(request):
    if not can_access_logs(request):
        return HttpResponseForbidden("Access denied")

    qs = AIRequestLog.objects.all()

    status = request.GET.get("status", "").strip()
    source = request.GET.get("source", "").strip()
    mode = request.GET.get("mode", "").strip()
    model = request.GET.get("model", "").strip()
    user_q = request.GET.get("user", "").strip()
    task_q = request.GET.get("task", "").strip()
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
        "status_choices": AIRequestLog.STATUS_CHOICES,
        "source_choices": AIRequestLog.SOURCE_CHOICES,
        "mode_choices": AIRequestLog.MODE_CHOICES,
        "model_choices": model_choices,
        "task_choices": task_choices,
        "filters_query": filters_query_str,
        "moscow_tz": MOSCOW_TZ,
        "filters": {
            "status": status,
            "source": source,
            "mode": mode,
            "model": model,
            "user": user_q,
            "task": task_q,
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
    report} для детали batch-solve лога — тот же формат, что потребляет JS
    мини-таблицы в /arm/solve/. Возвращает None, если прогон/результаты не
    найдены (тогда детал отрисуется как обычный текстовый лог)."""
    import re as _re
    from ..models import AIModelTestRun, AIModelTestResult
    from ..arm_runner import _batch_results_from_db, _build_batch_report

    m = _re.search(r"Batch solve run ([0-9a-f]{32})", log.message or "")
    if not m:
        return None
    run_id_hex = m.group(1)
    try:
        test_run = AIModelTestRun.objects.get(run_id=run_id_hex)
    except AIModelTestRun.DoesNotExist:
        return None

    results = _batch_results_from_db(test_run)
    report = _build_batch_report(results)

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
    }


def admin_request_log_detail_view(request, log_id):
    if not can_access_logs(request):
        return HttpResponseForbidden("Access denied")

    log = AIRequestLog.objects.get(pk=log_id)

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

    try:
        log = AIRequestLog.objects.get(pk=log_id)
    except AIRequestLog.DoesNotExist:
        return JsonResponse({"error": "Лог не найден"}, status=404)

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
        cookie_name = os.getenv("EXTERNAL_SESSION_COOKIE_NAME", "DLSID")
        session_id = (request.COOKIES.get(cookie_name, "") or "").strip()
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
    except DLUnauthorizedError:
        return JsonResponse({"ok": False, "error": "Authorization required"}, status=401)
    except DLForbiddenError:
        return JsonResponse({"ok": False, "error": "Access denied"}, status=403)
    except DLTaskNotFoundError:
        return JsonResponse({"ok": False, "error": "Задача не найдена"}, status=404)
    except DLApiUnavailable:
        return JsonResponse(
            {"ok": False, "error": "DL API временно недоступен"}, status=503
        )
    except DLServerError:
        return JsonResponse({"ok": False, "error": "Ошибка сервера DL"}, status=502)

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
    import re as _re
    from ..models import AIModelTestRun, AIModelTestResult, Prompt
    from ..arm_runner import start_batch_solve_run
    from ..services.task_registry import EXTENSION_TO_LANG, extension_to_language_ids

    if not can_access_logs(request):
        return JsonResponse({"error": "Access denied"}, status=403)

    # 1. Extract run_id from message.
    m = _re.search(r"Batch solve run ([0-9a-f]{32})", log.message or "")
    if not m:
        return JsonResponse({"error": "Не удалось извлечь run_id из лога"}, status=400)
    run_id_hex = m.group(1)

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
            prompt_name = prompt_obj.prompt_name or ""
            if prompt_obj.topic:
                topic_id_log = prompt_obj.topic_id
                topic_name_log = prompt_obj.topic.topic_name or ""
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
    try:
        log = AIRequestLog.objects.get(pk=log_id)
    except AIRequestLog.DoesNotExist:
        return JsonResponse({"error": "Лог не найден"}, status=404)
    return _rerun_arm_batch(request, log)
