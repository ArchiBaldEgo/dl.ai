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
    AIUserModel,
    AIWizardSeen,
    AuthorAlias,
    DLEducationName,
    ProgrammingLanguage,
    Prompt,
    SharedPrompt,
    Topic,
    UpdateLog,
)


def _bump_perf_epoch(name):
    """Инвалидирует производные кэши, зависящие от справочника ``name``."""
    from .services.cache_epochs import bump_epoch

    bump_epoch(name)


@receiver(post_save, sender=Prompt)
@receiver(post_delete, sender=Prompt)
@receiver(post_save, sender=SharedPrompt)
@receiver(post_delete, sender=SharedPrompt)
@receiver(post_save, sender=Topic)
@receiver(post_delete, sender=Topic)
@receiver(post_save, sender=ProgrammingLanguage)
@receiver(post_delete, sender=ProgrammingLanguage)
def _invalidate_prompt_meta_epoch(sender, **kwargs):
    """Сбрасывает каталоги промптов/тем/языков и списки фильтров админки.

    Эпоха ``prompt_meta`` входит в ключи ai/services/catalog.py и кэша
    подписи по умолчанию для режимов: правка промпта, темы, языка или общего
    промпта видна сразу, без ожидания TTL.
    """
    _bump_perf_epoch("prompt_meta")


@receiver(
    m2m_changed,
    sender=Prompt.editors.through,
    dispatch_uid="ai_prompt_editors_prompt_meta",
)
@receiver(
    m2m_changed,
    sender=SharedPrompt.programming_languages.through,
    dispatch_uid="ai_sharedprompt_langs_prompt_meta",
)
@receiver(
    m2m_changed,
    sender=SharedPrompt.editors.through,
    dispatch_uid="ai_sharedprompt_editors_prompt_meta",
)
def _invalidate_prompt_meta_epoch_m2m(sender, action, **kwargs):
    if action not in ("post_add", "post_remove", "post_clear"):
        return
    _bump_perf_epoch("prompt_meta")


@receiver(post_save, sender=AIWizardSeen)
@receiver(post_delete, sender=AIWizardSeen)
def _invalidate_wizard_epoch(sender, **kwargs):
    """Сбрасывает кэш «показывать ли тур» при отметке/сбросе просмотра."""
    _bump_perf_epoch("wizard")


@receiver(post_save, sender=AIUserModel)
@receiver(post_delete, sender=AIUserModel)
def _invalidate_user_models_epoch(sender, **kwargs):
    """Сбрасывает кэш опций личных моделей в селекторе (сохранение/удаление)."""
    _bump_perf_epoch("user_models")


@receiver(
    m2m_changed,
    sender=AIUserModel.shared_with.through,
    dispatch_uid="ai_usermodel_shared_with_epoch",
)
def _invalidate_user_models_epoch_m2m(sender, action, **kwargs):
    if action not in ("post_add", "post_remove", "post_clear"):
        return
    _bump_perf_epoch("user_models")


@receiver(post_save, sender=AuthorAlias)
@receiver(post_delete, sender=AuthorAlias)
def _invalidate_update_authors_cache(sender, **kwargs):
    """Список авторов на странице «Обновления» — из кэша; правка алиаса видна сразу."""
    from .services.catalog import UPDATE_AUTHORS_CACHE_KEY
    from django.core.cache import cache

    cache.delete(UPDATE_AUTHORS_CACHE_KEY)


@receiver(post_save, sender=UpdateLog)
@receiver(post_delete, sender=UpdateLog)
def _invalidate_last_update_date_cache(sender, **kwargs):
    """Сбрасывает кеш даты последнего обновления при изменении UpdateLog."""
    from django.core.cache import cache

    from .services.catalog import UPDATE_AUTHORS_CACHE_KEY

    cache.delete("ai_last_update_date")
    # Список авторов в фильтре «Обновлений» — новый ник/удаление записи
    # должны появиться сразу.
    cache.delete(UPDATE_AUTHORS_CACHE_KEY)


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