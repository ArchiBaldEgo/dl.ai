from django.shortcuts import render
from django.core.cache import cache
from django.http import JsonResponse
from django.http import HttpResponseForbidden, HttpResponseNotFound
from functools import wraps
from .models import ProgrammingLanguage, Topic, Prompt, AIAppSettings
import uuid


def ai_access_required(view_func):
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        is_django_authenticated = request.user.is_authenticated
        uid = (request.GET.get("uid") or request.session.get("external_uid") or "").strip()
        has_external_uid = uid.isdigit()

        if has_external_uid:
            request.session["external_uid"] = uid

        if not is_django_authenticated and not has_external_uid:
            return HttpResponseForbidden("Authentication required")

        if not AIAppSettings.get_solo().is_enabled:
            return HttpResponseNotFound("AI app is disabled")

        return view_func(request, *args, **kwargs)

    return _wrapped


@ai_access_required
def chat_view(request):
    # Генерируем уникальный client_id для каждого пользователя
    client_id = str(uuid.uuid4())
    return render(request, 'ai/chat.html', {'client_id': client_id})


@ai_access_required
def decide_task_view(request):
    client_id = str(uuid.uuid4())
    return render(request, 'ai/decide-task.html', {'client_id': client_id})


@ai_access_required
def find_error_view(request):
    client_id = str(uuid.uuid4())
    return render(request, 'ai/find-error.html', {'client_id': client_id})


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
