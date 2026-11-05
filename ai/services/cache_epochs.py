"""Именованные эпохи и общий помощник кэширования «горячих» справочников.

Эпоха — это счётчик в django-cache, который входит в ключ производных кэшей.
Сигналы моделей вызывают ``bump_epoch("prompt_meta")`` и тем самым разом
инвалидируют все зависящие от них значения (списки фильтров, каталоги
промптов/тем/языков, подписи), вместо того чтобы знать точный набор ключей.

Все кэши этого модуля — производительные: их можно безопасно потерять при
перезапуске Redis, значение просто пересчитается. Текст промпта, который
уходит в модель, здесь не кэшируется (см. non-goals в плане ускорения).

TTL управляется настройкой ``AI_PERF_CACHE_TTL`` (по умолчанию 120 с). В
тестовой сюите она выставлена в 0: кэш отключён, иначе записи, созданные в
одном тесте и откаченные в конце, «протекали» бы в следующий тест до
истечения TTL. Тесты, которые проверяют само кэширование, включают TTL
через ``override_settings``.
"""

from django.conf import settings
from django.core.cache import cache

from ..constants import AI_CACHE_KEY_PREFIX

DEFAULT_PERF_CACHE_TTL = 120

_SENTINEL = object()


def perf_cache_ttl(default=DEFAULT_PERF_CACHE_TTL):
    """TTL производительного кэша в секундах (0 — кэш выключен)."""
    try:
        value = int(getattr(settings, "AI_PERF_CACHE_TTL", default))
    except (TypeError, ValueError):
        value = default
    return max(0, value)


def cache_key(*parts):
    return f"{AI_CACHE_KEY_PREFIX}:" + ":".join(str(part) for part in parts)


def epoch_key(name):
    return cache_key("epoch", name)


def epoch(name):
    """Текущее значение эпохи (0, если её ещё не бампали)."""
    return int(cache.get(epoch_key(name), 0) or 0)


def bump_epoch(name):
    """Сдвигает эпоху: старые ключи производных кэшей становятся мёртвыми.

    Именно инкремент, а не delete: стартовое значение 0, и после удаления
    ``epoch()`` снова вернул бы 0 — ключ производного кэша остался бы тем же,
    и инвалидация не сработала бы (правка промпта «залипала» до истечения
    TTL). ``cache.add`` атомарен, поэтому параллельные бампы не теряются.
    """
    key = epoch_key(name)
    if cache.add(key, 1):
        return
    try:
        cache.incr(key)
    except ValueError:  # ключ успели удалить/вытеснить — начинаем заново
        cache.set(key, 1)


def cached_call(key, compute, ttl=DEFAULT_PERF_CACHE_TTL):
    """Возвращает значение из кэша или вычисляет и запоминает его.

    ``compute`` вызывается ровно один раз при промахе; значение должно быть
    сериализуемым (кэш может быть Redis).
    """
    seconds = perf_cache_ttl(ttl)
    if seconds:
        hit = cache.get(key, _SENTINEL)
        if hit is not _SENTINEL:
            return hit
    value = compute()
    if seconds:
        cache.set(key, value, seconds)
    return value
