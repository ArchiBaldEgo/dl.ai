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
