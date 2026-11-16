"""Конфигурация Django-приложения ai.

- import_models(): Django по умолчанию ищет модели в ``ai.models``; у нас
  они в пакете ``ai.db``. Переопределяем, чтобы ``migrate``, ``makemigrations``,
  админ и тесты видели все модели при ``django.setup()``.

- ready(): подключает сигнал post_migrate для создания RBAC-группы
  prompt_developer, регистрирует обработчики signals и запускает планировщик
  проверки доступности моделей (если не отключён через
  AI_DISABLE_HEALTH_SCHEDULER и запущен через daphne/gunicorn/uvicorn/runserver).
"""

import os
import sys
from importlib import import_module

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

    В import_models() переключает источник моделей с ``ai.models`` на ``ai.db``.
    В ready() регистрирует обработчик post_migrate для создания группы
    prompt_developer и запускает фоновый планировщик проверки доступности
    AI-моделей (model_health).
    """

    default_auto_field = 'django.db.models.BigAutoField'
    name = 'ai'
    verbose_name = 'Раздел ИИ'

    def import_models(self):
        """Загружает все модели из ``ai.db`` (вместо ``ai.models``).

        Django по умолчанию делает ``import_module(f"{self.name}.models")``.
        У нас модели в пакете ``ai.db``, а ``ai.models`` удалён — поэтому
        переопределяем:

        1. ``self.models = self.apps.all_models[self.label]`` — ModelBase.__new__
           регистрирует классы в этот словарь по мере определения.
        2. ``import_module("ai.db")`` — выполняет ``db/__init__.py``, который
           импортирует все модели, регистрируя их в ``all_models['ai']``.
        3. ``self.models_module = ...`` — используется makemigrations,
           admin-диагностикой, тестами и `python -c "import ai.models"` (не
           сработает — но и не нужно; см. примечание в конце).
        """
        self.models = self.apps.all_models[self.label]
        self.models_module = import_module("ai.db")

    def ready(self):
        post_migrate.connect(
            ensure_default_groups,
            sender=self,
            dispatch_uid="ai.ensure_default_groups",
        )

        # Подключаем сигналы (инвалидация кеша UpdateLog)
        from ai import signals  # noqa: F401

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

        from ai.model_health import start_model_health_scheduler
        # The scheduler thread performs the first health check immediately (in
        # its own daemon thread, so it never blocks app startup) and then waits
        # for the next 04:00 MSK window. No synchronous warm-up is needed here.
        start_model_health_scheduler()