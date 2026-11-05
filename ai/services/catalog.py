"""Каталоги справочников: списки фильтров админки и списки для API страниц.

Один владелец производных (кэшируемых) представлений Prompt/Topic/
ProgrammingLanguage/SharedPrompt/UpdateLog — их читают:

* фильтры ``PromptAdmin`` (``PromptUserIdFilter``, ``PromptLanguageFilter``,
  ``PromptTopicFilter``) — списки вариантов строятся на каждой загрузке
  changelist'а, хотя меняются редко;
* страница «Обновления» — список авторов;
* API ``/ai/api/get-problem-data|get-prompts|get-shared-prompts`` — полные
  списки языков/тем/промптов, которые запрашиваются на каждом открытии
  страницы чата/«Реши задачу»/«В чём ошибка».

Кэш — django-cache с ключом, включающим именованную эпоху ``prompt_meta``
(ai/services/cache_epochs.py). Эпоху бампают сигналы в ai/signals.py при
сохранении/удалении промпта, темы, языка, общего промпта и m2m-связей
(редакторы, языки общего промпта) — правка видна сразу, TTL нужен только
чтобы мусор изредка вычищался. Текст промпта, который уходит в модель, здесь
НЕ кэшируется: кэшируются подписи/списки для UI (см. non-goals).

В тестах ``AI_PERF_CACHE_TTL=0`` — кэш выключен, функции всегда идут в БД.
"""

from ..models import ProgrammingLanguage, SharedPrompt, Topic
from ..serializers import (
    programming_language as serialize_language,
    prompt as serialize_prompt,
    shared_prompt as serialize_shared_prompt,
    shared_prompt_with_dates as serialize_shared_prompt_with_dates,
    topic as serialize_topic,
)
from .cache_epochs import cache_key, cached_call, epoch

PROMPT_META_EPOCH = "prompt_meta"

#: Ключ списка авторов «Обновлений» (сбрасывается сигналами UpdateLog и
#: AuthorAlias — правка алиаса/появление ника видны сразу).
UPDATE_AUTHORS_CACHE_KEY = cache_key("catalog", "update_authors")

FILTER_TTL = 120
PAGE_CATALOG_TTL = 60


def _prompt_meta_key(*parts):
    return cache_key("catalog", epoch(PROMPT_META_EPOCH), *parts)


def user_filter_label(user):
    """Подпись пользователя в фильтрах: «ФИО (DL-id)», без дублей.

    Для DL-аккаунтов без имён ``display_name_for_user`` возвращает сам DL-id
    (``user_42`` → ``42``) — тогда не повторяем его дважды.
    """
    from .user_directory import display_name_for_user, dl_id_for_user

    display = (display_name_for_user(user) or "").strip()
    dl_id = (dl_id_for_user(user) or "").strip()
    if not display or display == dl_id:
        return dl_id or display
    return f"{display} ({dl_id})"


def prompt_filter_users():
    """Владельцы/редакторы, у которых есть промпты: [(str(pk), подпись)]."""
    from django.contrib.auth import get_user_model
    from django.db.models import Q

    def compute():
        User = get_user_model()
        users = (
            User.objects.filter(
                Q(owned_prompts__isnull=False) | Q(editable_prompts__isnull=False)
            )
            .select_related("external_dl_account")
            .distinct()
            .order_by("id")
        )
        return [(str(user.pk), user_filter_label(user)) for user in users]

    return cached_call(_prompt_meta_key("filter", "users"), compute, FILTER_TTL)


def prompt_filter_languages():
    """Все языки программирования для фильтра: [(str(pk), имя)]."""
    def compute():
        return [
            (str(lang.pk), lang.language_name)
            for lang in ProgrammingLanguage.objects.order_by("language_name")
        ]

    return cached_call(_prompt_meta_key("filter", "languages"), compute, FILTER_TTL)


def prompt_filter_topics(language_id=None):
    """Темы (только те, у которых есть промпты), опционально — одного языка.

    Возвращает ``[(str(pk), "Язык — Тема")]``. Подпись и порядок ровно те же,
    что были у ``PromptTopicFilter.lookups``.
    """
    language_key = str(language_id) if language_id else ""

    def compute():
        topics = (
            Topic.objects.select_related("programming_language")
            .filter(prompt__isnull=False)
            .distinct()
            .order_by(
                "programming_language__language_name", "topic_name_ru", "pk",
            )
        )
        if language_id:
            topics = topics.filter(programming_language_id=language_id)
        return [(str(topic.pk), topic_filter_label(topic)) for topic in topics]

    return cached_call(
        _prompt_meta_key("filter", "topics", language_key), compute, FILTER_TTL,
    )


def topic_filter_label(topic):
    """Подпись темы в фильтре: «Язык — Тема» (как раньше)."""
    name = topic.topic_name_ru or f"Тема #{topic.pk}"
    language = getattr(topic.programming_language, "language_name", "") or ""
    return f"{language} — {name}" if language else name


def prompt_filter_topic_label(topic_pk):
    """Подпись конкретной темы (для активного значения вне списка) или None."""
    if not topic_pk:
        return None

    def compute():
        topic = (
            Topic.objects.select_related("programming_language")
            .filter(pk=topic_pk)
            .first()
        )
        return topic_filter_label(topic) if topic is not None else None

    return cached_call(
        _prompt_meta_key("filter", "topic_label", topic_pk), compute, FILTER_TTL,
    )


def prompts_have_untopiced():
    """Есть ли промпты без темы (опция «— без темы» в фильтре тем)."""
    from ..models import Prompt

    return cached_call(
        _prompt_meta_key("filter", "has_untopiced"),
        lambda: Prompt.objects.filter(topic__isnull=True).exists(),
        FILTER_TTL,
    )


def update_authors():
    """Авторы видимых обновлений: [(сырой ник, подпись)] по подписи."""
    from ..models import AuthorAlias, UpdateLog

    def compute():
        author_map = AuthorAlias.display_map()
        return sorted(
            (
                (nick, author_map.get(nick, nick))
                for nick in UpdateLog.objects.filter(hidden=False)
                .values_list("author", flat=True)
                .distinct()
            ),
            key=lambda pair: pair[1].lower(),
        )

    return cached_call(UPDATE_AUTHORS_CACHE_KEY, compute, FILTER_TTL)


# ---------------------------------------------------------------------------
# Списки для API пользовательских страниц (тот же формат, что раньше отдавали
# view-функции: сериализаторы применяются здесь, ответы побайтно те же).
# ---------------------------------------------------------------------------


def language_list():
    def compute():
        return [
            serialize_language(lang)
            for lang in ProgrammingLanguage.objects.order_by("language_name")
        ]

    return cached_call(_prompt_meta_key("page", "languages"), compute, PAGE_CATALOG_TTL)


def topic_list(ui_language=""):
    def compute():
        return [
            serialize_topic(topic, ui_language)
            for topic in Topic.objects.select_related("programming_language").order_by(
                "topic_name_ru",
            )
        ]

    return cached_call(
        _prompt_meta_key("page", "topics", ui_language), compute, PAGE_CATALOG_TTL,
    )


def prompt_list(ui_language=""):
    from ..models import Prompt

    def compute():
        return [
            serialize_prompt(prompt, ui_language)
            for prompt in Prompt.objects.select_related(
                "topic", "topic__programming_language",
                "programming_language", "owner", "shared_prompt",
            ).order_by("prompt_name_ru", "id")
        ]

    return cached_call(
        _prompt_meta_key("page", "prompts", ui_language), compute, PAGE_CATALOG_TTL,
    )


def shared_prompt_list(ui_language="", language_id=None, with_dates=False):
    """Общие промпты без системного режима (для пользовательских страниц).

    ``language_id`` — оставить привязанные к языку или без привязки (для
    всех); ``with_dates`` — вариант сериализатора с created_at/updated_at
    (страница «Реши задачу»).
    """
    from django.db.models import Q

    def compute():
        queryset = (
            SharedPrompt.objects
            .prefetch_related("programming_languages")
            .filter(Q(mode__isnull=True) | Q(mode=""))
        )
        if language_id:
            queryset = queryset.filter(
                Q(programming_languages__id=language_id)
                | Q(programming_languages__isnull=True)
            ).distinct()
        serialize = (
            serialize_shared_prompt_with_dates if with_dates else serialize_shared_prompt
        )
        return [serialize(item, ui_language) for item in queryset]

    return cached_call(
        _prompt_meta_key(
            "page", "shared", ui_language, language_id or "", int(bool(with_dates)),
        ),
        compute,
        PAGE_CATALOG_TTL,
    )
