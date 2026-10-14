# ai/admin/debug/emit_request.py
# DESC: Создаёт тестовый лог о запросе и записывает через логгер.
import random
import uuid
from datetime import timedelta

from asgiref.sync import async_to_sync
from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.template.response import TemplateResponse
from django.views.decorators.http import require_POST

from .permissions import can_access_prompt_admin
from .site import ai_admin_site
from ..models import Prompt
from .models import PromptAdmin

from .permissions import is_superuser_user

from ..models import AIRequestLog
from ..services.log_writer import LogWriter


#region Register

def get_test_data_url():
    return "/ai/admin/test_data/"

def can_access_test_data(request):
    """Генератор тестовых данных: только суперпользователь/разработчик."""
    if not request.user.is_authenticated:
        return False
    return is_superuser_user(request.user)

#endregion 


def test_data_view(request: HttpRequest) -> TemplateResponse:

    context = {
        **ai_admin_site.each_context(request),
        "title": "DL.AI: Тестовые данные",
    }
    return TemplateResponse(request, "admin/ai/test_data.html", context)

def pick_model() -> str:
    return random.choice(["gpt-4o", "claude-3.5", "yandex-gpt"])

def pick_user_full_name() -> str:
    return random.choice(["Вадим Гурьянов", "Владислав Гулевич", "Егор Беспалов", "Михаил Долинский", "Михаил Трубецкой"])

def pick_message() -> str:
    return "Тестовый запрос: напиши функцию sum(a, b) на Python"

def pick_language_name() -> str:
    return random.choice(["Python", "C++", "Java"])

# Генерирует лог о запуске промпте модели
@require_POST
def test_data_emit_request_view(request: HttpRequest) -> HttpResponse:
    if not request.user.is_superuser:
        return JsonResponse({"error": "Только суперюзер"}, status=403)

    now = timezone.now()
    duration = round(random.uniform(0.5, 30.0), 2)
    end_time = now + timedelta(seconds=duration)
    is_error = random.random() < 0.2

    # TODO: valid model_title select
    model_title = pick_model()

    writer = LogWriter()

    log = async_to_sync(writer.create)(
        user=request.user,
        username=request.user.username,
        external_user_id="",
        user_full_name=pick_user_full_name(),
        client_id=f"debug-emit-{uuid.uuid4().hex[:8]}",
        source=AIRequestLog.SOURCE_WEBSOCKET,
        mode=random.choice([
            AIRequestLog.MODE_CHAT,
            AIRequestLog.MODE_SOLVE,
            AIRequestLog.MODE_FIND_ERROR,
            AIRequestLog.MODE_TESTING,
        ]),
        sent_at=now,
        model_names=[model_title],
        message=pick_message(),
        
        programming_language_id=None,
        programming_language_name=pick_language_name(),

        topic_id=None,
        topic_name=random.choice(["Циклы", "Массивы", "Рекурсия"]),
        
        prompt_id=None,
        prompt_name=random.choice(["Помощник по Python", "Объясни ошибку"]),
        
        task_node_id=random.randint(1000, 9999),
        task_name=random.choice(["Сумма массива", "Факториал"]),
    )

    if is_error:
        async_to_sync(writer.update_error)(
            log,
            friendly="Тестовая ошибка: модель недоступна",
            detailed="Timeout after 30s (debug emit)",
            end_time=end_time,
        )
    else:
        async_to_sync(writer.update_success)(
            log,
            response_text="def sum(a, b):\n    return a + b",
            tokens=random.randint(50, 5000),
            model_title=model_title,
            end_time=end_time,
        )

    return redirect(request.META.get("HTTP_REFERER") or reverse("ai_request_logs"))