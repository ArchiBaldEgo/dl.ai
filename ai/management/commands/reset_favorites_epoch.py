"""Разовый сброс счётчика «фаворитов» моделей для всех пользователей.

Устанавливает AIAppSettings.favorites_epoch = now(). После этого
_get_user_top_model_keys учитывает только успешные AIRequestLog новее этой
даты — то есть у всех пользователей список моделей становится строго
алфавитным (с Web_DeepSeek приоритетом), а по мере новых успешных запросов
топ-2 фаворита снова набираются. Логи не удаляются.
"""
from django.core.management.base import BaseCommand
from django.utils import timezone

from ai.models import AIAppSettings


class Command(BaseCommand):
    help = "Сбросить счётчик фаворитов моделей (дата-отсечка) для всех пользователей."

    def handle(self, *args, **options):
        settings = AIAppSettings.get_solo()
        settings.favorites_epoch = timezone.now()
        settings.save(update_fields=["favorites_epoch"])
        self.stdout.write(self.style.SUCCESS(
            "Favorites epoch reset to %s. User top-model counters now start fresh; "
            "model lists are strictly alphabetical (after Web_DeepSeek priority) "
            "until new successful requests rebuild the top-2." % settings.favorites_epoch
        ))