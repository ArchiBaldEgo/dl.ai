"""Внешняя аутентификация через dl.gsu.by REST API.

Проверяет DLSID-куку через POST-запрос к EXTERNAL_AUTH_API_URL
(по умолчанию https://dl.gsu.by/restapi/get-user-info).
Возвращает информацию о пользователе (userId, login, firstName, lastName).

Возможные ошибки:
- ExternalAuthMisconfigured: неверная конфигурация (нет URL, нет session_id).
- ExternalAuthUnauthorized: DLSID невалиден (HTTP 401).
- ExternalAuthUnavailable: API недоступен (сеть/таймаут/некорректный JSON).
"""

import os
from typing import Any

import requests

DEFAULT_EXTERNAL_AUTH_API_URL = "https://dl.gsu.by/restapi/get-user-info"
DEFAULT_EXTERNAL_SESSION_COOKIE_NAME = "DLSID"


class ExternalAuthError(RuntimeError):
    """Базовая ошибка внешней аутентификации."""
    pass


class ExternalAuthMisconfigured(ExternalAuthError):
    """Ошибка конфигурации: нет URL или session_id."""
    pass


class ExternalAuthUnavailable(ExternalAuthError):
    """API недоступен: сеть, таймаут, некорректный JSON."""
    pass


class ExternalAuthUnauthorized(ExternalAuthError):
    """DLSID невалиден — требуется повторная аутентификация на dl.gsu.by."""
    pass


def get_external_auth_api_url() -> str:
    """Возвращает URL внешнего API аутентификации (из env или по умолчанию)."""
    raw = os.getenv("EXTERNAL_AUTH_API_URL")
    if raw is None:
        return DEFAULT_EXTERNAL_AUTH_API_URL
    value = raw.strip()
    return value or DEFAULT_EXTERNAL_AUTH_API_URL


def get_external_session_cookie_name() -> str:
    """Возвращает имя куки сессии dl.gsu.by (DLSID по умолчанию)."""
    raw = os.getenv("EXTERNAL_SESSION_COOKIE_NAME")
    if raw:
        value = raw.strip()
        if value:
            return value
    return DEFAULT_EXTERNAL_SESSION_COOKIE_NAME


def fetch_external_user_info(
    session_id: str,
    *,
    api_url: str | None = None,
    timeout: int = 10,
) -> dict[str, Any]:
    """Запрашивает информацию о пользователе у внешнего API dl.gsu.by по DLSID.

    POST на EXTERNAL_AUTH_API_URL с {sessionId, removeHtmlTags: true}.
    Возвращает JSON-ответ (userId, login, firstName, lastName, ...).
    """
    if not session_id:
        raise ExternalAuthMisconfigured("session_id is required")
    resolved_api_url = (api_url or get_external_auth_api_url()).strip()
    if not resolved_api_url:
        raise ExternalAuthMisconfigured("EXTERNAL_AUTH_API_URL is empty")
    
    # SSL verification can be disabled via environment variable for development
    verify_ssl = not os.getenv("SKIP_SSL_VERIFICATION", "").lower() in ("1", "true")
    disable_proxy = os.getenv("EXTERNAL_AUTH_DISABLE_PROXY", "").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    proxies = {"http": None, "https": None} if disable_proxy else None
    
    try:
        request_kwargs = {
            "json": {"sessionId": session_id, "removeHtmlTags": True},
            "verify": verify_ssl,
            "timeout": timeout,
        }
        if proxies is not None:
            request_kwargs["proxies"] = proxies
        response = requests.post(resolved_api_url, **request_kwargs)
    except requests.RequestException as exc:
        raise ExternalAuthUnavailable(str(exc)) from exc

    if response.status_code == 401:
        raise ExternalAuthUnauthorized("External auth unauthorized")

    try:
        response.raise_for_status()
    except requests.RequestException as exc:
        raise ExternalAuthUnavailable(
            f"External auth returned status {response.status_code}"
        ) from exc

    try:
        return response.json()
    except ValueError as exc:
        raise ExternalAuthUnavailable("External auth returned invalid JSON") from exc
