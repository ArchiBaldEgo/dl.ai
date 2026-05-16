import os

from django.contrib.auth import authenticate, login, logout, get_user_model
from django.shortcuts import redirect, render
from django.http import JsonResponse
from django.http import FileResponse, Http404, HttpResponseForbidden, HttpResponseNotFound
from django.db import ProgrammingError
from django.contrib.staticfiles import finders
from functools import wraps
from .model_health import get_available_model_options
from .models import ProgrammingLanguage, Topic, Prompt, AIAppSettings

User = get_user_model()

PROMPT_DEVELOPER_GROUP = "prompt_developer"


def health_view(request):
    return JsonResponse({"ok": True})


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


def set_password_view(request):
    """Allow user with unusable password to set their first password."""
    if request.method == "POST":
        old_username = (request.POST.get("username") or "").strip()
        new_password = request.POST.get("new_password") or ""
        new_password_confirm = request.POST.get("new_password_confirm") or ""
        next_url = _safe_relative_url(request.POST.get("next"), "/ai/admin/")
        
        error_message = ""
        
        try:
            user = authenticate(request, username=old_username, password=None)
            if not user or not user.is_active:
                # User doesn't exist or inactive - authenticate by username for password-less account
                try:
                    user = User.objects.get(username=old_username, is_active=True)
                    if not user.has_unusable_password():
                        error_message = "Пользователь уже имеет пароль. Используйте обычный вход."
                        user = None
                except User.DoesNotExist:
                    error_message = "Пользователь не найден."
            
            if user and user.has_unusable_password():
                if new_password != new_password_confirm:
                    error_message = "Пароли не совпадают."
                elif len(new_password) < 8:
                    error_message = "Пароль должен быть не менее 8 символов."
                else:
                    # Set password and log in
                    user.set_password(new_password)
                    user.save()
                    login(request, user, backend='django.contrib.auth.backends.ModelBackend')
                    request.session["ai_testpanel_back_url"] = "/"
                    return redirect(next_url)
            elif not user:
                if not error_message:
                    error_message = "Не удалось найти пользователя для установки пароля."
        except Exception as e:
            error_message = f"Ошибка: {str(e)}"
        
        response = render(
            request,
            "ai/set-password.html",
            {
                "error_message": error_message,
                "next_url": next_url,
            },
        )
        response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response["Pragma"] = "no-cache"
        return response
    
    # GET: Show form
    next_url = _safe_relative_url(request.GET.get("next"), "/ai/admin/")
    response = render(
        request,
        "ai/set-password.html",
        {
            "error_message": "",
            "next_url": next_url,
        },
    )
    response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response["Pragma"] = "no-cache"
    return response


def chat_view(request):
    return render(request, 'ai/chat.html', {
        'available_models': get_available_model_options(),
    })


def decide_task_view(request):
    return render(request, 'ai/decide-task.html', {
        'available_models': get_available_model_options(),
    })


def find_error_view(request):
    return render(request, 'ai/find-error.html', {
        'available_models': get_available_model_options(),
    })


def get_languages(request):
    languages = ProgrammingLanguage.objects.all().values('id', 'language_name')
    return JsonResponse(list(languages), safe=False)


def get_topics(request):
    topics = list(Topic.objects.values('id', 'topic_name', 'programming_language'))
    return JsonResponse(topics, safe=False)


def get_prompts(request):
    prompts = list(Prompt.objects.values(
        'id', 
        'topic_id',  # ID Topic
        'topic__programming_language',  # ID ProgrammingLanguage
        'prompt_text', 
        'prompt_name',
    ))
    return JsonResponse(prompts, safe=False)


def asset_view(request, asset_path):
    asset_full_path = finders.find(asset_path)
    if not asset_full_path or not os.path.isfile(asset_full_path):
        raise Http404("Asset not found")

    return FileResponse(open(asset_full_path, "rb"))
