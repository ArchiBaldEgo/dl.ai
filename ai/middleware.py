import os
from urllib.parse import unquote
from dotenv import load_dotenv
import requests
from django.http import JsonResponse
from django.contrib.auth import login
from .external_account import get_or_create_user_from_external
import logging

logger = logging.getLogger(__name__)

class ExternalAuthMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        load_dotenv()
        self.api_url = os.getenv('EXTERNAL_AUTH_API_URL')
        self.session_cookie_name = os.getenv('EXTERNAL_SESSION_COOKIE_NAME', 'DLSID')
        skip_paths = os.getenv('EXTERNAL_AUTH_SKIP_PATHS', '')
        self.skip_paths = [p.strip() for p in skip_paths.split(',') if p.strip()]
        print(f"Middleware init: skip_paths={self.skip_paths}")

    def __call__(self, request):
        # Пропуск путей
        for path in self.skip_paths:
            if request.path == path or request.path.startswith(path.rstrip('/') + '/'):
                return self.get_response(request)

        raw_session_id = request.COOKIES.get(self.session_cookie_name)
        if not raw_session_id:
            return JsonResponse({'error': 'Unauthorized: missing session cookie'}, status=401)

        session_id = unquote(raw_session_id)  
        print(f"Session ID decoded: {session_id}") 

        try:
            response = requests.post(
                self.api_url,
                json={'sessionId': session_id, 'removeHtmlTags': True},
                verify=False,   #ssl 
                timeout=10     
            )
            if response.status_code == 401:
                return JsonResponse({'error': 'Unauthorized: invalid or expired session'}, status=401)
            response.raise_for_status()
            user_info = response.json()
        except requests.RequestException as e:
            logger.error(f"Request to external API failed: {e}")
            return JsonResponse({'error': 'Authentication service unavailable'}, status=503)

        request.user_info = user_info
        
        # Auto-provision user if needed
        try:
            user, created = get_or_create_user_from_external(user_info)
            if user:
                # Login user for web interface access
                login(request, user, backend='django.contrib.auth.backends.ModelBackend')
                if created:
                    logger.info(f"New user provisioned: {user.username} (external_id={user_info.get('userId')})")
        except Exception as e:
            logger.exception(f"User provisioning failed: {e}")
            return JsonResponse({'error': 'User provisioning failed'}, status=500)
        
        return self.get_response(request)