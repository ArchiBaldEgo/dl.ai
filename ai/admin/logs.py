"""AI request logs admin and custom list view."""

from django.contrib import admin
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpResponseForbidden
from django.template.response import TemplateResponse
from django.utils.http import urlencode

from ..models import AIRequestLog
from .permissions import can_access_logs


class AIRequestLogAdmin(admin.ModelAdmin):
    list_display = (
        "sent_at_display",
        "received_at_display",
        "user_full_name",
        "external_user_id",
        "model_names_display",
        "programming_language_name",
        "topic_name_display",
        "prompt_name",
        "status",
        "source",
        "duration_seconds_display",
    )
    list_filter = ("status", "source", "programming_language_name", "sent_at")
    search_fields = (
        "external_user_id",
        "user_full_name",
        "message",
        "programming_language_name",
        "topic_name",
        "prompt_name",
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

    def model_names_display(self, obj):
        return ", ".join(obj.model_names or [])
    model_names_display.short_description = "Модели"

    def programming_language_name(self, obj):
        return obj.programming_language_name or "—"
    programming_language_name.short_description = "Язык программирования"

    def topic_name_display(self, obj):
        return obj.topic_name or "—"
    topic_name_display.short_description = "Тема"

    def prompt_name(self, obj):
        return obj.prompt_name or "—"
    prompt_name.short_description = "Препромпт"

    def duration_seconds_display(self, obj):
        if obj.duration_seconds is None:
            return "—"
        return str(round(obj.duration_seconds))
    duration_seconds_display.short_description = "Длительность, с"


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
    model = request.GET.get("model", "").strip()
    user_q = request.GET.get("user", "").strip()
    date_from = request.GET.get("date_from", "").strip()
    date_to = request.GET.get("date_to", "").strip()

    status_values = dict(AIRequestLog.STATUS_CHOICES)
    source_values = dict(AIRequestLog.SOURCE_CHOICES)

    if status in status_values:
        qs = qs.filter(status=status)
    if source in source_values:
        qs = qs.filter(source=source)
    if model:
        qs = qs.filter(model_names__contains=[model])
    if user_q:
        qs = qs.filter(
            Q(user_full_name__icontains=user_q)
            | Q(username__icontains=user_q)
            | Q(external_user_id__icontains=user_q)
        )
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

    context = {
        **admin.site.each_context(request),
        "title": "DL.AI: Логи запросов",
        "page_obj": page_obj,
        "status_choices": AIRequestLog.STATUS_CHOICES,
        "source_choices": AIRequestLog.SOURCE_CHOICES,
        "filters_query": filters_query_str,
        "filters": {
            "status": status,
            "source": source,
            "model": model,
            "user": user_q,
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
        **admin.site.each_context(request),
        "title": "DL.AI: Детали запроса",
        "log": log,
    }
    return TemplateResponse(request, "admin/ai/airequestlog_detail.html", context)
