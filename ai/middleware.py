import os
from urllib.parse import unquote
from dotenv import load_dotenv
import requests
from django.conf import settings
from django.contrib.auth import login
from django.http import JsonResponse, HttpResponseRedirect
from django.middleware import csrf
from .external_account import get_or_create_user_from_external
import logging

logger = logging.getLogger(__name__)


def _is_admin_path(path):
    normalized = (path or "/").rstrip("/") or "/"
    return normalized == "/ai/admin" or normalized.startswith("/ai/admin/")


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
        self.api_url = os.getenv('EXTERNAL_AUTH_API_URL')
        self.session_cookie_name = os.getenv('EXTERNAL_SESSION_COOKIE_NAME', 'DLSID')
        self.redirect_url = os.getenv('EXTERNAL_AUTH_REDIRECT_URL', 'https://dl.gsu.by')
        skip_paths = os.getenv('EXTERNAL_AUTH_SKIP_PATHS', '')
        self.skip_paths = [p.strip() for p in skip_paths.split(',') if p.strip()]
        logger.info(f"Middleware init: skip_paths={self.skip_paths}")

    def __call__(self, request):
        # Пропуск путей
        request_path = (request.path or "/").rstrip("/") or "/"
        for path in self.skip_paths:
            normalized = (path or "/").rstrip("/") or "/"
            if request_path == normalized or request_path.startswith(normalized + '/'):
                return self.get_response(request)

        raw_session_id = request.COOKIES.get(self.session_cookie_name)
        if not raw_session_id:
            return HttpResponseRedirect(self.redirect_url)

        session_id = unquote(raw_session_id)
        logger.debug("Session ID decoded")

        try:
            response = requests.post(
                self.api_url,
                json={'sessionId': session_id, 'removeHtmlTags': True},
                verify=False,   #ssl 
                timeout=10     
            )
            if response.status_code == 401:
                return HttpResponseRedirect(self.redirect_url)
            response.raise_for_status()
            user_info = response.json()
            logger.info(f"API response: {user_info}")
        except requests.RequestException as e:
            logger.error(f"Request to external API failed: {e}")
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
