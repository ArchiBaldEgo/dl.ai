from django.shortcuts import render
from django.core.cache import cache
from django.http import JsonResponse
from .models import ProgrammingLanguage, Topic, Prompt
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
