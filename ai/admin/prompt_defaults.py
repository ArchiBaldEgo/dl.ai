"""Инструмент «Препромпты по умолчанию» (только суперпользователь).

Привязка (язык программирования, тема, вид ARM) → препромпт: после выбора
темы на /arm/solve/ и /arm/find-error/ препромпт подтягивается автоматически
(см. ``ArmPromptBinding`` и JS авто-подстановку на ARM-страницах). Сам промпт
редактируется по обычным правилам ACL (PromptAdmin: владелец/редактор/
суперюзер) — привязка ссылается на него по FK и всегда тянет актуальный текст.
"""

from django.http import HttpResponseForbidden, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404
from django.template.response import TemplateResponse

from ..i18n import get_localized_name
from ..models import ArmPromptBinding, ProgrammingLanguage, Prompt, Topic
from ..serializers import arm_prompt_binding
from .permissions import is_superuser_user
from .site import ai_admin_site


def _binding_rows():
    """Строки таблицы привязок: локализованные имена + serialized-данные."""
    bindings = (
        ArmPromptBinding.objects.select_related(
            "programming_language", "topic", "prompt",
        ).order_by("programming_language__language_name", "topic__topic_name", "mode")
    )
    rows = []
    for b in bindings:
        rows.append({
            **arm_prompt_binding(b),
            "language_name": b.programming_language.language_name,
            "topic_name": get_localized_topic_name(b.topic),
            "mode_label": b.get_mode_display(),
            "prompt_name": str(b.prompt),
        })
    return rows


def get_localized_topic_name(topic):
    """Локализованное имя темы (русский дефолт админки)."""
    return get_localized_name(topic, "Русский", "topic_name")


def _form_data():
    """Данные для формы: языки, темы (с language_id), промптs (с topic_id)."""
    languages = [
        {"id": lang.id, "name": lang.language_name}
        for lang in ProgrammingLanguage.objects.order_by("language_name")
    ]
    topics = [
        {"id": t.id, "name": get_localized_topic_name(t), "language_id": t.programming_language_id}
        for t in Topic.objects.select_related("programming_language").order_by("topic_name")
    ]
    prompts = [
        {"id": p.id, "name": str(p), "topic_id": p.topic_id}
        for p in Prompt.objects.select_related("topic").order_by("prompt_name", "id")
    ]
    return languages, topics, prompts


def admin_prompt_defaults_view(request):
    if not is_superuser_user(request.user):
        return HttpResponseForbidden("Access denied")

    if request.method == "POST":
        return _handle_save_or_delete(request)

    languages, topics, prompts = _form_data()

    # Редактирование существующей привязки: ?edit=<id> пред-заполняет форму.
    editing = None
    edit_id = (request.GET.get("edit") or "").strip()
    if edit_id.isdigit():
        binding = ArmPromptBinding.objects.filter(pk=int(edit_id)).first()
        if binding:
            editing = arm_prompt_binding(binding)

    context = {
        **ai_admin_site.each_context(request),
        "title": "AI: Препромпты по умолчанию",
        "bindings": _binding_rows(),
        "languages": languages,
        "topics": topics,
        "prompts": prompts,
        "editing": editing,
        "mode_choices": [
            {"value": value, "label": label}
            for value, label in ArmPromptBinding.ARM_MODE_CHOICES
        ],
    }
    return TemplateResponse(request, "admin/ai/prompt_defaults.html", context)


def _handle_save_or_delete(request):
    """POST-эндпоинт save/delete — JSON-ответы (AJAX-хелпер parseJsonResponse
    на фронте ждёт именно JSON, редирект логина ломает JSON.parse)."""
    from django.db import IntegrityError

    action = request.POST.get("action") or ""
    if action not in ("save", "delete"):
        return JsonResponse({"ok": False, "error": "Неизвестное действие"}, status=400)

    if action == "delete":
        binding_id = (request.POST.get("binding_id") or "").strip()
        if not binding_id.isdigit():
            return JsonResponse({"ok": False, "error": "Привязка не найдена"}, status=404)
        deleted, _ = ArmPromptBinding.objects.filter(pk=int(binding_id)).delete()
        if not deleted:
            return JsonResponse({"ok": False, "error": "Привязка не найдена"}, status=404)
        return JsonResponse({"ok": True, "message": "Привязка удалена"})

    # save: upsert привязки (язык, тема, вид) → промпт.
    language_id = (request.POST.get("language_id") or "").strip()
    topic_id = (request.POST.get("topic_id") or "").strip()
    mode = (request.POST.get("mode") or "").strip()
    prompt_id = (request.POST.get("prompt_id") or "").strip()
    if not (language_id.isdigit() and topic_id.isdigit() and prompt_id.isdigit()):
        return JsonResponse(
            {"ok": False, "error": "Выберите язык, тему и препромпт"}, status=400,
        )
    if mode not in dict(ArmPromptBinding.ARM_MODE_CHOICES):
        return JsonResponse({"ok": False, "error": "Выберите вид ARM"}, status=400)

    topic = Topic.objects.filter(pk=int(topic_id)).select_related("programming_language").first()
    prompt = Prompt.objects.filter(pk=int(prompt_id)).first()
    if topic is None or prompt is None:
        return JsonResponse({"ok": False, "error": "Тема или препромпт не найдены"}, status=404)
    if topic.programming_language_id != int(language_id):
        return JsonResponse(
            {"ok": False, "error": "Тема не принадлежит выбранному языку программирования"},
            status=400,
        )
    if prompt.topic_id != topic.id:
        return JsonResponse(
            {"ok": False, "error": "Препромпт привязан к другой теме"}, status=400,
        )

    try:
        binding, created = ArmPromptBinding.objects.update_or_create(
            programming_language_id=topic.programming_language_id,
            topic_id=topic.id,
            mode=mode,
            defaults={"prompt_id": prompt.id},
        )
    except IntegrityError:
        return JsonResponse({"ok": False, "error": "Привязка уже существует"}, status=400)

    message = "Привязка создана" if created else "Привязка обновлена"
    return JsonResponse({"ok": True, "message": message, "binding": arm_prompt_binding(binding)})