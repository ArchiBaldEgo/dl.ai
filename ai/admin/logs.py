"""Логи запросов к AI-моделям: admin view и кастомная страница списка."""

import logging

from django.contrib import admin
from django.core.paginator import Paginator
from django.db.models import Q
from .site import ai_admin_site
from django.http import HttpResponseForbidden, JsonResponse
from django.template.response import TemplateResponse
from django.utils.http import urlencode
from django.views.decorators.http import require_POST

from ..constants import MOSCOW_TZ
from ..models import AIRequestLog
from ..model_health import get_runtime_model_handlers
from .permissions import can_access_logs

logger = logging.getLogger(__name__)


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
    date_from = request.GET.get("date_from", "").strip()
    date_to = request.GET.get("date_to", "").strip()

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

    filters_query = request.GET.copy()
    filters_query.pop("page", None)
    filters_query_str = urlencode(filters_query)
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


def admin_request_log_detail_view(request, log_id):
    if not can_access_logs(request):
        return HttpResponseForbidden("Access denied")

    log = AIRequestLog.objects.get(pk=log_id)
    context = {
        **ai_admin_site.each_context(request),
        "title": "DL.AI: Детали запроса",
        "log": log,
        "moscow_tz": MOSCOW_TZ,
    }
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
