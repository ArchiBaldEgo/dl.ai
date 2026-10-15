"""Генератор тестовых записей журнала (/ai/admin/test_data/, только суперюзер).

Создаёт случайную запись AIRequestLog из РЕАЛЬНЫХ данных приложения —
модели из реестра (registry), существующие пользователи, режимы,
языки/темы/промпты из БД — чтобы фильтры и таблицы журнала показывали то же,
что и настоящие записи (раньше здесь были выдуманные «gpt-4o» и «Гурьянов»).

Запись идёт через LogWriter._sync-методы: публичные методы LogWriter —
async-обёртки (для WS-consumer), их вызов из синхронного view вернул бы
корутину, которая никогда не исполняется — записи не создаётся.
"""
import random
import uuid
from datetime import timedelta

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from ..i18n import get_localized_name
from ..model_clients import registry
from ..models import AIRequestLog, ProgrammingLanguage, Prompt, Task, Topic
from ..services.auth import get_user_identity_for_log
from ..services.log_writer import LogWriter
from .permissions import is_superuser_user
from .site import ai_admin_site


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


def pick_identity(request):
    """Identity случайного активного пользователя — для колонок «Кто отправил».

    Сначала — пользователи с внешним DL-аккаунтом (студенты), иначе любой
    активный. Поля заполняются единственной реализацией
    ``get_user_identity_for_log`` (DRY: user/username/external_user_id/ФИО —
    как в настоящих записях). Никого нет вообще → оператор-суперюзер.
    """
    from django.contrib.auth import get_user_model

    User = get_user_model()
    user = User.objects.filter(is_active=True, external_dl_account__isnull=False).order_by("?").first()
    if user is None:
        user = User.objects.filter(is_active=True).order_by("?").first()
    if user is None:
        user = None
    return get_user_identity_for_log(user or request.user, None, getattr(user or request.user, "external_dl_account", None))


def pick_model_title() -> str:
    """Название случайной ЗАРЕГИСТРИРОВАННОЙ модели (registry.title)."""
    return registry.title(random.choice(list(registry.keys())))


def pick_language():
    """Случайный ProgrammingLanguage (id + имя согласованы)."""
    return ProgrammingLanguage.objects.order_by("?").first()


def pick_topic() -> Topic | None:
    return Topic.objects.order_by("?").first()


def pick_prompt() -> Prompt | None:
    return Prompt.objects.order_by("?").first()


def pick_task() -> Task | None:
    """Случайная активная Task (реальный node_id — живая ссылка в деталях)."""
    return Task.objects.filter(active=True).order_by("?").first()


def pick_mode() -> str:
    """Случайный режим из существующих (source=websocket — это чат-режимы;

    arm/batch_solve живут на source=arm и здесь места не имеют)."""
    return random.choice([
        AIRequestLog.MODE_CHAT,
        AIRequestLog.MODE_SOLVE,
        AIRequestLog.MODE_FIND_ERROR,
        AIRequestLog.MODE_TESTING,
    ])


# Генерирует лог о запуске промпте модели
@require_POST
def test_data_emit_request_view(request: HttpRequest) -> HttpResponse:
    if not request.user.is_superuser:
        return JsonResponse({"error": "Только суперюзер"}, status=403)

    now = timezone.now()
    duration = round(random.uniform(0.5, 30.0), 2)
    end_time = now + timedelta(seconds=duration)
    is_error = random.random() < 0.2

    identity = pick_identity(request)
    mode = pick_mode()
    model_title = pick_model_title()
    language = pick_language()
    # solve/find_error привязаны к задаче (как настоящие записи); у чата и
    # тестирования задачи нет и в деталях не должно быть мёртвой DL-ссылки.
    task = pick_task() if mode in (AIRequestLog.MODE_SOLVE, AIRequestLog.MODE_FIND_ERROR) else None
    topic = pick_topic()
    prompt = pick_prompt()

    writer = LogWriter()

    log = writer.create_sync(
        user=identity["user"],
        username=identity["username"],
        external_user_id=identity["external_user_id"],
        user_full_name=identity["user_full_name"],
        client_id=f"debug-emit-{uuid.uuid4().hex[:8]}",
        source=AIRequestLog.SOURCE_WEBSOCKET,
        mode=mode,
        sent_at=now,
        model_names=[model_title],
        message="Тестовый запрос: напиши функцию sum(a, b) на Python",

        programming_language_id=language.pk if language else None,
        programming_language_name=language.language_name if language else "",

        topic_id=topic.pk if topic else None,
        topic_name=get_localized_name(topic, "ru", default_attr="topic_name") if topic else "",

        prompt_id=prompt.pk if prompt else None,
        prompt_name=get_localized_name(prompt, "ru", default_attr="prompt_name") if prompt else "",

        task_node_id=task.node_id if task else None,
        task_name=task.name if task else "",
    )

    if is_error:
        writer.update_error_sync(
            log,
            friendly="Тестовая ошибка: модель недоступна",
            detailed="Timeout after 30s (debug emit)",
            end_time=end_time,
        )
    else:
        writer.update_success_sync(
            log,
            response_text="def sum(a, b):\n    return a + b",
            tokens=random.randint(50, 5000),
            model_title=model_title,
            end_time=end_time,
        )

    # AJAX (fetch со страницы) → JSON-квитанция; обычная форма — redirect
    # (кнопка уходит в скрытый iframe, фидбек на странице — index.js).
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({"ok": True})
    return redirect(request.META.get("HTTP_REFERER") or reverse("ai_request_logs"))