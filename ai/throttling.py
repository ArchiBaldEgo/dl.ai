"""Per-user rate limiting for HTTP views and WebSocket messages."""

import logging
from functools import wraps
from typing import Callable

from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse

logger = logging.getLogger(__name__)

DEFAULT_WS_LIMIT = 120
DEFAULT_HTTP_LIMIT = 60
DEFAULT_WINDOW = 60


def _get_limits():
    return (
        getattr(settings, "AI_WS_RATE_LIMIT", DEFAULT_WS_LIMIT),
        getattr(settings, "AI_HTTP_RATE_LIMIT", DEFAULT_HTTP_LIMIT),
        getattr(settings, "AI_RATE_LIMIT_WINDOW", DEFAULT_WINDOW),
    )


def _identity_key(prefix: str, user_id: str) -> str:
    return f"ai:ratelimit:{prefix}:{user_id}"


class RateLimiter:
    """Simple per-user sliding-window rate limiter backed by Django cache."""

    def __init__(self, ws_limit=None, http_limit=None, window_seconds=None):
        self._ws_limit = ws_limit
        self._http_limit = http_limit
        self._window_seconds = window_seconds

    @property
    def ws_limit(self) -> int:
        if self._ws_limit is None:
            self._ws_limit = _get_limits()[0]
        return self._ws_limit

    @property
    def http_limit(self) -> int:
        if self._http_limit is None:
            self._http_limit = _get_limits()[1]
        return self._http_limit

    @property
    def window_seconds(self) -> int:
        if self._window_seconds is None:
            self._window_seconds = _get_limits()[2]
        return self._window_seconds

    def _check(self, prefix: str, user_id: str, limit: int) -> bool:
        if not user_id:
            return True
        key = _identity_key(prefix, str(user_id))
        try:
            current = cache.get(key, 0)
            if not isinstance(current, int):
                current = 0
            current += 1
            cache.set(key, current, timeout=self.window_seconds)
            return current <= limit
        except Exception:
            logger.exception("Rate limiter cache failure; allowing request")
            return True

    def is_allowed_ws(self, user_id: str) -> bool:
        return self._check("ws", user_id, self.ws_limit)

    def is_allowed_http(self, user_id: str) -> bool:
        return self._check("http", user_id, self.http_limit)


# Global instance used by consumers and middleware.
# Settings are resolved lazily so this can be imported before django.setup().
rate_limiter = RateLimiter()


def get_request_user_id(request) -> str:
    """Extract a stable user identifier from a Django request."""
    user = getattr(request, "user", None)
    if user and getattr(user, "is_authenticated", False):
        try:
            return str(user.external_dl_account.external_user_id)
        except AttributeError:
            return str(user.pk)
    user_info = getattr(request, "user_info", None)
    if isinstance(user_info, dict):
        return str(user_info.get("userId", ""))
    return ""


RATE_LIMIT_MESSAGE = "Слишком много запросов. Попробуйте позже."


def _is_ajax_request(request) -> bool:
    """Return True for fetch/XHR/AJAX requests that expect a JSON response.

    Used so the rate limiter always answers API-style callers with JSON
    (preventing ``JSON.parse`` errors on the frontend) and only falls back to
    plain text for browser navigations.
    """
    accept = (request.headers.get("Accept") or "").lower()
    if "application/json" in accept:
        return True
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return True
    if request.headers.get("Sec-Fetch-Mode") == "cors":
        return True
    if request.path.startswith("/ai/api/"):
        return True
    return False


def _rate_limit_response(request):
    """Build the 429 response in the format the caller can parse."""
    if _is_ajax_request(request):
        return JsonResponse({"error": RATE_LIMIT_MESSAGE}, status=429)
    from django.http import HttpResponse
    return HttpResponse(RATE_LIMIT_MESSAGE, status=429)


def rate_limited(view_func: Callable) -> Callable:
    """Decorator that applies the HTTP rate limit to a view."""
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        user_id = get_request_user_id(request)
        if user_id and not rate_limiter.is_allowed_http(user_id):
            logger.warning(f"HTTP rate limit exceeded for user {user_id}")
            return _rate_limit_response(request)
        return view_func(request, *args, **kwargs)
    return _wrapped


class RateLimitMiddleware:
    """Middleware that enforces rate limits on all /ai/ HTTP requests."""

    def __init__(self, get_response):
        self.get_response = get_response
        self.enabled = getattr(settings, "AI_RATE_LIMIT_ENABLED", True)

    def __call__(self, request):
        if not self.enabled or not request.path.startswith("/ai/"):
            return self.get_response(request)

        user_id = get_request_user_id(request)
        if user_id and not rate_limiter.is_allowed_http(user_id):
            logger.warning(f"HTTP rate limit exceeded for user {user_id}")
            return _rate_limit_response(request)

        return self.get_response(request)
