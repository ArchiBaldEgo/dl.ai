from django.contrib import admin
from django.contrib.admin.forms import AdminAuthenticationForm
from django import forms
from django.http import HttpResponseForbidden, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import path
from django.utils import timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

from .arm_runner import get_arm_run_snapshot, start_arm_sequential_run
from .model_health import (
    get_available_model_options,
    get_model_status_rows,
    get_health_window_date,
    is_model_health_refresh_running,
    trigger_model_health_refresh_async,
)
from .models import ProgrammingLanguage, Topic, Prompt, AIAppSettings

MOSCOW_TZ = ZoneInfo("Europe/Moscow")


def _safe_relative_url(candidate, fallback):
    value = (candidate or "").strip()
    if value.startswith("/") and not value.startswith("//"):
        return value
    return fallback


def _is_tester_user(request):
    if not request.user.is_authenticated:
        return False
    return request.user.groups.filter(name="tester").exists()


def _can_access_arm(request):
    if not request.user.is_authenticated:
        return False
    if request.user.is_superuser or request.user.is_staff:
        return True
    return _is_tester_user(request)


def _can_access_model_status(request):
    if not request.user.is_authenticated:
        return False
    return request.user.is_superuser or request.user.is_staff


class TesterOrStaffAdminAuthenticationForm(AdminAuthenticationForm):
    def confirm_login_allowed(self, user):
        if not user.is_active:
            raise forms.ValidationError(
                self.error_messages["inactive"],
                code="inactive",
            )

        if user.is_superuser or user.is_staff or user.groups.filter(name="tester").exists():
            if getattr(self, "request", None) is not None:
                self.request.session["admin_fresh_auth"] = True
            return

        raise forms.ValidationError(
            "Please enter the correct username and password for a staff or tester account.",
            code="invalid_login",
        )


def _language_instruction(language_name):
    if language_name == "Русский":
        return ". Разговаривай со мной только по-русски"
    if language_name == "Français":
        return ". Communiquez avec moi uniquement en français"
    if language_name == "English":
        return ". Communicate with me only in English"
    return ""


def _build_find_error_message(task_text, code_text, prog_lang_name, prompt_text, ui_language):
    message = (
        "У меня есть задача по программированию, я написал для нее код на языке "
        f"{prog_lang_name}, код не работает, найди пожалуйста ошибку. "
        f"Задача: {task_text}. Код: {code_text}."
    )
    if prompt_text:
        message += f". Препромпт: {prompt_text}"

    message += _language_instruction(ui_language)
    return message


def _collect_arm_form_state(request):
    return {
        "selected_models": request.POST.getlist("models"),
        "selected_language_ui": request.POST.get("interface_language", "Русский"),
        "selected_prog_lng": request.POST.get("programming_language", ""),
        "selected_topic": request.POST.get("topic", ""),
        "selected_prompt": request.POST.get("prompt", ""),
        "task_text": (request.POST.get("task_text") or "").strip(),
        "code_text": (request.POST.get("code_text") or "").strip(),
    }


def _prepare_arm_run_payload(form_state):
    selected_models = form_state["selected_models"]
    task_text = form_state["task_text"]
    code_text = form_state["code_text"]

    if not selected_models:
        return None, "Выберите хотя бы одну модель"

    if not task_text and not code_text:
        return None, "Заполните условие задачи или код"

    prog_lng_name = ProgrammingLanguage.objects.filter(
        id=form_state["selected_prog_lng"]
    ).values_list("language_name", flat=True).first() or "Python"

    prompt_text = Prompt.objects.filter(
        id=form_state["selected_prompt"]
    ).values_list("prompt_text", flat=True).first() or ""

    message = _build_find_error_message(
        task_text=task_text,
        code_text=code_text,
        prog_lang_name=prog_lng_name,
        prompt_text=prompt_text,
        ui_language=form_state["selected_language_ui"],
    )

    return {
        "selected_models": selected_models,
        "message": message,
    }, ""


def admin_arm_find_error_view(request):
    if not _can_access_arm(request):
        return HttpResponseForbidden("Access denied")

    languages = list(ProgrammingLanguage.objects.all().values("id", "language_name"))
    topics = list(Topic.objects.all().values("id", "topic_name", "programming_language_id"))
    prompts = list(Prompt.objects.all().values("id", "prompt_name", "prompt_text", "topic_id"))

    selected_models = []
    selected_language_ui = "Русский"
    selected_prog_lng = ""
    selected_topic = ""
    selected_prompt = ""
    task_text = ""
    code_text = ""
    results = []
    report = None
    error_message = ""
    active_run_id = (request.GET.get("run_id") or "").strip()
    active_run_snapshot = None

    if request.method == "POST":
        form_state = _collect_arm_form_state(request)
        selected_models = form_state["selected_models"]
        selected_language_ui = form_state["selected_language_ui"]
        selected_prog_lng = form_state["selected_prog_lng"]
        selected_topic = form_state["selected_topic"]
        selected_prompt = form_state["selected_prompt"]
        task_text = form_state["task_text"]
        code_text = form_state["code_text"]

        run_payload, error_message = _prepare_arm_run_payload(form_state)
        if not error_message:
            run_id, start_error = start_arm_sequential_run(
                run_payload["message"],
                run_payload["selected_models"],
                request.user.id,
            )

            if run_id:
                return redirect(f"/ai/admin/arm/find-error/?run_id={run_id}")

            error_message = start_error or "Не удалось запустить ARM процесс"

    if active_run_id:
        active_run_snapshot = get_arm_run_snapshot(active_run_id)
        if active_run_snapshot:
            results = active_run_snapshot.get("results") or []
            report = active_run_snapshot.get("report")
            if active_run_snapshot.get("status") == "failed":
                error_message = active_run_snapshot.get("error_message") or "ARM процесс завершился с ошибкой"
        else:
            error_message = "ARM процесс не найден или уже завершен"

    arm_back_url = _safe_relative_url(request.session.get("ai_testpanel_back_url"), "/")
    context = {
        **admin.site.each_context(request),
        "title": "ARM: В чем ошибка",
        "health_window_date": get_health_window_date().strftime("%d.%m.%Y"),
        "arm_back_url": arm_back_url,
        "languages": languages,
        "topics": topics,
        "prompts": prompts,
        "model_options": get_available_model_options(),
        "selected_models": selected_models,
        "selected_language_ui": selected_language_ui,
        "selected_prog_lng": selected_prog_lng,
        "selected_topic": selected_topic,
        "selected_prompt": selected_prompt,
        "task_text": task_text,
        "code_text": code_text,
        "results": results,
        "report": report,
        "error_message": error_message,
        "arm_find_error_start_url": "/ai/admin/arm/find-error/start/",
        "arm_find_error_status_url": "/ai/admin/arm/find-error/status/",
        "active_run_id": active_run_id,
        "active_run_snapshot": active_run_snapshot or {},
    }
    return TemplateResponse(request, "admin/ai/arm_find_error.html", context)


def admin_arm_find_error_start_view(request):
    if not _can_access_arm(request):
        return HttpResponseForbidden("Access denied")

    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    form_state = _collect_arm_form_state(request)
    run_payload, error_message = _prepare_arm_run_payload(form_state)
    if error_message:
        return JsonResponse({"ok": False, "message": error_message}, status=400)

    run_id, start_error = start_arm_sequential_run(
        run_payload["message"],
        run_payload["selected_models"],
        request.user.id,
    )
    if not run_id:
        return JsonResponse(
            {
                "ok": False,
                "message": start_error or "Не удалось запустить ARM процесс",
            },
            status=400,
        )

    return JsonResponse(
        {
            "ok": True,
            "run_id": run_id,
            "run": get_arm_run_snapshot(run_id),
        }
    )


def admin_arm_find_error_status_view(request):
    if not _can_access_arm(request):
        return HttpResponseForbidden("Access denied")

    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])

    run_id = (request.GET.get("run_id") or "").strip()
    if not run_id:
        return JsonResponse({"ok": False, "message": "run_id is required"}, status=400)

    run_snapshot = get_arm_run_snapshot(run_id)
    if not run_snapshot:
        return JsonResponse(
            {
                "ok": False,
                "message": "ARM процесс не найден или уже завершен",
            },
            status=404,
        )

    return JsonResponse({"ok": True, "run": run_snapshot})


def admin_model_status_view(request):
    if not _can_access_model_status(request):
        return HttpResponseForbidden("Access denied")

    refresh_message = ""
    refresh_error = ""

    if request.method == "POST" and request.POST.get("action") == "refresh_models":
        try:
            if trigger_model_health_refresh_async():
                refresh_message = (
                    "Обновление моделей запущено в фоне. "
                    "Окно 04:00 МСК не изменяется."
                )
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


def _serialize_model_status_rows_for_api(rows):
    serialized = []

    for row in rows:
        checked_at = row.get("checked_at")
        checked_at_msk = ""
        if checked_at:
            checked_at_msk = timezone.localtime(checked_at, MOSCOW_TZ).strftime("%d.%m.%Y %H:%M:%S")

        window_date = row.get("window_date")

        serialized.append(
            {
                "key": row.get("key") or "",
                "title": row.get("title") or "",
                "is_active": bool(row.get("is_active")),
                "status_label": row.get("status_label") or "",
                "window_date": window_date.isoformat() if window_date else "",
                "checked_at_msk": checked_at_msk,
                "is_current_window": bool(row.get("is_current_window")),
            }
        )

    return serialized


def admin_model_status_state_view(request):
    if not _can_access_model_status(request):
        return HttpResponseForbidden("Access denied")

    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])

    rows = get_model_status_rows()
    return JsonResponse(
        {
            "ok": True,
            "health_window_date": get_health_window_date().strftime("%d.%m.%Y"),
            "refresh_in_progress": is_model_health_refresh_running(),
            "model_status_rows": _serialize_model_status_rows_for_api(rows),
        }
    )


def admin_model_status_refresh_view(request):
    if not _can_access_arm(request):
        return HttpResponseForbidden("Access denied")

    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    try:
        started = trigger_model_health_refresh_async()
    except Exception as exc:
        return JsonResponse(
            {
                "ok": False,
                "message": f"Не удалось запустить обновление моделей: {exc}",
            },
            status=500,
        )

    if started:
        message = "Обновление моделей запущено в фоне. Окно 04:00 МСК не изменяется."
    else:
        message = "Обновление уже выполняется. Дождитесь завершения."

    return JsonResponse(
        {
            "ok": True,
            "message": message,
            "refresh_in_progress": is_model_health_refresh_running(),
        }
    )


_default_get_urls = admin.site.get_urls


def _custom_admin_urls():
    custom_urls = [
        path(
            "arm/find-error/start/",
            admin.site.admin_view(admin_arm_find_error_start_view),
            name="ai_arm_find_error_start",
        ),
        path(
            "arm/find-error/status/",
            admin.site.admin_view(admin_arm_find_error_status_view),
            name="ai_arm_find_error_status",
        ),
        path(
            "arm/models/refresh/",
            admin.site.admin_view(admin_model_status_refresh_view),
            name="ai_arm_model_status_refresh",
        ),
        path(
            "arm/models/state/",
            admin.site.admin_view(admin_model_status_state_view),
            name="ai_arm_model_status_state",
        ),
        path(
            "arm/models/",
            admin.site.admin_view(admin_model_status_view),
            name="ai_arm_model_status",
        ),
        path(
            "arm/find-error/",
            admin.site.admin_view(admin_arm_find_error_view),
            name="ai_arm_find_error",
        ),
    ]
    return custom_urls + _default_get_urls()


admin.site.get_urls = _custom_admin_urls


def _custom_has_permission(request):
    # Prevent login view from auto-redirecting authenticated users to index.
    # This avoids /admin -> /admin/login ping-pong when fresh-auth marker is missing.
    if request.path.startswith("/ai/admin/login/"):
        if request.method != "POST":
            request.session.pop("admin_fresh_auth", None)
        return False

    user = getattr(request, "user", None)
    if not user or not user.is_authenticated or not user.is_active:
        return False
    if user.is_superuser or user.is_staff:
        return True
    return user.groups.filter(name="tester").exists()


admin.site.has_permission = _custom_has_permission
admin.site.login_form = TesterOrStaffAdminAuthenticationForm


_default_admin_view = admin.site.admin_view


def _custom_admin_view(view, cacheable=False):
    wrapped_view = _default_admin_view(view, cacheable)

    def inner(request, *args, **kwargs):
        if request.user.is_authenticated and not request.session.get("admin_fresh_auth"):
            next_path = quote(request.get_full_path(), safe="/?=&")
            return redirect(f"/ai/admin/login/?next={next_path}")

        if _is_tester_user(request) and not request.user.is_superuser:
            arm_path = "/ai/admin/arm/find-error/"
            if not request.path.startswith(arm_path):
                return redirect(arm_path)
        return wrapped_view(request, *args, **kwargs)

    return inner


admin.site.admin_view = _custom_admin_view


_default_each_context = admin.site.each_context


def _custom_each_context(request):
    context = _default_each_context(request)
    context["show_arm_link"] = _can_access_arm(request)
    context["show_model_status_link"] = _can_access_model_status(request)
    context["arm_find_error_url"] = "/ai/admin/arm/find-error/"
    context["arm_model_status_url"] = "/ai/admin/arm/models/"
    context["arm_model_status_refresh_url"] = "/ai/admin/arm/models/refresh/"
    context["arm_model_status_state_url"] = "/ai/admin/arm/models/state/"
    return context


admin.site.each_context = _custom_each_context
admin.site.index_template = "admin/ai/index.html"
admin.site.app_index_template = "admin/ai/app_index.html"

# Форма для Prompt с улучшенным Textarea
class PromptForm(forms.ModelForm):
    class Meta:
        model = Prompt
        widgets = {
            'prompt_text': forms.Textarea(attrs={
                'rows': 25,
                'style': 'width: 95%; font-family: monospace; line-height: 1.4; white-space: pre-wrap;'
            }),
        }
        fields = '__all__'

# Inline для Prompt
class PromptInline(admin.TabularInline):
    model = Prompt
    form = PromptForm
    extra = 1
    fields = ('prompt_name',)  # Показываем только нужные поля
    classes = ('collapse',)  # Делаем сворачиваемым

# Inline для Topic (исправлено: было "ininlines")
class TopicInline(admin.TabularInline):
    model = Topic
    extra = 1
    fk_name = 'programming_language'
    show_change_link = True  # Добавляем ссылку на изменение

class ProgrammingLanguageAdmin(admin.ModelAdmin):
    inlines = [TopicInline]
    list_display = ('language_name',)
    search_fields = ('language_name',)

class TopicAdmin(admin.ModelAdmin):
    inlines = [PromptInline]
    list_display = ('topic_name', 'programming_language')
    list_filter = ('programming_language',)
    search_fields = ('topic_name',)
    raw_id_fields = ('programming_language',)  # Для удобства при многих языках

class PromptAdmin(admin.ModelAdmin):
    form = PromptForm
    list_display = ('prompt_name', 'topic', 'short_prompt_text')
    list_filter = ('topic__programming_language', 'topic')
    search_fields = ('prompt_name', 'prompt_text')
    
    def short_prompt_text(self, obj):
        return f"{obj.prompt_text[:100]}..." if len(obj.prompt_text) > 100 else obj.prompt_text
    short_prompt_text.short_description = "Prompt Text"


@admin.register(AIAppSettings)
class AIAppSettingsAdmin(admin.ModelAdmin):
    list_display = ("is_enabled", "updated_at")

    def has_add_permission(self, request):
        if AIAppSettings.objects.exists():
            return False
        return super().has_add_permission(request)

    def has_delete_permission(self, request, obj=None):
        return False

# Регистрация
admin.site.register(ProgrammingLanguage, ProgrammingLanguageAdmin)
admin.site.register(Topic, TopicAdmin)
admin.site.register(Prompt, PromptAdmin)
