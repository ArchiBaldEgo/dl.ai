"""Сигналы для AI-приложения.

Инвалидирует кеш _get_last_update_date при сохранении/удалении UpdateLog,
чтобы дата последнего обновления на главной странице обновлялась мгновенно.
Также инвалидирует кеш get_available_model_options при изменении
AIModelAvailability, чтобы список доступных моделей обновлялся сразу после
health-check / автопроверки, не дожидаясь 30-секундного TTL.
И кеш справочника учебных названий (education_directory.education_title_maps)
при правке DLEducationName — правка справочника должна быть видна сразу.
И общий кеш набора ключей доступа (admin/permissions._all_access_keys) при
правке AIAdminGroupPermission — суперюзер не должен до 5 минут видеть устаревший
список ключей после добавления/удаления права.
И request-scoped кеш ключей доступа (admin/permissions.user_access_keys) при
изменении состава групп доступа и их m2m-связей (users/permissions) — отзыв
или выдача права действует немедленно, без перелогина.
"""

from django.db.models.signals import m2m_changed, post_save, post_delete
from django.dispatch import receiver

from .models import (
    AIAccessGroup,
    AIAdminGroupPermission,
    AIModelAvailability,
    DLEducationName,
    UpdateLog,
)


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


@receiver(post_save, sender=DLEducationName)
@receiver(post_delete, sender=DLEducationName)
def _invalidate_education_title_maps_cache(sender, **kwargs):
    """Сбрасывает кеш справочника «id → название» при правке DLEducationName."""
    from django.core.cache import cache
    from .constants import AI_CACHE_KEY_PREFIX
    cache.delete(f"{AI_CACHE_KEY_PREFIX}:education_title_maps")


@receiver(post_save, sender=AIAdminGroupPermission)
@receiver(post_delete, sender=AIAdminGroupPermission)
def _invalidate_all_access_keys_cache(sender, **kwargs):
    """Сбрасывает кеш полного набора ключей доступа при правке прав групп.

    Суперпользователь получает ВСЕ существующие ключи (admin/permissions.py::
    _all_access_keys) — набор кешируется в django-cache, поэтому добавление или
    удаление права должно сбрасывать кеш немедленно, иначе новое право не
    появится у суперюзера до истечения TTL. Заодно сбрасывается request-scoped
    кэш (тот же набор у суперюзера лежит и на инстансе request.user).
    """
    from django.core.cache import cache
    from .constants import AI_CACHE_KEY_PREFIX
    cache.delete(f"{AI_CACHE_KEY_PREFIX}:all_access_keys")
    from .admin.permissions import bump_access_keys_epoch

    bump_access_keys_epoch()


@receiver(post_save, sender=AIAccessGroup)
@receiver(post_delete, sender=AIAccessGroup)
def _invalidate_access_keys_epoch(sender, **kwargs):
    """Сбрасывает request-scoped кеш ключей доступа при правке группы."""
    from .admin.permissions import bump_access_keys_epoch

    bump_access_keys_epoch()


@receiver(m2m_changed, sender=AIAccessGroup.users.through)
@receiver(m2m_changed, sender=AIAccessGroup.permissions.through)
def _invalidate_access_keys_epoch_m2m(sender, action, **kwargs):
    """Сбрасывает request-scoped кеш ключей при изменении состава группы.

    Добавление/удаление участника или права меняет набор ключей юзера
    немедленно — кеш на инстансе request.user обязан это увидеть (иначе
    отзыв права «залипает» до конца запроса).
    """
    if action not in ("post_add", "post_remove", "post_clear"):
        return
    from .admin.permissions import bump_access_keys_epoch

    bump_access_keys_epoch()