"""Сигналы для AI-приложения.

Инвалидирует кеш _get_last_update_date при сохранении/удалении UpdateLog,
чтобы дата последнего обновления на главной странице обновлялась мгновенно.
Также инвалидирует кеш get_available_model_options при изменении
AIModelAvailability, чтобы список доступных моделей обновлялся сразу после
health-check / автопроверки, не дожидаясь 30-секундного TTL.
"""

from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver

from .models import UpdateLog, AIModelAvailability


@receiver(post_save, sender=UpdateLog)
@receiver(post_delete, sender=UpdateLog)
def _invalidate_last_update_date_cache(sender, **kwargs):
    """Сбрасывает кеш даты последнего обновления при изменении UpdateLog."""
    from django.core.cache import cache
    cache.delete("ai_last_update_date")


@receiver(post_save, sender=AIModelAvailability)
@receiver(post_delete, sender=AIModelAvailability)
def _invalidate_available_models_cache(sender, **kwargs):
    """Сбрасывает кеш списка доступных моделей при изменении AIModelAvailability."""
    from django.core.cache import cache
    from .constants import AI_CACHE_KEY_PREFIX
    cache.delete(f"{AI_CACHE_KEY_PREFIX}:available_models")