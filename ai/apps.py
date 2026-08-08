"""Конфигурация Django-приложения ai.

При ready() подключает сигнал post_migrate для создания группы prompt_developer
и запускает планировщик проверки доступности моделей (если не отключён
через AI_DISABLE_HEALTH_SCHEDULER и запущен через daphne/gunicorn/uvicorn/runserver).
"""

import os
import sys

from django.apps import AppConfig
from django.db.models.signals import post_migrate


def ensure_default_groups(sender, **kwargs):
    """Создаёт RBAC-группу prompt_developer и назначает ей права на Prompt.

    Вызывается через сигнал post_migrate после применения миграций.
    Гарантирует наличие группы в любой среде (dev, staging, prod).
    """
    from django.contrib.auth.models import Group, Permission
    from django.contrib.contenttypes.models import ContentType

    prompt_developer_group, _ = Group.objects.get_or_create(name="prompt_developer")

    prompt_content_type = ContentType.objects.filter(app_label="ai", model="prompt").first()
    if prompt_content_type:
        prompt_permissions = Permission.objects.filter(
            content_type=prompt_content_type,
            codename__in=("add_prompt", "view_prompt", "change_prompt"),
        )
        prompt_developer_group.permissions.add(*prompt_permissions)


class AiConfig(AppConfig):
    """Конфигурация приложения ai.

    В методе ready() регистрирует обработчик post_migrate для создания
    группы prompt_developer и запускает фоновый планировщик проверки
    доступности AI-моделей (model_health). Планировщик запускается только
    в основном процессе (не в autoreloader) и только для daphne/gunicorn/uvicorn/runserver.
    """
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'ai'
    verbose_name = 'Раздел ИИ'

    def ready(self):
        post_migrate.connect(ensure_default_groups, sender=self, dispatch_uid="ai.ensure_default_groups")

        # Подключаем сигналы (инвалидация кеша UpdateLog)
        from . import signals  # noqa: F401

        if os.getenv("AI_DISABLE_HEALTH_SCHEDULER", "").strip().lower() in {"1", "true", "yes", "on"}:
            return

        argv = [arg.lower() for arg in sys.argv]
        executable = argv[0] if argv else ""

        if executable.endswith("manage.py"):
            command = argv[1] if len(argv) >= 2 else ""
            if command != "runserver":
                return

            # Avoid duplicate scheduler thread from Django autoreloader parent process.
            if os.getenv("RUN_MAIN") != "true":
                return

        elif not any(name in executable for name in ("daphne", "gunicorn", "uvicorn")):
            return

        from .model_health import start_model_health_scheduler
        # The scheduler thread performs the first health check immediately (in
        # its own daemon thread, so it never blocks app startup) and then waits
        # for the next 04:00 MSK window. No synchronous warm-up is needed here.
        start_model_health_scheduler()
