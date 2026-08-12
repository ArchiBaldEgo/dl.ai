"""Клиент для внешнего REST API dl.gsu.by.

Предоставляет функции для:
- Получения информации о задачах (fetch_task_info).
- Получения образцовых решений (fetch_task_solution).
- Отправки решений на проверку (send_solution_to_dl, get_solution_result_from_dl).
- Получения списка решений (get_solutions_from_dl).
- Получения имён пользователей (fetch_user_names).
- Навигации по дереву задач: корневые ноды курса (fetch_course_nodes),
  список задач в папке (fetch_node_tasks), вложенное дерево (fetch_node_tree),
  плоское извлечение задач из дерева (flatten_node_tree).

Содержит робустную логику декодирования ответов: автодетект mojibake
(UTF-8-as-cp1251, CP866-via-cp1251) и восстановление корректного текста.
Все ошибки обёрнуты в типизированные исключения (DLApiError и подклассы).
"""

import json
import os
import re
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


def _body_snippet(response: requests.Response, limit: int = 300) -> str:
    """Best-effort short snippet of a response body for error messages.

    Used when DL returns a non-JSON body (HTML error page, empty body, nginx 413
    page, login redirect, …) so the operator sees what actually came back instead
    of a bare «некорректный JSON». Whitespace is collapsed; the snippet is bounded.
    """
    try:
        content = response.content
    except Exception:
        return ""
    if not content:
        return " (пустой ответ)"
    try:
        text = content.decode("utf-8", errors="replace")
    except Exception:
        text = str(content[:limit])
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[:limit] + "…"
    return f": {text}"


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
    if response.status_code >= 400:
        # Прочие 4xx (400/405/413/422/…): DL вернул ошибку запроса, тело часто
        # HTML (страница nginx/Spring/WAF), а не JSON. Поднимаем ошибку с фрагментом
        # тела, чтобы не пытаться парсить HTML как JSON в _decode_response_json.
        raise DLServerError(f"DL API вернул ошибку (код {response.status_code}){_body_snippet(response)}")


# Map every Unicode character that exists in the cp1251 code page back to
# its byte value, and build the set of characters that correspond to the
# UTF-8 continuation byte range (0x80-0xBF). These are used to detect and
# reverse the CP866-via-cp1251 mojibake pattern where CP866 glyphs were
# interpreted as cp1251 bytes.
_CP1251_UNICODE_TO_BYTE: dict[str, int] = {}
_CP1251_CONTINUATION_CHARS: set[str] = set()
for _b in range(256):
    try:
        _ch = bytes([_b]).decode("cp1251")
        _CP1251_UNICODE_TO_BYTE[_ch] = _b
        if 0x80 <= _b <= 0xBF:
            _CP1251_CONTINUATION_CHARS.add(_ch)
    except UnicodeDecodeError:
        pass


# Leading cp1251 characters produced by UTF-8 leading bytes 0xD0-0xDF
# (uppercase Р-Я) and 0xE0-0xEF (lowercase а-п).
_UTF8_LEAD_CHARS: set[str] = set("РСТУФХЦЧШЩЪЫЬЭЮЯабвгдежзийклмноп")


def _looks_like_utf8_as_cp1251(text: str) -> bool:
    """Detect the classic 'UTF-8 bytes decoded as cp1251' mojibake pattern."""
    # A UTF-8 2-byte sequence starts with a lead byte (cp1251 becomes one of
    # the lead chars) followed by a continuation byte (0x80-0xBF). We only
    # count a lead/continuation pair when the second character actually maps to
    # a cp1251 byte in the continuation range, so ordinary Cyrillic text is
    # not flagged.
    matches = 0
    for i in range(len(text) - 1):
        if text[i] in _UTF8_LEAD_CHARS and text[i + 1] in _CP1251_CONTINUATION_CHARS:
            matches += 1
    if matches < 3:
        return False
    cyrillic = sum(1 for c in text if "Ѐ" <= c <= "ӿ")
    return matches / max(cyrillic, 1) > 0.5


def _looks_like_cp866_as_cp1251(text: str) -> bool:
    """Detect CP866 bytes that were decoded/presented as cp1251 codepoints.

    The text consists of cp1251 codepoints, but contains many characters from
    the cp1251 high-byte range (bytes 0x80-0xBF) that are not part of ordinary
    Russian text. Legitimate Russian punctuation like guillemets, dashes and
    the letter ё are ignored so they do not trigger a false repair.
    """
    non_ascii = sum(1 for c in text if c > "")
    if non_ascii == 0:
        return False

    allowed_punctuation = set("–—«»„\"\"‘’‚‹›")
    artifact_count = 0
    for c in text:
        if c <= "" or c == "ё" or c == "\xa0" or c in allowed_punctuation:
            continue
        b = _CP1251_UNICODE_TO_BYTE.get(c)
        if b is not None and 0x80 <= b <= 0xBF:
            artifact_count += 1

    return artifact_count >= 5 and artifact_count / non_ascii > 0.3


def _repair_cp866_via_cp1251(text: str) -> str:
    """Repair CP866 bytes that were presented as cp1251 codepoints.

    Some PDF statements on dl.gsu.by extract Russian text using CP866 glyphs,
    but the API returns those bytes interpreted as cp1251 Unicode characters.
    For each character we map it back to the original cp1251 byte, then decode
    that byte as CP866. Characters that do not exist in cp1251 are kept as-is;
    Unicode replacement characters are dropped.
    """
    out = []
    for c in text:
        if c == chr(0xFFFD):
            continue
        if c <= "":
            out.append(c)
            continue
        b = _CP1251_UNICODE_TO_BYTE.get(c)
        if b is not None:
            out.append(bytes([b]).decode("cp866"))
        else:
            out.append(c)
    return "".join(out)


def _quality(t: str) -> float:
    """Score readability of a candidate decoded response.

    Higher is better. Rewards Cyrillic, Latin letters, digits, spaces and
    common punctuation; penalizes the UTF-8-as-cp1251 mojibake signature and
    stray high-byte symbols typical of mojibake.
    """
    cyrillic = sum(1 for c in t if "Ѐ" <= c <= "ӿ")
    latin = sum(1 for c in t if c.isascii() and c.isalpha())
    digits = sum(1 for c in t if c.isdigit())
    spaces = sum(1 for c in t if c.isspace())
    punctuation = sum(
        1
        for c in t
        if c in ".,;:!?()[]{}\"'–—-=+/*_…«»"
    )
    # Penalize the UTF-8-as-cp1251 mojibake signature pattern: a UTF-8 lead
    # char followed by a character that maps to a cp1251 continuation byte.
    mojibake_matches = 0
    for i in range(len(t) - 1):
        if t[i] in _UTF8_LEAD_CHARS and t[i + 1] in _CP1251_CONTINUATION_CHARS:
            mojibake_matches += 1
    # Penalize the nbsp-mojibake pair: a UTF-8 non-breaking space is bytes
    # [0xC2, 0xA0]; when these are decoded as cp1251 they become В (Cyrillic
    # В) + nbsp. Without this penalty the cp1251 candidate outscores the
    # correct UTF-8 candidate (В gets the +2 Cyrillic bonus) for English text
    # that uses nbsp for spacing, and a later CP866 repair turns В+nbsp into
    # ┬а ("...┬аOlympiad┬а...").
    nbsp_mojibake = 0
    for i in range(len(t) - 1):
        if t[i] == "В" and t[i + 1] == "\xa0":
            nbsp_mojibake += 1
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
        - nbsp_mojibake * 3
        - suspicious * 2
    )


def _repair_response_strings(obj: Any) -> Any:
    """Recursively repair CP866-via-cp1251 mojibake in decoded JSON values.

    Individual string values are repaired only when they carry the
    CP866-via-cp1251 signature. This avoids corrupting legitimate Cyrillic
    fields that may sit next to corrupted PDF-derived fields in the same
    response.
    """
    if isinstance(obj, dict):
        return {k: _repair_response_strings(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_repair_response_strings(v) for v in obj]
    if isinstance(obj, str) and _looks_like_cp866_as_cp1251(obj):
        return _repair_cp866_via_cp1251(obj)
    return obj


def _decode_response_json(response: requests.Response) -> dict[str, Any]:
    """Decode DL API JSON response with robust encoding detection.

    Tries UTF-8 first, falls back to cp1251, repairs the common
    'UTF-8 bytes decoded as cp1251' mojibake pattern, then repairs individual
    JSON string values that show the CP866-via-cp1251 signature used by some
    PDF-derived statements.
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

    if not candidates:
        raise DLServerError("Не удалось декодировать ответ DL API")

    text = max(candidates, key=_quality)

    try:
        data = json.loads(text)
    except ValueError as exc:
        # DL вернул 2xx, но тело не JSON (HTML-страница, пустой ответ, redirect на
        # логин, WAF-блок…). Без статуса и фрагмента тела ошибку невозможно
        # диагностировать — поднимаем с контекстом.
        raise DLServerError(
            f"DL API вернул некорректный JSON (код {response.status_code}){_body_snippet(response)}"
        ) from exc

    # Repair corrupted string fields individually so that a clean Cyrillic
    # field next to a corrupted statement is not damaged.
    return _repair_response_strings(data)


def fetch_user_names(user_id: int | str, timeout: int = 10) -> dict[str, str]:
    """Fetch firstName / lastName by userId from the DL REST API.

    GET /restapi/get-id-user-info?userId=<id> → {"firstName": "...", "lastName": "..."}

    Returns a dict with "first_name" and "last_name" keys (empty strings on
    failure). Never raises — callers use this as a best-effort enrichment step
    during user provisioning.
    """
    if not user_id:
        return {"first_name": "", "last_name": ""}

    try:
        response = _dl_request(
            "GET",
            "/restapi/get-id-user-info",
            params={"userId": str(user_id)},
            timeout=timeout,
        )
    except DLApiUnavailable:
        return {"first_name": "", "last_name": ""}

    if response.status_code != 200:
        return {"first_name": "", "last_name": ""}

    try:
        data = response.json()
    except ValueError:
        return {"first_name": "", "last_name": ""}

    return {
        "first_name": (data.get("firstName") or "").strip(),
        "last_name": (data.get("lastName") or "").strip(),
    }


def fetch_task_info(
    node_id: int,
    session_id: str | None = None,
    remove_html_tags: bool | None = True,
    course_id: int | None = None,
) -> dict[str, Any]:
    """Fetch task metadata (name, taskId, statement, path) by nodeId.

    The external DL API needs the caller's session to authorize the request,
    so ``session_id`` is forwarded as the ``sessionId`` query parameter.

    ``remove_html_tags`` controls the ``removeHtmlTags`` query parameter:
    ``True``/``False`` send it as-is; ``None`` omits the parameter so DL
    returns its default (raw, non-stripped) response — used as a fallback when
    DL's own stripping yields an empty ``statement``.

    ``course_id`` (optional) enables the ``courseId`` query parameter: when
    set to a valid course, DL includes the ``path`` field — the full path to
    the task in the course tree (e.g. "Программирование\\...\\Сложение").

    Raises:
        DLUnauthorizedError: when the session is missing/invalid (401).
        DLForbiddenError: when the user cannot access this task (403).
        DLTaskNotFoundError: when the task does not exist (404).
        DLApiUnavailable: when the DL API cannot be reached.
        DLServerError: on unexpected 5xx responses.
    """
    params: dict[str, Any] = {
        "nodeId": node_id,
    }
    if remove_html_tags is not None:
        params["removeHtmlTags"] = remove_html_tags
    if session_id:
        params["sessionId"] = session_id
    if course_id is not None:
        params["courseId"] = course_id

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


def send_solution_to_dl(
    session_id: str,
    node_id: int,
    code: str,
    file_extension: str,
) -> dict[str, Any]:
    """Submit code to DL for automated testing.

    POST /restapi/send-solution → {"queueId": N, "message": "..."}

    Returns the raw JSON response. Raises DLUnauthorizedError (401),
    DLServerError (500), DLApiUnavailable on network failure.
    """
    response = _dl_request(
        "POST",
        "/restapi/send-solution",
        json={
            "sessionId": session_id,
            "nodeId": node_id,
            "code": code,
            "fileExtension": file_extension,
        },
    )

    _raise_for_status(response)
    return _decode_response_json(response)


def get_solution_result_from_dl(
    session_id: str,
    queue_id: int,
) -> dict[str, Any]:
    """Poll DL for the result of a submitted solution.

    POST /restapi/get-solution-result → {"isFinished": bool, "comment": "..."}

    Returns the raw JSON response. Raises DLUnauthorizedError (401),
    DLTaskNotFoundError (404), DLServerError (500), DLApiUnavailable.
    """
    response = _dl_request(
        "POST",
        "/restapi/get-solution-result",
        json={
            "sessionId": session_id,
            "queueId": queue_id,
        },
    )

    _raise_for_status(response)
    return _decode_response_json(response)


def get_solutions_from_dl(
    session_id: str,
    course_id: int,
    node_id: int,
    *,
    extension: str = "",
    include_correct: bool = True,
    include_incorrect: bool = True,
    start_date: str = "",
    end_date: str = "",
) -> dict[str, Any]:
    """Fetch list of solutions for a task from DL.

    POST /restapi/get-solutions → {"solutions": [{queueId, userId, code, result, report, isCorrect}, ...]}

    Regular users get only their own solutions; admins/editors get all.

    ``extension`` is the file extension WITHOUT a leading dot (e.g. "cpp",
    "pas", "java"); when empty, solutions with any extension are returned.
    Dates use the 'YYYY-MM-DD' or 'YYYY-MM-DD HH:mm:ss' format.
    """
    payload: dict[str, Any] = {
        "sessionId": session_id,
        "courseId": course_id,
        "nodeId": node_id,
        "includeCorrect": include_correct,
        "includeIncorrect": include_incorrect,
    }
    if extension:
        payload["extension"] = extension
    if start_date:
        payload["startDate"] = start_date
    if end_date:
        payload["endDate"] = end_date

    response = _dl_request(
        "POST",
        "/restapi/get-solutions",
        json=payload,
    )

    _raise_for_status(response)
    return _decode_response_json(response)


def fetch_course_nodes(session_id: str, course_id: int) -> dict[str, Any]:
    """Fetch root node IDs (tasks + theory) for a course.

    POST /restapi/get-course-node → {"tasksRootId": N, "theoryRootId": N}

    Raises DLUnauthorizedError (401), DLForbiddenError (403), DLServerError (500).
    """
    response = _dl_request(
        "POST",
        "/restapi/get-course-node",
        json={
            "sessionId": session_id,
            "courseId": course_id,
        },
    )

    _raise_for_status(response)
    return _decode_response_json(response)


def fetch_node_tasks(
    session_id: str, node_id: int, course_id: int | None = None
) -> dict[str, Any]:
    """Fetch flat list of tasks directly inside a folder (no subfolders).

    POST /restapi/get-node-tasks → {"tasks": [{"nodeId": N, "name": "..."}, ...]}

    ``course_id`` is optional but recommended — DL uses it for access checks
    when the node belongs to a specific course.

    Raises DLUnauthorizedError (401), DLForbiddenError (403),
    DLTaskNotFoundError (404), DLServerError (500).
    """
    payload: dict[str, Any] = {
        "sessionId": session_id,
        "nodeId": node_id,
    }
    if course_id is not None:
        payload["courseId"] = course_id
    response = _dl_request(
        "POST",
        "/restapi/get-node-tasks",
        json=payload,
    )

    _raise_for_status(response)
    return _decode_response_json(response)


def fetch_node_tree(
    session_id: str, node_id: int, course_id: int | None = None
) -> dict[str, Any]:
    """Fetch nested tree of all folders and tasks inside a folder.

    POST /restapi/get-node-tree → {"tree": [{nodeId, name, isFolder, children}, ...]}

    ``course_id`` is optional but recommended — DL uses it for access checks
    when the node belongs to a specific course.

    Raises DLUnauthorizedError (401), DLForbiddenError (403),
    DLTaskNotFoundError (404), DLServerError (500).
    """
    payload: dict[str, Any] = {
        "sessionId": session_id,
        "nodeId": node_id,
    }
    if course_id is not None:
        payload["courseId"] = course_id
    response = _dl_request(
        "POST",
        "/restapi/get-node-tree",
        json=payload,
    )

    _raise_for_status(response)
    return _decode_response_json(response)


def flatten_node_tree(tree: list | dict) -> list[dict[str, Any]]:
    """Flatten a nested get-node-tree response into a flat list of task nodes.

    Recursively walks the tree and returns only non-folder leaf nodes
    (tasks), each as {"nodeId": N, "name": "..."}.

    Accepts either the full response {"tree": [...]} or a bare list of nodes.
    """
    if isinstance(tree, dict):
        nodes = tree.get("tree", [])
    elif isinstance(tree, list):
        nodes = tree
    else:
        return []

    result: list[dict[str, Any]] = []

    def _walk(items):
        for item in items:
            if not isinstance(item, dict):
                continue
            is_folder = item.get("isFolder", False)
            if not is_folder:
                result.append({
                    "nodeId": item.get("nodeId"),
                    "name": item.get("name", ""),
                })
            children = item.get("children")
            if children:
                _walk(children)

    _walk(nodes)
    return result
