import os
import sys

from django.apps import AppConfig
from django.db.models.signals import post_migrate


def ensure_default_groups(sender, **kwargs):
    # Ensure required RBAC groups exist in every environment.
    from django.contrib.auth.models import Group

    Group.objects.get_or_create(name="tester")


class AiConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'ai'

    def ready(self):
        post_migrate.connect(ensure_default_groups, sender=self, dispatch_uid="ai.ensure_default_groups")

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
        start_model_health_scheduler()
