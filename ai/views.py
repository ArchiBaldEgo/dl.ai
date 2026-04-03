from django.shortcuts import render
from django.core.cache import cache
from django.http import JsonResponse, Http404, FileResponse
from django.conf import settings
from .models import ProgrammingLanguage, Topic, Prompt
from pathlib import Path
import mimetypes
import uuid

def chat_view(request):
    # Генерируем уникальный client_id для каждого пользователя
    client_id = str(uuid.uuid4())
    return render(request, 'ai/chat.html', {'client_id': client_id})

def decide_task_view(request):
    client_id = str(uuid.uuid4())
    return render(request, 'ai/decide-task.html', {'client_id': client_id})

def find_error_view(request):
    client_id = str(uuid.uuid4())
    return render(request, 'ai/find-error.html', {'client_id': client_id})


def asset_view(request, asset_path):
    static_dir = (Path(settings.BASE_DIR) / 'static').resolve()
    requested_path = asset_path.lstrip('/').strip()
    file_path = (static_dir / requested_path).resolve()

    if not str(file_path).startswith(str(static_dir)):
        raise Http404('Asset path is not allowed')

    if not file_path.is_file():
        raise Http404('Asset not found')

    content_type, _ = mimetypes.guess_type(str(file_path))
    response = FileResponse(
        file_path.open('rb'),
        content_type=content_type or 'application/octet-stream',
    )
    response['Cache-Control'] = 'public, max-age=86400'
    return response


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
