"""Небольшие HTTP-хелперы для AI-приложения."""

from urllib.parse import unquote

from django.http import HttpResponseForbidden, JsonResponse
from django.views.csrf import csrf_failure

from .constants import CHECK_AI_ADMIN_PATH
from .external_auth import get_external_session_cookie_name


def csrf_failure_view(request, reason=""):
    if request.path_info.startswith(CHECK_AI_ADMIN_PATH):
        message = "Не удалось подтвердить отправку формы. Откройте страницу заново и повторите проверку."
        if request.GET.get("format") == "json" or request.headers.get("Accept") == "application/json":
            return JsonResponse({"error": message}, status=403)
        return HttpResponseForbidden(message)
    return csrf_failure(request, reason=reason)


def safe_relative_url(candidate, fallback):
    """Возвращает относительный URL, если он безопасен, иначе fallback.

    Защита от open-redirect: принимает только пути, начинающиеся с '/'
    и не начинающиеся с '//' (протокол-относительный URL).
    """
    value = (candidate or "").strip()
    if value.startswith("/") and not value.startswith("//"):
        return value
    return fallback


def resolve_dl_session_id(request) -> str:
    """Извлечь session_id DLSID-сессии из кук запроса.

    Единственная точка чтения куки dl.gsu.by для DL API-прокси
    (имя куки — через ``get_external_session_cookie_name()``, значение
    проходит ``unquote`` так же, как в ``ExternalAuthMiddleware``).
    Возвращает "" если куки нет.
    """
    cookie_name = get_external_session_cookie_name()
    return unquote(request.COOKIES.get(cookie_name, "").strip())
