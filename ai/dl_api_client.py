"""Thin client for the external dl.gsu.by REST API.

Provides helpers to fetch task information and sample solutions.
Reuses the same SSL/proxy settings as the external auth flow.
"""

import os
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

from .external_auth import get_external_auth_api_url


class DLApiError(RuntimeError):
    """Base error for DL REST API calls."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class DLTaskNotFoundError(DLApiError):
    """Task for the requested nodeId was not found (HTTP 404)."""

    def __init__(self, message: str = "Задача не найдена"):
        super().__init__(message, status_code=404)


class DLUnauthorizedError(DLApiError):
    """Session is missing or invalid (HTTP 401)."""

    def __init__(self, message: str = "Не авторизован"):
        super().__init__(message, status_code=401)


class DLForbiddenError(DLApiError):
    """User cannot use AI for this task (HTTP 403)."""

    def __init__(self, message: str = "Доступ запрещён"):
        super().__init__(message, status_code=403)


class DLServerError(DLApiError):
    """External DL API returned a server error (HTTP 5xx)."""

    def __init__(self, message: str = "Ошибка сервера DL"):
        super().__init__(message, status_code=500)


class DLApiUnavailable(DLApiError):
    """Could not reach the external DL API (network/DNS/proxy issue)."""

    def __init__(self, message: str = "DL API недоступен"):
        super().__init__(message, status_code=503)


def _get_dl_base_url() -> str:
    """Return the base URL of the external DL site.

    Derived from EXTERNAL_AUTH_API_URL so that local/test environments can
    point the whole integration at a different host.
    """
    auth_url = get_external_auth_api_url()
    parsed = urlparse(auth_url)
    scheme = parsed.scheme or "https"
    netloc = parsed.netloc
    if not netloc:
        return "https://dl.gsu.by"
    return f"{scheme}://{netloc}"


def _get_verify_ssl() -> bool:
    return not os.getenv("SKIP_SSL_VERIFICATION", "").lower() in ("1", "true")


def _get_proxies() -> dict[str, None] | None:
    disable_proxy = os.getenv("EXTERNAL_AUTH_DISABLE_PROXY", "").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    return {"http": None, "https": None} if disable_proxy else None


def _dl_request(method: str, path: str, **kwargs) -> requests.Response:
    """Make a request to the DL REST API with shared SSL/proxy settings."""
    base_url = _get_dl_base_url()
    url = urljoin(base_url, path)
    verify_ssl = _get_verify_ssl()
    proxies = _get_proxies()

    request_kwargs = {
        "verify": verify_ssl,
        "timeout": kwargs.pop("timeout", 30),
    }
    if proxies is not None:
        request_kwargs["proxies"] = proxies
    request_kwargs.update(kwargs)

    try:
        response = requests.request(method, url, **request_kwargs)
    except requests.RequestException as exc:
        raise DLApiUnavailable(f"Не удалось связаться с DL API: {exc}") from exc

    return response


def _raise_for_status(response: requests.Response) -> None:
    """Map common DL API error statuses to typed exceptions."""
    if response.status_code == 401:
        raise DLUnauthorizedError()
    if response.status_code == 403:
        raise DLForbiddenError()
    if response.status_code == 404:
        raise DLTaskNotFoundError()
    if response.status_code >= 500:
        raise DLServerError(f"Ошибка сервера DL (код {response.status_code})")


def fetch_task_info(node_id: int, remove_html_tags: bool = True) -> dict[str, Any]:
    """Fetch task metadata (name, taskId, statement) by nodeId.

    Raises:
        DLTaskNotFoundError: when the task does not exist (404).
        DLApiUnavailable: when the DL API cannot be reached.
        DLServerError: on unexpected 5xx responses.
    """
    response = _dl_request(
        "GET",
        "/restapi/get-task-info",
        params={
            "nodeId": node_id,
            "removeHtmlTags": remove_html_tags,
        },
    )

    _raise_for_status(response)

    try:
        return response.json()
    except ValueError as exc:
        raise DLServerError("DL API вернул некорректный JSON") from exc


def fetch_task_solution(session_id: str, task_id: int, file_extension: str) -> dict[str, Any]:
    """Fetch sample solution file contents for a task.

    Raises:
        DLUnauthorizedError: when the session is missing/invalid (401).
        DLForbiddenError: when the user cannot use AI for this task (403).
        DLTaskNotFoundError: when the solution file was not found (404).
        DLApiUnavailable: when the DL API cannot be reached.
        DLServerError: on unexpected 5xx responses.
    """
    response = _dl_request(
        "POST",
        "/restapi/get-solution",
        json={
            "sessionId": session_id,
            "taskId": task_id,
            "fileExtension": file_extension,
        },
    )

    _raise_for_status(response)

    try:
        return response.json()
    except ValueError as exc:
        raise DLServerError("DL API вернул некорректный JSON") from exc
