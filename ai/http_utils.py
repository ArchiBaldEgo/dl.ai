"""Небольшие HTTP-хелперы для AI-приложения."""

from urllib.parse import unquote

from .external_auth import get_external_session_cookie_name


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