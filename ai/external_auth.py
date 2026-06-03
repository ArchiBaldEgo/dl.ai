import os
from typing import Any

import requests

DEFAULT_EXTERNAL_AUTH_API_URL = "https://dl.gsu.by/restapi/get-user-info"
DEFAULT_EXTERNAL_SESSION_COOKIE_NAME = "DLSID"


class ExternalAuthError(RuntimeError):
    pass


class ExternalAuthMisconfigured(ExternalAuthError):
    pass


class ExternalAuthUnavailable(ExternalAuthError):
    pass


class ExternalAuthUnauthorized(ExternalAuthError):
    pass


def get_external_auth_api_url() -> str:
    raw = os.getenv("EXTERNAL_AUTH_API_URL")
    if raw is None:
        return DEFAULT_EXTERNAL_AUTH_API_URL
    value = raw.strip()
    return value or DEFAULT_EXTERNAL_AUTH_API_URL


def get_external_session_cookie_name() -> str:
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
    if not session_id:
        raise ExternalAuthMisconfigured("session_id is required")
    resolved_api_url = (api_url or get_external_auth_api_url()).strip()
    if not resolved_api_url:
        raise ExternalAuthMisconfigured("EXTERNAL_AUTH_API_URL is empty")
    try:
        response = requests.post(
            resolved_api_url,
            json={"sessionId": session_id, "removeHtmlTags": True},
            verify=False,
            timeout=timeout,
        )
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
