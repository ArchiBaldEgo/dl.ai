"""Глобальная статистика дневного использования токенов.

Отображает баннер на странице чата: «использовано сегодня / лимит»
в единых единицах (миллионах). Сумма AIRequestLog.tokens за текущий
Московский день, с краткосрочным кэшированием.
"""

from datetime import datetime
from functools import lru_cache

from django.conf import settings
from django.core.cache import cache
from django.db.models import Sum
from django.utils import timezone

from .constants import AI_CACHE_KEY_PREFIX, MOSCOW_TZ
from .models import AIRequestLog

# Default daily token budget. Override with the AI_DAILY_TOKEN_LIMIT env var.
# 0 / unset disables the limit side of the banner (only the used count shows).
DEFAULT_DAILY_TOKEN_LIMIT = 1_900_000

# Cache TTL for the aggregate query (seconds). Short, so the banner is fresh
# without aggregating the log table on every page render.
DEFAULT_CACHE_TTL = 60


def get_daily_token_limit():
    """Configured daily token budget, or 0 when disabled."""
    raw = getattr(settings, "AI_DAILY_TOKEN_LIMIT", DEFAULT_DAILY_TOKEN_LIMIT)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 0
    return max(value, 0)


def _msk_day_start():
    """Return the start of the current Moscow day as an aware datetime."""
    now_msk = timezone.now().astimezone(MOSCOW_TZ)
    return datetime(now_msk.year, now_msk.month, now_msk.day, tzinfo=MOSCOW_TZ)


def _cache_key():
    # Day-scoped key so the boundary rolling over invalidates naturally.
    day = timezone.now().astimezone(MOSCOW_TZ).strftime("%Y%m%d")
    return f"{AI_CACHE_KEY_PREFIX}:tokens:today:{day}"


def get_daily_tokens_used():
    """Sum of AIRequestLog.tokens for the current Moscow day (cached)."""
    ttl = getattr(settings, "AI_TOKEN_USAGE_TTL", DEFAULT_CACHE_TTL)
    try:
        ttl = max(int(ttl), 0)
    except (TypeError, ValueError):
        ttl = DEFAULT_CACHE_TTL

    key = _cache_key()
    cached = None if not ttl else cache.get(key)
    if cached is not None:
        return int(cached)

    start = _msk_day_start()
    aggregate = (
        AIRequestLog.objects.filter(sent_at__gte=start)
        .aggregate(total=Sum("tokens"))
        .get("total")
    )
    total = int(aggregate or 0)
    if ttl:
        cache.set(key, total, ttl)
    return total


@lru_cache(maxsize=1)
def _millions_format_template():
    # "%.3f" gives 3-decimal precision; trailing zeros are stripped so the
    # limit renders as "1.9", not "1.900". One unit (millions) for both sides.
    return "%.3f"


def _format_millions(value):
    if value is None:
        return "0"
    try:
        n = int(value)
    except (TypeError, ValueError):
        return "0"
    text = _millions_format_template() % (n / 1_000_000)
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def get_daily_token_usage():
    """Return the banner payload with clear consumed/remaining labels.

    Returns a dict with:
    - used: raw token count consumed today
    - limit: raw daily token budget (0 if disabled)
    - remaining: raw tokens remaining (limit - used, or 0 if no limit)
    - used_display: human-readable consumed (e.g. "1.2M")
    - limit_display: human-readable budget (e.g. "1.9M")
    - remaining_display: human-readable remaining (e.g. "0.7M")
    - percent: percentage used (0-100, or 0 if no limit)
    """
    used = get_daily_tokens_used()
    limit = get_daily_token_limit()
    remaining = max(limit - used, 0) if limit > 0 else 0
    percent = min(int(used / limit * 100), 100) if limit > 0 else 0
    return {
        "used": used,
        "limit": limit,
        "remaining": remaining,
        "used_display": _format_millions(used),
        "limit_display": _format_millions(limit) if limit > 0 else "",
        "remaining_display": _format_millions(remaining) if limit > 0 else "",
        "percent": percent,
    }


def invalidate_daily_tokens_cache():
    """Drop the cached daily total (for tests / admin force-refresh)."""
    cache.delete(_cache_key())