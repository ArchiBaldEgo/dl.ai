"""Сигналы для AI-приложения.

Инвалидирует кеш _get_last_update_date при сохранении/удалении UpdateLog,
чтобы дата последнего обновления на главной странице обновлялась мгновенно.
"""

from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver

from .models import UpdateLog


@receiver(post_save, sender=UpdateLog)
@receiver(post_delete, sender=UpdateLog)
def _invalidate_last_update_date_cache(sender, **kwargs):
    """Сбрасывает кеш даты последнего обновления при изменении UpdateLog."""
    from django.core.cache import cache
    cache.delete("ai_last_update_date")