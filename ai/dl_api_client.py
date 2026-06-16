"""Thin client for the external dl.gsu.by REST API.

Provides helpers to fetch task information and sample solutions.
Reuses the same SSL/proxy settings as the external auth flow.
"""

import json
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


def _looks_like_utf8_as_cp1251(text: str) -> bool:
    """Detect the classic 'UTF-8 bytes decoded as cp1251' mojibake pattern."""
    import re

    # When UTF-8 Cyrillic bytes are decoded as cp1251, the leading bytes of
    # 2-byte sequences (0xD0-0xDF) become uppercase Р-Я, and the leading bytes
    # of 3-byte sequences (0xE0-0xEF) become lowercase а-п.
    lead_chars = "РСТУФХЦЧШЩЪЫЬЭЮЯабвгдежзийклмноп"
    # Look for a lead char immediately followed by any non-ASCII char
    # (the continuation byte decoded as cp1251).
    pattern = re.compile(f"[{lead_chars}][^\\x00-\\x7f]")
    matches = len(pattern.findall(text))
    if matches < 3:
        return False
    cyrillic = sum(1 for c in text if "Ѐ" <= c <= "ӿ")
    return matches / max(cyrillic, 1) > 0.5


def _looks_like_cp866_as_cp1251(text: str) -> bool:
    """Detect CP866 bytes that were decoded/presented as cp1251 codepoints.

    The resulting text is encodable as cp1251, but contains many characters
    from the cp1251 high-byte range (bytes 0x80-0xBF) that are not part of
    ordinary Russian text. Legitimate Russian punctuation like guillemets,
    dashes and the letter ё are ignored so they do not trigger a false repair.
    """
    try:
        text.encode("cp1251")
    except UnicodeEncodeError:
        return False

    non_ascii = sum(1 for c in text if c > "")
    if non_ascii == 0:
        return False

    allowed_punctuation = set("–—«»„\"\"‘’‚‹›")
    artifact_count = 0
    for c in text:
        if c <= "" or c == "ё" or c in allowed_punctuation:
            continue
        b = c.encode("cp1251")[0]
        if 0x80 <= b <= 0xBF:
            artifact_count += 1

    return artifact_count >= 5 and artifact_count / non_ascii > 0.3


def _try_repair_cp866_as_cp1251(text: str) -> str | None:
    """Try to repair text that is CP866 bytes presented as cp1251 codepoints.

    Some PDF statements on dl.gsu.by extract Russian text using what looks
    like CP866 glyphs, but the API returns those bytes interpreted as cp1251
    Unicode characters. Re-encoding as cp1251 and decoding as cp866 recovers
    the original Cyrillic.
    """
    try:
        return text.encode("cp1251").decode("cp866")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return None


def _repair_response_strings(obj: Any) -> Any:
    """Recursively repair CP866-via-cp1251 mojibake in decoded JSON values."""
    if isinstance(obj, dict):
        return {k: _repair_response_strings(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_repair_response_strings(v) for v in obj]
    if isinstance(obj, str) and _looks_like_cp866_as_cp1251(obj):
        return _try_repair_cp866_as_cp1251(obj) or obj
    return obj


def _decode_response_json(response: requests.Response) -> dict[str, Any]:
    """Decode DL API JSON response with robust encoding detection.

    Tries UTF-8 first, falls back to cp1251, repairs the common
    'UTF-8 bytes decoded as cp1251' mojibake pattern, and also tries the
    CP866-via-cp1251 repair used by some PDF-derived statements.
    """
    content = response.content
    candidates: list[str] = []

    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    else:
        candidates.append(text)
        if _looks_like_utf8_as_cp1251(text):
            try:
                fixed = text.encode("cp1251").decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass
            else:
                candidates.append(fixed)

    try:
        candidates.append(content.decode("cp1251", errors="replace"))
    except UnicodeDecodeError:
        pass

    # Add the CP866-via-cp1251 repair candidate when the text shows the
    # signature of this particular mojibake pattern. Quality scoring then
    # selects the best readable version.
    repaired = [
        fixed
        for candidate in candidates
        if _looks_like_cp866_as_cp1251(candidate)
        and (fixed := _try_repair_cp866_as_cp1251(candidate)) is not None
    ]
    candidates.extend(repaired)

    if not candidates:
        raise DLServerError("Не удалось декодировать ответ DL API")

    def _quality(t: str) -> float:
        import re

        cyrillic = sum(1 for c in t if "Ѐ" <= c <= "ӿ")
        latin = sum(1 for c in t if c.isascii() and c.isalpha())
        digits = sum(1 for c in t if c.isdigit())
        spaces = sum(1 for c in t if c.isspace())
        punctuation = sum(
            1
            for c in t
            if c in ".,;:!?()[]{}\"'–—-=+/*_…«»"
        )
        # Penalize the UTF-8-as-cp1251 mojibake signature pattern.
        lead_chars = "РСТУФХЦЧШЩЪЫЬЭЮЯабвгдежзийклмноп"
        mojibake_pattern = re.compile(f"[{lead_chars}][^\\x00-\\x7f]")
        mojibake_matches = len(mojibake_pattern.findall(t))
        # Penalize stray high-byte symbols typical of mojibake.
        suspicious = sum(
            1
            for c in t
            if c > "" and not ("Ѐ" <= c <= "ӿ" or c in "–—«»…\"'")
        )
        return (
            cyrillic * 2
            + latin
            + digits
            + spaces
            + punctuation
            - mojibake_matches * 3
            - suspicious * 2
        )

    text = max(candidates, key=_quality)

    try:
        data = json.loads(text)
    except ValueError as exc:
        raise DLServerError("DL API вернул некорректный JSON") from exc

    # If the whole-response repair did not fire (for example because a non-cp1251
    # character appeared elsewhere in the payload), repair individual string
    # fields that still carry the CP866-via-cp1251 signature.
    return _repair_response_strings(data)


def fetch_task_info(
    node_id: int,
    session_id: str | None = None,
    remove_html_tags: bool = True,
) -> dict[str, Any]:
    """Fetch task metadata (name, taskId, statement) by nodeId.

    The external DL API needs the caller's session to authorize the request,
    so ``session_id`` is forwarded as the ``sessionId`` query parameter.

    Raises:
        DLUnauthorizedError: when the session is missing/invalid (401).
        DLForbiddenError: when the user cannot access this task (403).
        DLTaskNotFoundError: when the task does not exist (404).
        DLApiUnavailable: when the DL API cannot be reached.
        DLServerError: on unexpected 5xx responses.
    """
    params: dict[str, Any] = {
        "nodeId": node_id,
        "removeHtmlTags": remove_html_tags,
    }
    if session_id:
        params["sessionId"] = session_id

    response = _dl_request(
        "GET",
        "/restapi/get-task-info",
        params=params,
    )

    _raise_for_status(response)

    return _decode_response_json(response)


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

    return _decode_response_json(response)
