import os

import tempfile
from django.contrib.auth import login
from django.shortcuts import redirect, render
from django.http import JsonResponse
from django.http import FileResponse, Http404, HttpResponseForbidden, HttpResponseNotFound
from django.db import ProgrammingError, models
from django.contrib.staticfiles import finders
from django.middleware import csrf
from functools import wraps
from django.views.decorators.csrf import csrf_exempt       
from django.views.decorators.http import require_http_methods  
from django.conf import settings
from .model_health import get_available_model_options
from .models import ProgrammingLanguage, Topic, Prompt, SharedPrompt, AIAppSettings, ExternalDLAccount
from .admin.permissions import external_id_matches_session
from .auth_backends import (
    ADMIN_EXTERNAL_AUTH_BACKEND,
    create_admin_user_with_password,
    ensure_prompt_developer_group,
    get_admin_user_by_external_id,
    get_external_user_id_from_request,
    normalize_external_user_id,
)
from .constants import PROMPT_DEVELOPER_GROUP
from .http_utils import safe_relative_url
from .i18n import get_language_instruction, get_localized_name, get_localized_text
from .querysets import prompt_queryset_for_user
from .serializers import (
    programming_language as serialize_programming_language,
    prompt as serialize_prompt,
    shared_prompt as serialize_shared_prompt,
    shared_prompt_with_dates as serialize_shared_prompt_with_dates,
    topic as serialize_topic,
)

_WEB_PRIORITY_MODELS = ("Web_DeepSeek", "Web_DeepSeek_Thinking")


def health_view(request):
    return JsonResponse({"ok": True})

try:
    import speech_recognition as sr
    SPEECH_RECOGNITION_AVAILABLE = True
except ImportError:
    SPEECH_RECOGNITION_AVAILABLE = False


_safe_relative_url = safe_relative_url


def prompt_developer_access_required(view_func):
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        user = getattr(request, "user", None)
        if not user or not user.is_authenticated:
            return HttpResponseForbidden("Prompt developer access required")
        if user.is_superuser or user.groups.filter(name=PROMPT_DEVELOPER_GROUP).exists():
            return view_func(request, *args, **kwargs)
        return HttpResponseForbidden("Prompt developer access required")

    return _wrapped


def _is_ai_app_enabled():
    try:
        return AIAppSettings.get_solo().is_enabled
    except ProgrammingError:
        return True


def _has_page_access(request):
    """Require a verified external user id and a matching Django session.

    The external user id can come from any of:
    * ``request.user_info`` (filled in by ``ExternalAuthMiddleware`` after
      a successful DLSID lookup against ``EXTERNAL_AUTH_API_URL``),
    * the ``uid`` / ``userId`` query parameter (set by the dl.gsu.by
      toolbar links), or
    * one of the recognized cookies (``userId`` / ``user_id`` / ``userid`` /
      ``DLID``).

    We additionally enforce that the session-bound Django user is the
    same person as the one holding the cookie / query parameter —
    otherwise a stale session would still grant access to /ai/...
    """
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return False
    if getattr(user, "is_active", True) is False:
        return False
    external_id = get_external_user_id_from_request(request)
    if not external_id:
        return False
    return external_id_matches_session(request)


def _render_ai_page(request, template_name):
    if not _has_page_access(request):
        return HttpResponseForbidden("Authentication required")
    if not _is_ai_app_enabled():
        return HttpResponseNotFound("AI app is disabled")
    available_models = get_available_model_options()
    if available_models:
        priority_map = {item["key"]: item for item in available_models}
        ordered_priority = [
            priority_map[key]
            for key in _WEB_PRIORITY_MODELS
            if key in priority_map
        ]
        rest = [item for item in available_models if item["key"] not in _WEB_PRIORITY_MODELS]
        available_models = ordered_priority + rest
    external_session_id = request.session.get('external_session_id')
    return render(request, template_name, {
        'available_models': available_models,
        'external_session_id': external_session_id,
    })


def set_password_view(request):
    """Allow an externally authenticated admin user to set a first password."""
    next_url = _safe_relative_url(
        request.POST.get("next") if request.method == "POST" else request.GET.get("next"),
        "/ai/admin/",
    )
    error_message = ""
    subtitle = "Для входа в админку введите пароль. Это однократная регистрация."
    target_user = None
    external_user_id = get_external_user_id_from_request(request)
    if not external_user_id:
        external_user_id = normalize_external_user_id(
            request.POST.get("external_user_id")
            or request.GET.get("uid")
            or request.GET.get("userId")
        )

    is_admin_registration = request.path.startswith("/ai/admin/set-password/") and bool(external_user_id)
    if is_admin_registration:
        target_user = get_admin_user_by_external_id(external_user_id)

    if request.method == "POST":
        new_password = request.POST.get("new_password") or ""
        new_password_confirm = request.POST.get("new_password_confirm") or ""

        if is_admin_registration and target_user and target_user.has_usable_password():
            ensure_prompt_developer_group(target_user)
            login(request, target_user, backend=ADMIN_EXTERNAL_AUTH_BACKEND)
            csrf.rotate_token(request)
            request.session["admin_fresh_auth"] = True
            return redirect(next_url)

        if not is_admin_registration:
            error_message = "Установка пароля вне админки больше не поддерживается."
        elif is_admin_registration and target_user and getattr(target_user, "is_active", True) is False:
            error_message = "Учётная запись заблокирована."
        elif is_admin_registration and target_user and target_user.has_usable_password():
            error_message = "Пользователь уже имеет пароль. Используйте обычный вход."
        elif new_password != new_password_confirm:
            error_message = "Пароли не совпадают."
        elif len(new_password) < 8:
            error_message = "Пароль должен быть не менее 8 символов."
        else:
            if target_user:
                target_user.set_password(new_password)
                target_user.save(update_fields=["password"])
                ensure_prompt_developer_group(target_user)
            else:
                target_user = create_admin_user_with_password(external_user_id, new_password)
            login(request, target_user, backend=ADMIN_EXTERNAL_AUTH_BACKEND)
            csrf.rotate_token(request)
            request.session["admin_fresh_auth"] = True
            return redirect(next_url)

        response = render(
            request,
            "ai/set-password.html",
            {
                "error_message": error_message,
                "next_url": next_url,
                "username": target_user.username if target_user else external_user_id,
                "subtitle": subtitle,
            },
        )
        response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response["Pragma"] = "no-cache"
        return response

    if is_admin_registration and target_user and target_user.has_usable_password():
        ensure_prompt_developer_group(target_user)
        login(request, target_user, backend=ADMIN_EXTERNAL_AUTH_BACKEND)
        csrf.rotate_token(request)
        request.session["admin_fresh_auth"] = True
        return redirect(next_url)

    response = render(
        request,
        "ai/set-password.html",
        {
            "error_message": error_message,
            "next_url": next_url,
            "username": target_user.username if target_user else external_user_id,
            "subtitle": subtitle,
        },
    )
    response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response["Pragma"] = "no-cache"
    return response


def chat_view(request):
    return _render_ai_page(request, 'ai/chat.html')


def decide_task_view(request):
    return _render_ai_page(request, 'ai/decide-task.html')


def find_error_view(request):
    return _render_ai_page(request, 'ai/find-error.html')


def get_languages(request):
    languages = [
        serialize_programming_language(lang)
        for lang in ProgrammingLanguage.objects.order_by('language_name')
    ]
    return JsonResponse(languages, safe=False)


def get_topics(request):
    ui_language = request.GET.get('ui_language', 'Русский')
    topics = [
        serialize_topic(topic, ui_language)
        for topic in Topic.objects.select_related("programming_language").order_by('topic_name')
    ]
    return JsonResponse(topics, safe=False)


def get_prompts(request):
    if not _has_page_access(request):
        return HttpResponseForbidden("Authentication required")

    ui_language = request.GET.get('ui_language', 'Русский')
    prompts = [
        serialize_prompt(p, ui_language)
        for p in Prompt.objects.select_related("topic", "topic__programming_language", "owner", "shared_prompt").order_by('prompt_name', 'id')
    ]
    return JsonResponse(prompts, safe=False)


def get_shared_prompts(request):
    """Возвращает общие (shared) препромпты с привязкой к языкам."""
    if not _has_page_access(request):
        return HttpResponseForbidden("Authentication required")

    ui_language = request.GET.get('ui_language', 'Русский')
    language_id = request.GET.get('language_id')
    qs = SharedPrompt.objects.prefetch_related('programming_languages')

    if language_id:
        # Фильтруем: либо общий препромпт привязан к этому языку, либо без привязки (для всех)
        qs = qs.filter(
            models.Q(programming_languages__id=language_id) | models.Q(programming_languages__isnull=True)
        ).distinct()

    shared = [
        serialize_shared_prompt_with_dates(sp, ui_language)
        for sp in qs
    ]
    return JsonResponse(shared, safe=False)


def get_problem_data(request):
    """Возвращает языки, темы, промпты и общие промпты одним запросом."""
    if not _has_page_access(request):
        return HttpResponseForbidden("Authentication required")

    ui_language = request.GET.get('ui_language', 'Русский')

    languages = [
        serialize_programming_language(lang)
        for lang in ProgrammingLanguage.objects.order_by('language_name')
    ]
    topics = [
        serialize_topic(topic, ui_language)
        for topic in Topic.objects.select_related("programming_language").order_by('topic_name')
    ]
    prompts = [
        serialize_prompt(p, ui_language)
        for p in Prompt.objects.select_related("topic", "topic__programming_language", "owner", "shared_prompt").order_by('prompt_name', 'id')
    ]
    shared_prompts = [
        serialize_shared_prompt(sp, ui_language)
        for sp in SharedPrompt.objects.prefetch_related('programming_languages')
    ]

    return JsonResponse({
        'languages': languages,
        'topics': topics,
        'prompts': prompts,
        'shared_prompts': shared_prompts,
    })


def asset_view(request, asset_path):
    asset_full_path = finders.find(asset_path)
    if not asset_full_path or not os.path.isfile(asset_full_path):
        raise Http404("Asset not found")

    return FileResponse(open(asset_full_path, "rb"))

@csrf_exempt
@require_http_methods(["POST"])
def transcribe_audio(request):
    audio_file = request.FILES.get('audio')
    if not audio_file:
        return JsonResponse({'success': False, 'error': 'No audio file provided'})
    
    language = request.POST.get('language', 'Russian')
    
    # Сохраняется временный файл
    with tempfile.NamedTemporaryFile(suffix='.webm', delete=False) as tmp:
        for chunk in audio_file.chunks():
            tmp.write(chunk)
        tmp_path = tmp.name

    try:
        # Конвертируется webm в wav (ffmpeg берём из pip-пакета imageio-ffmpeg, не из apt)
        from pydub import AudioSegment
        import imageio_ffmpeg
        AudioSegment.converter = imageio_ffmpeg.get_ffmpeg_exe()

        audio = AudioSegment.from_file(tmp_path, format='webm')

        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as wav_tmp:
            audio.export(wav_tmp.name, format='wav')
            wav_path = wav_tmp.name

        import speech_recognition as sr
        recognizer = sr.Recognizer()

        with sr.AudioFile(wav_path) as source:
            audio_data = recognizer.record(source)

        lang_map = {
            'Russian': 'ru-RU',
            'English': 'en-US',
            'French': 'fr-FR'
        }

        text = recognizer.recognize_google(audio_data, language=lang_map.get(language, 'en-US'))

        # Чистим временные файлы
        os.unlink(tmp_path)
        os.unlink(wav_path)

        return JsonResponse({'success': True, 'text': text})

    except sr.UnknownValueError:
        return JsonResponse({'success': False, 'error': 'Не удалось разобрать речь'})
    except sr.RequestError as e:
        return JsonResponse({'success': False, 'error': f'Ошибка сервиса распознавания: {e}'})
    except Exception as e:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        return JsonResponse({'success': False, 'error': str(e)})
