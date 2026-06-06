import os

import tempfile
from django.contrib.auth import authenticate, login, logout
from django.shortcuts import redirect, render
from django.http import JsonResponse
from django.http import FileResponse, Http404, HttpResponseForbidden, HttpResponseNotFound
from django.db import ProgrammingError
from django.contrib.staticfiles import finders
from django.middleware import csrf
from functools import wraps
from django.views.decorators.csrf import csrf_exempt       
from django.views.decorators.http import require_http_methods  
from django.conf import settings
from .model_health import get_available_model_options
from .models import ProgrammingLanguage, Topic, Prompt, AIAppSettings, ExternalDLAccount
from .auth_backends import (
    ADMIN_EXTERNAL_AUTH_BACKEND,
    create_admin_user_with_password,
    ensure_prompt_developer_group,
    get_admin_user_by_external_id,
    get_external_user_id_from_request,
    normalize_external_user_id,
)
from .constants import PROMPT_DEVELOPER_GROUP

_WEB_PRIORITY_MODELS = ("Web_DeepSeek", "Web_DeepSeek_Thinking")


def health_view(request):
    return JsonResponse({"ok": True})

try:
    import speech_recognition as sr
    SPEECH_RECOGNITION_AVAILABLE = True
except ImportError:
    SPEECH_RECOGNITION_AVAILABLE = False

def _safe_relative_url(candidate, fallback):
    value = (candidate or "").strip()
    if value.startswith("/") and not value.startswith("//"):
        return value
    return fallback


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
    user = getattr(request, "user", None)
    return bool(
        user
        and user.is_authenticated
        or get_external_user_id_from_request(request)
        or getattr(request, "user_info", None)
    )


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
    return render(request, template_name, {
        'available_models': available_models,
    })


def _prompt_queryset_for_user(user):
    queryset = Prompt.objects.select_related("topic", "topic__programming_language", "owner")
    if not user or not user.is_authenticated:
        return queryset.none()
    return queryset.filter(owner=user)


def prompt_developer_login_view(request):
    default_next = "/ai/admin/arm/find-error/"
    next_url = _safe_relative_url(request.GET.get("next"), default_next)
    back_url = _safe_relative_url(request.GET.get("back"), "/")

    # Test-panel entry should always require explicit credentials.
    if request.method != "POST" and request.user.is_authenticated:
        logout(request)

    error_message = ""

    if request.method == "POST":
        next_url = _safe_relative_url(request.POST.get("next"), default_next)
        back_url = _safe_relative_url(request.POST.get("back"), "/")
        username = (request.POST.get("username") or "").strip()
        password = request.POST.get("password") or ""

        user = authenticate(request, username=username, password=password)
        if user and user.is_active and user.groups.filter(name=PROMPT_DEVELOPER_GROUP).exists():
            request.session["ai_testpanel_back_url"] = back_url
            login(request, user)
            csrf.rotate_token(request)
            return redirect(next_url)

        error_message = "Неверный логин/пароль или у пользователя нет группы prompt_developer."

    request.session["ai_testpanel_back_url"] = back_url

    response = render(
        request,
        "ai/test-panel-login.html",
        {
            "error_message": error_message,
            "next_url": next_url,
            "back_url": back_url,
        },
    )
    response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response["Pragma"] = "no-cache"
    return response


def legacy_set_password_view(request):
    """Allow user with unusable password to set their first password."""
    next_url = _safe_relative_url(
        request.POST.get("next") if request.method == "POST" else request.GET.get("next"),
        "/ai/admin/",
    )
    error_message = ""
    target_user = None

    if request.user.is_authenticated and request.user.is_active:
        target_user = request.user
    else:
        external_user_id = (
            request.POST.get("external_user_id")
            or request.GET.get("uid")
            or request.GET.get("userId")
        )
        if external_user_id:
            try:
                target_user = ExternalDLAccount.objects.select_related("user").get(
                    external_user_id=str(external_user_id)
                ).user
            except ExternalDLAccount.DoesNotExist:
                target_user = None

    if request.method == "POST":
        new_password = request.POST.get("new_password") or ""
        new_password_confirm = request.POST.get("new_password_confirm") or ""
        if not target_user or not target_user.is_active:
            error_message = "Не удалось найти пользователя для установки пароля."
        elif target_user.has_usable_password():
            error_message = "Пользователь уже имеет пароль. Используйте обычный вход."
        elif new_password != new_password_confirm:
            error_message = "Пароли не совпадают."
        elif len(new_password) < 8:
            error_message = "Пароль должен быть не менее 8 символов."
        else:
            target_user.set_password(new_password)
            target_user.save()
            login(request, target_user, backend='django.contrib.auth.backends.ModelBackend')
            csrf.rotate_token(request)
            request.session["ai_testpanel_back_url"] = "/"
            return redirect(next_url)

        response = render(
            request,
            "ai/set-password.html",
            {
                "error_message": error_message,
                "next_url": next_url,
                "username": target_user.username if target_user else "",
            },
        )
        response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response["Pragma"] = "no-cache"
        return response
    
    # GET: Show form
    response = render(
        request,
        "ai/set-password.html",
        {
            "error_message": error_message,
            "next_url": next_url,
            "username": target_user.username if target_user else "",
        },
    )
    response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response["Pragma"] = "no-cache"
    return response


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
    elif request.user.is_authenticated and request.user.is_active:
        target_user = request.user
    elif external_user_id:
        try:
            target_user = ExternalDLAccount.objects.select_related("user").get(
                external_user_id=str(external_user_id)
            ).user
        except ExternalDLAccount.DoesNotExist:
            target_user = None

    if request.method == "POST":
        new_password = request.POST.get("new_password") or ""
        new_password_confirm = request.POST.get("new_password_confirm") or ""

        if is_admin_registration and target_user and target_user.has_usable_password():
            ensure_prompt_developer_group(target_user)
            login(request, target_user, backend=ADMIN_EXTERNAL_AUTH_BACKEND)
            csrf.rotate_token(request)
            request.session["admin_fresh_auth"] = True
            return redirect(next_url)

        if not is_admin_registration and (not target_user or not target_user.is_active):
            error_message = "Не удалось найти пользователя для установки пароля."
        elif not is_admin_registration and target_user.has_usable_password():
            error_message = "Пользователь уже имеет пароль. Используйте обычный вход."
        elif new_password != new_password_confirm:
            error_message = "Пароли не совпадают."
        elif len(new_password) < 8:
            error_message = "Пароль должен быть не менее 8 символов."
        else:
            if is_admin_registration:
                if target_user:
                    target_user.set_password(new_password)
                    target_user.save(update_fields=["password"])
                    ensure_prompt_developer_group(target_user)
                else:
                    target_user = create_admin_user_with_password(external_user_id, new_password)
                login(request, target_user, backend=ADMIN_EXTERNAL_AUTH_BACKEND)
                csrf.rotate_token(request)
                request.session["admin_fresh_auth"] = True
            else:
                target_user.set_password(new_password)
                target_user.save()
                login(request, target_user, backend='django.contrib.auth.backends.ModelBackend')
                csrf.rotate_token(request)
                request.session["ai_testpanel_back_url"] = "/"
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
    languages = ProgrammingLanguage.objects.order_by('language_name').values('id', 'language_name')
    return JsonResponse(list(languages), safe=False)


def get_topics(request):
    topics = list(
        Topic.objects.select_related("programming_language")
        .order_by("topic_name")
        .values('id', 'topic_name', 'programming_language')
    )
    return JsonResponse(topics, safe=False)


def get_prompts(request):
    if not _has_page_access(request):
        return HttpResponseForbidden("Authentication required")

    prompts = list(
        Prompt.objects.select_related("topic", "topic__programming_language", "owner")
        .order_by("prompt_name", "id")
        .values(
            'id',
            'topic_id',
            'topic__programming_language',
            'prompt_text',
            'prompt_name',
        )
    )
    return JsonResponse(prompts, safe=False)


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
        # Конвертируется webm в wav
        from pydub import AudioSegment
        audio = AudioSegment.from_file(tmp_path)
        
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
