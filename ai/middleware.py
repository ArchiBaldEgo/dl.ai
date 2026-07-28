"""Промежуточное ПО (middleware) для внешней аутентификации и CSRF.

ExternalAuthMiddleware: проверяет DLSID-куку, обращается к внешнему API
(dl.gsu.by) для получения информации о пользователе, автосоздаёт Django-пользователя
и привязывает сессию. При невалидном DLSID — редирект на dl.gsu.by.

CsrfSessionFallbackMiddleware: восстанавливает CSRF-токен из сессии при отсутствии куки.
"""

import os
import time
from urllib.parse import unquote

from django.conf import settings
from django.contrib.auth import login
from django.http import JsonResponse, HttpResponseRedirect
from django.middleware import csrf
from .external_account import get_or_create_user_from_external
from .external_auth import (
    ExternalAuthMisconfigured,
    ExternalAuthUnauthorized,
    ExternalAuthUnavailable,
    fetch_external_user_info,
    get_external_auth_api_url,
    get_external_session_cookie_name,
)
import logging

logger = logging.getLogger(__name__)


def _is_admin_path(path):
    """Проверяет, является ли путь путём админки (/ai/admin или /ai/admin/...)."""
    normalized = (path or "/").rstrip("/") or "/"
    return normalized == "/ai/admin" or normalized.startswith("/ai/admin/")


def _normalize_path(path: str | None) -> str:
    """Нормализует путь: убирает trailing slash, пустой путь возвращает как '/'."""
    normalized = (path or "/").rstrip("/") or "/"
    return normalized


def _is_optional_auth_path(path: str) -> bool:
    """Пути, где отсутствие DLSID не вызывает редирект на dl.gsu.by.

    Только для точек входа админки (login/logout/set-password), где пользователь
    должен иметь доступ к форме до аутентификации через внешний API.
    """
    normalized = _normalize_path(path)
    return _is_admin_path(normalized)


class CsrfSessionFallbackMiddleware:
    """Восстанавливает CSRF-токен из сессии при отсутствии куки.

    При CSRF_USE_SESSIONS=True Django хранит токен в сессии. Если кука
    отсутствует (например, после очистки куки), middleware восстанавливает
    токен из сессии, чтобы формы продолжали работать.
    """
    def __init__(self, get_response):
        self.get_response = get_response
        self.use_sessions = bool(getattr(settings, "CSRF_USE_SESSIONS", False))
        self.primary_cookie_name = getattr(settings, "CSRF_COOKIE_NAME", "csrftoken")
        self.fallback_cookie_names = []
        if self.primary_cookie_name != "csrftoken":
            self.fallback_cookie_names.append("csrftoken")

    def __call__(self, request):
        if self.use_sessions and hasattr(request, "session"):
            if csrf.CSRF_SESSION_KEY not in request.session:
                for name in [self.primary_cookie_name, *self.fallback_cookie_names]:
                    value = request.COOKIES.get(name)
                    if value:
                        request.session[csrf.CSRF_SESSION_KEY] = value
                        request.session.modified = True
                        break
        return self.get_response(request)


class ExternalAuthMiddleware:
    """Промежуточное ПО внешней аутентификации через dl.gsu.by.

    Поток:
    1. Извлекает DLSID-куку из запроса.
    2. Если DLSID отсутствует — редирект на dl.gsu.by (кроме админских путей).
    3. Проверяет DLSID через внешний API (fetch_external_user_info).
    4. Автосоздаёт Django-пользователя через get_or_create_user_from_external.
    5. Привязывает сессию к подтверждённому пользователю (защита от stale-сессий).
    6. Кэширует user_info в сессии для повторных запросов.

    Результат работы доступен через request.user_info и request._ai_provisioned_user.
    """
    def __init__(self, get_response):
        self.get_response = get_response
        self.api_url = get_external_auth_api_url()
        self.session_cookie_name = get_external_session_cookie_name()
        self.redirect_url = os.getenv("EXTERNAL_AUTH_REDIRECT_URL", "https://dl.gsu.by")
        skip_paths = os.getenv("EXTERNAL_AUTH_SKIP_PATHS", "")
        self.skip_paths = self._build_skip_paths(skip_paths)
        self.cache_session_key = "external_session_id"
        self.cache_user_key = "external_user_info"
        self.cache_fetched_at_key = "external_user_info_fetched_at"
        # How long a cached user_info is trusted without revalidating the DLSID
        # against dl.gsu.by (seconds). Default 60s; 0 = always revalidate.
        try:
            self.auth_cache_ttl = int(os.getenv("AI_AUTH_CACHE_TTL", "60"))
        except (TypeError, ValueError):
            self.auth_cache_ttl = 60
        logger.info(f"Middleware init: skip_paths={self.skip_paths}")

    def _build_skip_paths(self, raw_paths: str) -> list[str]:
        default_paths = ["/health", "/ai/assets/", "/ai/api/groq-limits"]
        entries = [*default_paths, *[p.strip() for p in raw_paths.split(",") if p.strip()]]
        normalized = []
        for path in entries:
            candidate = _normalize_path(path)
            if candidate not in normalized:
                normalized.append(candidate)
        return normalized

    def _is_skipped_path(self, request_path: str) -> bool:
        normalized = _normalize_path(request_path)
        for path in self.skip_paths:
            if normalized == path or normalized.startswith(path + "/"):
                return True
        return False

    def _get_cached_user_info(self, request, session_id: str) -> dict | None:
        if not hasattr(request, "session"):
            return None
        cached_session_id = request.session.get(self.cache_session_key)
        if cached_session_id != session_id:
            return None
        cached_user_info = request.session.get(self.cache_user_key)
        if isinstance(cached_user_info, dict) and cached_user_info:
            return cached_user_info
        return None

    def _attach_cached_user_info(self, request) -> None:
        if not hasattr(request, "session"):
            return
        cached_user_info = request.session.get(self.cache_user_key)
        if isinstance(cached_user_info, dict) and cached_user_info:
            request.user_info = cached_user_info

    def _cached_fetched_at(self, request) -> float:
        """Unix-таймштамп последнего успешного fetch_external_user_info (0 если нет)."""
        if not hasattr(request, "session"):
            return 0.0
        try:
            return float(request.session.get(self.cache_fetched_at_key, 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    def _store_cached_user_info(self, request, session_id: str, user_info: dict) -> None:
        if not hasattr(request, "session"):
            return
        request.session[self.cache_session_key] = session_id
        request.session[self.cache_user_key] = user_info
        request.session[self.cache_fetched_at_key] = time.time()
        request.session.modified = True

    def _redirect_or_optional(self, request, request_path: str):
        """Return the response when the request is unauthenticated.

        Optional auth paths continue; everything else is redirected.
        """
        if _is_optional_auth_path(request_path):
            return self.get_response(request)
        return HttpResponseRedirect(self.redirect_url)

    def __call__(self, request):
        # Пропуск путей
        request_path = _normalize_path(request.path)
        if self._is_skipped_path(request_path):
            return self.get_response(request)

        # An already-authenticated Django session does NOT mean the
        # request is fresh — the DLSID chain might have changed (user
        # signed in as someone else on dl.gsu.by) or a stale session
        # cookie from a different account could be sitting in the
        # browser. Always revalidate against the external API and
        # rebind the local session to the user the API just confirmed.
        raw_session_id = request.COOKIES.get(self.session_cookie_name)
        if not raw_session_id:
            # No DLSID at all — anything other than admin entry points
            # redirects to dl.gsu.by.
            return self._redirect_or_optional(request, request_path)

        session_id = unquote(raw_session_id)
        logger.debug("Session ID decoded")

        try:
            cached_user_info = self._get_cached_user_info(request, session_id)
            fetched_at = self._cached_fetched_at(request)
            if cached_user_info and (time.time() - fetched_at) < self.auth_cache_ttl:
                # Свежий кэш (< TTL) — используем без повторной проверки DLSID.
                user_info = cached_user_info
            else:
                # Кэша нет либо он протух (> TTL) — ревалидируем DLSID через внешний API.
                user_info = fetch_external_user_info(session_id, api_url=self.api_url)
                self._store_cached_user_info(request, session_id, user_info)
            logger.debug("External user_info fetched (userId=%s)", (user_info or {}).get("userId"))
        except ExternalAuthUnauthorized:
            # DLSID is no longer valid — drop the local session and
            # bounce the user back to dl.gsu.by so they re-authenticate.
            from django.contrib.auth import logout as auth_logout
            if request.user.is_authenticated:
                auth_logout(request)
            return self._redirect_or_optional(request, request_path)
        except ExternalAuthMisconfigured as exc:
            logger.error(f"External auth misconfigured: {exc}")
            if _is_optional_auth_path(request_path):
                return self.get_response(request)
            return JsonResponse(
                {"error": "Authentication service misconfigured"},
                status=500,
            )
        except ExternalAuthUnavailable as exc:
            # dl.gsu.by недоступен — падаем на stale-кэш (graceful degradation):
            # пользуемся последним подтверждённым user_info, не закрывая доступ.
            logger.error(f"Request to external API failed: {exc}")
            cached_user_info = self._get_cached_user_info(request, session_id)
            if cached_user_info:
                user_info = cached_user_info
            elif _is_optional_auth_path(request_path):
                return self.get_response(request)
            else:
                return JsonResponse({"error": "Authentication service unavailable"}, status=503)

        request.user_info = user_info

        # Auto-provision user if needed. We do this for /ai/admin/...
        # paths too, even though the admin views have their own
        # permission checks: the admin's has_permission() verifies
        # that request.user matches the user the DLSID chain just
        # authenticated, and that rebind only works when we go all
        # the way through provisioning.
        try:
            user, created = get_or_create_user_from_external(user_info)
            if user:
                # Always rebind the session to the user the API just
                # confirmed. This is the only way to defend against
                # a stale Django session (e.g. a superuser from
                # yesterday) being reused under a different DLSID.
                current = getattr(request, "user", None)
                current_pk = getattr(current, "pk", None) if current is not None else None
                is_authed = bool(getattr(current, "is_authenticated", False))
                if not is_authed or current_pk != user.pk:
                    login(request, user, backend='django.contrib.auth.backends.ModelBackend')
                    csrf.rotate_token(request)
                    # Устанавливаем маркер свежей аутентификации для админки
                    request.session["admin_fresh_auth"] = True
                # Stash the freshly-provisioned user so downstream
                # code (notably AIAdminSite.has_permission) can verify
                # the request user matches the DLSID chain.
                request._ai_provisioned_user = user
                if created:
                    logger.info(f"New user provisioned: {user.username} (external_id={user_info.get('userId')})")
        except Exception as e:
            logger.exception(f"User provisioning failed: {e}")
            return JsonResponse({"error": "User provisioning failed"}, status=500)

        return self.get_response(request)
