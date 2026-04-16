import os

from django.contrib.auth import authenticate, login
from django.shortcuts import redirect, render
from django.core.cache import cache
from django.http import JsonResponse
from django.http import FileResponse, Http404, HttpResponseForbidden, HttpResponseNotFound
from django.db import ProgrammingError
from django.contrib.staticfiles import finders
from functools import wraps
from .model_health import get_available_model_options
from .models import ProgrammingLanguage, Topic, Prompt, AIAppSettings
import uuid


def ai_access_required(view_func):
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        is_authenticated = bool(getattr(request, "user", None) and request.user.is_authenticated)
        uid = (request.GET.get("uid") or "").strip()

        # Fast-fail before touching DB when request is clearly unauthorized.
        if not is_authenticated and not uid.isdigit():
            return HttpResponseForbidden("Authentication required")

        try:
            if not AIAppSettings.get_solo().is_enabled:
                return HttpResponseNotFound("AI app is disabled")
        except ProgrammingError:
            # AIAppSettings table may be absent before migrations are applied.
            pass

        return view_func(request, *args, **kwargs)

    return _wrapped


def tester_access_required(view_func):
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        user = getattr(request, "user", None)
        if not user or not user.is_authenticated:
            return HttpResponseForbidden("Tester access required")
        if user.is_superuser or user.groups.filter(name="tester").exists():
            return view_func(request, *args, **kwargs)
        return HttpResponseForbidden("Tester access required")

    return _wrapped


def tester_login_view(request):
    if request.user.is_authenticated and request.user.groups.filter(name="tester").exists():
        return redirect("/ai/admin/arm/find-error/")

    error_message = ""

    if request.method == "POST":
        username = (request.POST.get("username") or "").strip()
        password = request.POST.get("password") or ""

        user = authenticate(request, username=username, password=password)
        if user and user.is_active and user.groups.filter(name="tester").exists():
            login(request, user)
            return redirect("/ai/admin/arm/find-error/")

        error_message = "Неверный логин/пароль или у пользователя нет группы tester."

    return render(request, "ai/test-panel-login.html", {"error_message": error_message})


@ai_access_required
def chat_view(request):
    # Генерируем уникальный client_id для каждого пользователя
    client_id = str(uuid.uuid4())
    return render(request, 'ai/chat.html', {
        'client_id': client_id,
        'available_models': get_available_model_options(),
    })


@ai_access_required
@tester_access_required
def decide_task_view(request):
    client_id = str(uuid.uuid4())
    return render(request, 'ai/decide-task.html', {
        'client_id': client_id,
        'available_models': get_available_model_options(),
    })


@ai_access_required
@tester_access_required
def find_error_view(request):
    client_id = str(uuid.uuid4())
    return render(request, 'ai/find-error.html', {
        'client_id': client_id,
        'available_models': get_available_model_options(),
    })


@ai_access_required
@tester_access_required
def get_languages(request):
    languages = ProgrammingLanguage.objects.all().values('id', 'language_name')
    return JsonResponse(list(languages), safe=False)


@ai_access_required
@tester_access_required
def get_topics(request):
    topics = list(Topic.objects.values('id', 'topic_name', 'programming_language'))
    return JsonResponse(topics, safe=False)



@ai_access_required
@tester_access_required
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
