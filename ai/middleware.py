import os
from urllib.parse import unquote

from dotenv import load_dotenv
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
    normalized = (path or "/").rstrip("/") or "/"
    return normalized == "/ai/admin" or normalized.startswith("/ai/admin/")


def _normalize_path(path: str | None) -> str:
    normalized = (path or "/").rstrip("/") or "/"
    return normalized


def _is_optional_auth_path(path: str) -> bool:
    normalized = _normalize_path(path)
    return normalized == "/ai/test-panel/login" or _is_admin_path(normalized)


class CsrfSessionFallbackMiddleware:
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
    def __init__(self, get_response):
        self.get_response = get_response
        load_dotenv()
        self.api_url = get_external_auth_api_url()
        self.session_cookie_name = get_external_session_cookie_name()
        self.redirect_url = os.getenv("EXTERNAL_AUTH_REDIRECT_URL", "https://dl.gsu.by")
        skip_paths = os.getenv("EXTERNAL_AUTH_SKIP_PATHS", "")
        self.skip_paths = self._build_skip_paths(skip_paths)
        self.cache_session_key = "external_session_id"
        self.cache_user_key = "external_user_info"
        logger.info(f"Middleware init: skip_paths={self.skip_paths}")

    def _build_skip_paths(self, raw_paths: str) -> list[str]:
        default_paths = ["/health", "/ai/assets/"]
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

    def _store_cached_user_info(self, request, session_id: str, user_info: dict) -> None:
        if not hasattr(request, "session"):
            return
        request.session[self.cache_session_key] = session_id
        request.session[self.cache_user_key] = user_info
        request.session.modified = True

    def __call__(self, request):
        # Пропуск путей
        request_path = _normalize_path(request.path)
        if self._is_skipped_path(request_path):
            return self.get_response(request)

        user = getattr(request, "user", None)
        if user is not None and getattr(user, "is_authenticated", False):
            self._attach_cached_user_info(request)
            return self.get_response(request)

        raw_session_id = request.COOKIES.get(self.session_cookie_name)
        if not raw_session_id:
            if _is_optional_auth_path(request_path):
                return self.get_response(request)
            return HttpResponseRedirect(self.redirect_url)

        session_id = unquote(raw_session_id)
        logger.debug("Session ID decoded")

        try:
            cached_user_info = self._get_cached_user_info(request, session_id)
            if cached_user_info:
                user_info = cached_user_info
            else:
                user_info = fetch_external_user_info(session_id, api_url=self.api_url)
                self._store_cached_user_info(request, session_id, user_info)
            logger.info(f"API response: {user_info}")
        except ExternalAuthUnauthorized:
            if _is_optional_auth_path(request_path):
                return self.get_response(request)
            return HttpResponseRedirect(self.redirect_url)
        except ExternalAuthMisconfigured as exc:
            logger.error(f"External auth misconfigured: {exc}")
            if _is_optional_auth_path(request_path):
                return self.get_response(request)
            return JsonResponse(
                {"error": "Authentication service misconfigured"},
                status=500,
            )
        except ExternalAuthUnavailable as exc:
            logger.error(f"Request to external API failed: {exc}")
            if _is_optional_auth_path(request_path):
                return self.get_response(request)
            return JsonResponse({'error': 'Authentication service unavailable'}, status=503)

        request.user_info = user_info

        if _is_admin_path(request.path):
            return self.get_response(request)
        
        # Auto-provision user if needed
        try:
            user, created = get_or_create_user_from_external(user_info)
            if user:
                # Avoid rotating CSRF/session on every request when already authenticated.
                if not request.user.is_authenticated or request.user.pk != user.pk:
                    login(request, user, backend='django.contrib.auth.backends.ModelBackend')
                    csrf.rotate_token(request)
                    # Устанавливаем маркер свежей аутентификации для админки
                    request.session["admin_fresh_auth"] = True
                if created:
                    logger.info(f"New user provisioned: {user.username} (external_id={user_info.get('userId')})")
        except Exception as e:
            logger.exception(f"User provisioning failed: {e}")
            return JsonResponse({'error': 'User provisioning failed'}, status=500)
        
        return self.get_response(request)
