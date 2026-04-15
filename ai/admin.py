from django.contrib import admin
from django.contrib.admin.forms import AdminAuthenticationForm
from django import forms
from django.contrib.admin.views.decorators import staff_member_required
from django.http import HttpResponseForbidden
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import path
from django.utils.html import strip_tags
from asgiref.sync import async_to_sync
import time

from .models import ProgrammingLanguage, Topic, Prompt, AIAppSettings


MODEL_SPECS = {
    "DeepSeek_R1_Distill_Llama_70B": {
        "title": "DeepSeek-R1-Distill-Llama-70B",
        "handler_name": "ask_DeepSeek_R1_Distill_Llama_70B_async",
    },
    "DeepSeek_R1": {
        "title": "DeepSeek-R1",
        "handler_name": "ask_DeepSeek_R1_async",
    },
    "Meta_Llama_3_1_70B_Instruct": {
        "title": "Meta-Llama-3.1-70B-Instruct",
        "handler_name": "ask_Meta_Llama_3_1_70B_Instruct_async",
    },
    "Mixtral_8x22b": {
        "title": "Mixtral-8x22b",
        "handler_name": "ask_Mixtral_8x22b_async",
    },
    "Gpt_oss_120b": {
        "title": "Gpt_oss_120b",
        "handler_name": "ask_Gpt_oss_120b_async",
    },
    "Web_DeepSeek": {
        "title": "Web DeepSeek",
        "handler_name": "ask_Web_DeepSeek_async",
    },
    "Web_DeepSeek_Thinking": {
        "title": "Web DeepSeek Thinking",
        "handler_name": "ask_Web_DeepSeek_Thinking_async",
    },
}


def _load_model_handlers():
    try:
        from . import utils as ai_utils
    except Exception:
        return {}

    handlers = {}
    for key, spec in MODEL_SPECS.items():
        handler = getattr(ai_utils, spec["handler_name"], None)
        if handler:
            handlers[key] = {
                "title": spec["title"],
                "handler": handler,
            }
    return handlers


def _can_access_arm(request):
    if not request.user.is_authenticated:
        return False
    if request.user.is_superuser:
        return True
    return request.user.groups.filter(name="tester").exists()


class TesterOrStaffAdminAuthenticationForm(AdminAuthenticationForm):
    def confirm_login_allowed(self, user):
        if not user.is_active:
            raise forms.ValidationError(
                self.error_messages["inactive"],
                code="inactive",
            )

        if user.is_superuser or user.is_staff or user.groups.filter(name="tester").exists():
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


@staff_member_required
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

    if request.method == "POST":
        model_handlers = _load_model_handlers()
        selected_models = request.POST.getlist("models")
        selected_language_ui = request.POST.get("interface_language", "Русский")
        selected_prog_lng = request.POST.get("programming_language", "")
        selected_topic = request.POST.get("topic", "")
        selected_prompt = request.POST.get("prompt", "")
        task_text = (request.POST.get("task_text") or "").strip()
        code_text = (request.POST.get("code_text") or "").strip()

        if not selected_models:
            error_message = "Выберите хотя бы одну модель"
        elif not task_text and not code_text:
            error_message = "Заполните условие задачи или код"
        else:
            prog_lng_name = ProgrammingLanguage.objects.filter(id=selected_prog_lng).values_list(
                "language_name", flat=True
            ).first() or "Python"
            prompt_text = Prompt.objects.filter(id=selected_prompt).values_list(
                "prompt_text", flat=True
            ).first() or ""

            message = _build_find_error_message(
                task_text=task_text,
                code_text=code_text,
                prog_lang_name=prog_lng_name,
                prompt_text=prompt_text,
                ui_language=selected_language_ui,
            )

            success_count = 0
            total_tokens = 0

            for model_key in selected_models:
                model_info = model_handlers.get(model_key)
                if not model_info:
                    continue

                started = time.perf_counter()
                try:
                    response = async_to_sync(model_info["handler"])(
                        message,
                        f"admin-{request.user.id}-{model_key}",
                    )
                    elapsed = round(time.perf_counter() - started, 2)

                    if isinstance(response, tuple):
                        response_text = response[0] if len(response) > 0 else ""
                        tokens = int(response[1]) if len(response) > 1 and str(response[1]).isdigit() else 0
                    else:
                        response_text = str(response)
                        tokens = 0

                    cleaned_text = strip_tags(response_text or "")
                    short_response = cleaned_text[:300] + ("..." if len(cleaned_text) > 300 else "")
                    is_ok = bool(cleaned_text) and "ошибка" not in cleaned_text.lower()[:25]
                    if is_ok:
                        success_count += 1
                    total_tokens += tokens

                    results.append(
                        {
                            "model_key": model_key,
                            "model_title": model_info["title"],
                            "duration": elapsed,
                            "tokens": tokens,
                            "short_response": short_response,
                            "status": "ok" if is_ok else "error",
                            "raw_response": cleaned_text,
                        }
                    )
                except Exception as exc:
                    elapsed = round(time.perf_counter() - started, 2)
                    results.append(
                        {
                            "model_key": model_key,
                            "model_title": model_info["title"],
                            "duration": elapsed,
                            "tokens": 0,
                            "short_response": f"Ошибка вызова модели: {exc}",
                            "status": "error",
                            "raw_response": "",
                        }
                    )

            if results:
                fastest = min(results, key=lambda item: item["duration"])
                report = {
                    "models_total": len(results),
                    "success_count": success_count,
                    "error_count": len(results) - success_count,
                    "tokens_total": total_tokens,
                    "fastest_model": fastest["model_title"],
                    "fastest_duration": fastest["duration"],
                }

    context = {
        **admin.site.each_context(request),
        "title": "ARM: В чем ошибка",
        "languages": languages,
        "topics": topics,
        "prompts": prompts,
        "model_options": [
            {"key": key, "title": value["title"]} for key, value in MODEL_SPECS.items()
        ],
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
    }
    return TemplateResponse(request, "admin/ai/arm_find_error.html", context)


_default_get_urls = admin.site.get_urls


def _custom_admin_urls():
    custom_urls = [
        path(
            "arm/find-error/",
            admin.site.admin_view(admin_arm_find_error_view),
            name="ai_arm_find_error",
        ),
    ]
    return custom_urls + _default_get_urls()


admin.site.get_urls = _custom_admin_urls


def _custom_has_permission(request):
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
        if _can_access_arm(request) and not request.user.is_superuser:
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
    context["arm_find_error_url"] = "/ai/admin/arm/find-error/"
    return context


admin.site.each_context = _custom_each_context

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
