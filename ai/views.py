import os

from django.contrib.auth import authenticate, login, logout
from django.shortcuts import redirect, render
from django.http import JsonResponse
from django.http import FileResponse, Http404, HttpResponseForbidden, HttpResponseNotFound
from django.db import ProgrammingError
from django.contrib.staticfiles import finders
from functools import wraps
from .model_health import get_available_model_options
from .models import ProgrammingLanguage, Topic, Prompt, AIAppSettings
import uuid

PROMPT_DEVELOPER_GROUP = "prompt_developer"


def _safe_relative_url(candidate, fallback):
    value = (candidate or "").strip()
    if value.startswith("/") and not value.startswith("//"):
        return value
    return fallback


def ai_access_required(view_func):
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        uid = (request.GET.get("uid") or "").strip()

        if not uid.isdigit():
            return HttpResponseForbidden("UID query parameter is required")

        try:
            if not AIAppSettings.get_solo().is_enabled:
                return HttpResponseNotFound("AI app is disabled")
        except ProgrammingError:
            # AIAppSettings table may be absent before migrations are applied.
            pass

        return view_func(request, *args, **kwargs)

    return _wrapped


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


@ai_access_required
def chat_view(request):
    # Генерируем уникальный client_id для каждого пользователя
    client_id = str(uuid.uuid4())
    return render(request, 'ai/chat.html', {
        'client_id': client_id,
        'available_models': get_available_model_options(),
    })


@ai_access_required
def decide_task_view(request):
    client_id = str(uuid.uuid4())
    return render(request, 'ai/decide-task.html', {
        'client_id': client_id,
        'available_models': get_available_model_options(),
    })


@ai_access_required
def find_error_view(request):
    client_id = str(uuid.uuid4())
    return render(request, 'ai/find-error.html', {
        'client_id': client_id,
        'available_models': get_available_model_options(),
    })


@ai_access_required
def get_languages(request):
    languages = ProgrammingLanguage.objects.all().values('id', 'language_name')
    return JsonResponse(list(languages), safe=False)


@ai_access_required
def get_topics(request):
    topics = list(Topic.objects.values('id', 'topic_name', 'programming_language'))
    return JsonResponse(topics, safe=False)



@ai_access_required
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
