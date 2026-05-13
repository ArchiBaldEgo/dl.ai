import os
from urllib.parse import unquote
from dotenv import load_dotenv
import requests
from django.http import JsonResponse

class ExternalAuthMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        load_dotenv()
        self.api_url = os.getenv('EXTERNAL_AUTH_API_URL')
        self.session_cookie_name = os.getenv('EXTERNAL_SESSION_COOKIE_NAME', 'DLSID')
        skip_paths = os.getenv('EXTERNAL_AUTH_SKIP_PATHS', '')
        self.skip_paths = [p.strip() for p in skip_paths.split(',') if p.strip()]
        # Для отладки
        print(f"Middleware init: skip_paths={self.skip_paths}")

    def __call__(self, request):
        # Пропускаем пути (учитываем и /health, и /health/)
        for path in self.skip_paths:
            if request.path == path or request.path.startswith(path.rstrip('/') + '/'):
                return self.get_response(request)

        raw_session_id = request.COOKIES.get(self.session_cookie_name)
        if not raw_session_id:
            return JsonResponse({'error': 'Unauthorized: missing session cookie'}, status=401)

        session_id = unquote(raw_session_id)  # декодируем %7B...%7D
        print(f"Session ID decoded: {session_id}")  # временно

        try:
            response = requests.post(
                self.api_url,
                json={'sessionId': session_id, 'removeHtmlTags': True},
                verify=False,   # <-- обязательно
                timeout=10      # добавим таймаут на всякий случай
            )
            if response.status_code == 401:
                return JsonResponse({'error': 'Unauthorized: invalid or expired session'}, status=401)
            response.raise_for_status()
            user_info = response.json()
        except requests.RequestException as e:
            print(f"Request failed: {e}")  # увидим реальную ошибку
            return JsonResponse({'error': 'Authentication service unavailable'}, status=503)

        request.user_info = user_info
        return self.get_response(request)