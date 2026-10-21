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
import logging
import os
import re
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

from django.http import JsonResponse

from .external_auth import get_external_auth_api_url

logger = logging.getLogger(__name__)


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


def dl_error_response(exc: DLApiError, extra: dict | None = None) -> JsonResponse:
    """Преобразовать исключение DL API в единый JSON error-ответ.

    Единственная точка маппинга DL-исключений на HTTP-статусы и сообщения
    (DRY: раньше 5 копий этой лестницы жили в views.py и admin/request_logs.py):
    DLUnauthorizedError→401, DLForbiddenError→403, DLTaskNotFoundError→404,
    DLApiUnavailable→503, DLServerError/прочие DLApiError→502.

    Причина (текст исключения — код ответа DL и сниппет тела) пишется в лог:
    наружу отдаётся обезличенный JSON, а без лога диагностика апстрима
    сводилась бы к голому «Bad Gateway» (ситуация 2026-09-14: dl.gsu.by
    стал требовать courseId в get-task-info — без лога это не видно).

    ``extra`` дописывает ключи в payload (например, ``{"ok": False}`` для
    admin-эндпоинтов, где JS проверяет ``d.ok``).
    """
    status, message = 502, "Server error"
    for exc_type, mapped_status, mapped_message in (
        (DLUnauthorizedError, 401, "Authorization required"),
        (DLForbiddenError, 403, "Access denied"),
        (DLTaskNotFoundError, 404, "Task not found"),
        (DLApiUnavailable, 503, "API temporarily unavailable"),
        (DLServerError, 502, "Server error"),
    ):
        if isinstance(exc, exc_type):
            status, message = mapped_status, mapped_message
            break
    # 401/403 — штатные состояния (протухшая сессия пользователя), шуметь
    # не нужно; всё остальное — признак проблемы апстрима/нашего запроса.
    if status not in (401, 403):
        logger.warning("DL API error → HTTP %s: %s", status, exc)
    payload = {"error": message}
    if extra:
        payload.update(extra)
    return JsonResponse(payload, status=status)


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
        # Тело 5xx часто несёт реальную причину (stacktrace/HTML-страница DL) —
        # прикладываем сниппет, иначе диагностика сводится к голому «код 500».
        raise DLServerError(
            f"Ошибка сервера DL (код {response.status_code}){_body_snippet(response)}"
        )
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

    GET /restapi/get-id-user-info?userId=<id> →
        {"firstName": "...", "lastName": "...", "education": {...}}

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


def fetch_user_education(user_id: int | str, timeout: int = 10) -> dict[str, str]:
    """Fetch the ``education`` block by userId from the DL REST API.

    GET /restapi/get-id-user-info?userId=<id> → ``{"education": {...}}``.
    The block has the same shape as in ``get-user-info``
    (dl-rest-api.json, EducationInfo): ``form``/``schoolId``/``schoolKind``/
    ``schoolNo``/``groupMaskId``/``formLetter`` plus the human-readable
    ``schoolName``/``formName``/``groupName``.

    Unlike ``get-user-info`` this endpoint needs NO sessionId, so the
    education data can be back-filled for every known student — that is how
    the group/school names got into the database without the manual
    ``DLEducationName`` directory.

    Returns a dict with the DL key names as keys (empty strings on failure).
    Never raises — best-effort enrichment.
    """
    empty = {
        "form": "", "schoolId": "", "schoolKind": "", "schoolNo": "",
        "groupMaskId": "", "formLetter": "",
        "schoolName": "", "formName": "", "groupName": "",
    }
    if not user_id:
        return dict(empty)

    try:
        response = _dl_request(
            "GET",
            "/restapi/get-id-user-info",
            params={"userId": str(user_id)},
            timeout=timeout,
        )
    except DLApiUnavailable:
        return dict(empty)

    if response.status_code != 200:
        return dict(empty)

    try:
        data = response.json()
    except ValueError:
        return dict(empty)

    education = data.get("education") or {}
    if not isinstance(education, dict):
        return dict(empty)

    return {
        key: str(education.get(key) or "").strip()
        for key in empty
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


def _send_solution_payload_context(node_id, code, file_extension, course_id=0, file_name="") -> str:
    """Build a diagnostic context string for send-solution failures.

    DL returns a bare «Bad Request» (400) without saying WHICH field it rejected,
    so we attach the payload context (nodeId, courseId, fileExtension, code length
    and a short head of the code) to the error. This lets the operator tell apart:
    a missing/zero courseId (the documented required field), oversized code
    (codeLen huge), a non-code payload from a broken extraction (codeHead is
    prose, not source), or an unsupported fileExtension.
    """
    code_head = " ".join((code or "").split())[:120]
    return (
        f"send-solution(nodeId={node_id}, courseId={course_id}, "
        f"fileExtension={file_extension!r}, "
        f"fileName={file_name!r}, "
        f"codeLen={len(code or '')}, "
        f"codeHead={code_head!r})"
    )


#: Контракт REST API (dl-rest-api.json, SendSolutionRequest.fileName):
#: ``^[A-Za-z0-9_-]+\.[A-Za-z0-9]+$``, maxLength 200, расширение должно
#: ТОЧНО совпадать с ``fileExtension`` (включая регистр), зарезервированные
#: имена Windows запрещены.
_DL_FILE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9]+$")
_DL_FILE_NAME_MAX_LEN = 200
#: Зарезервированные имена Windows (CON, PRN, AUX, NUL, COM1–COM9, LPT1–LPT9) —
#: DL их не принимает, а проверка на нашей стороне даёт внятную ошибку вместо
#: 400 от сервера.
_DL_RESERVED_FILE_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{i}" for i in range(1, 10)]
    + [f"LPT{i}" for i in range(1, 10)]
)


#: Транслитерация кириллицы в латиницу для имени файла. Контракт DL
#: допускает только ``[A-Za-z0-9_-]`` — без транслитерации русское имя
#: «Моё решение» превратилось бы в «1» (одна цифра), и файл в DL был бы
#: безымянным. Транслитерация даёт читаемое латинское имя.
_DL_TRANSLIT_MAP = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def _dl_transliterate(text: str) -> str:
    """Кириллица → латиница (для имени файла по контракту DL).

    Только кириллица заменяется; латиница, цифры и дефисы сохраняются
    как есть (в т.ч. регистр — «Deepseek» не должен стать «deepseek»).
    """
    out = []
    for ch in text:
        lower = ch.lower()
        if lower in _DL_TRANSLIT_MAP:
            mapped = _DL_TRANSLIT_MAP[lower]
            # Регистр первой буквы слова сохраняем.
            if ch.isupper() and mapped:
                mapped = mapped[0].upper() + mapped[1:]
            out.append(mapped)
        else:
            out.append(ch)
    return "".join(out)


def build_dl_file_name(base_name: str, file_extension: str) -> str:
    """Собрать имя временного файла решения по контракту DL (v1.4.2).

    Контракт ``SendSolutionRequest.fileName``: ``^[A-Za-z0-9_-]+\\.[A-Za-z0-9]+$``,
    длина ≤ 200, расширение — ТОЧНО как в ``fileExtension`` (с точкой, с тем же
    регистром), зарезервированные имена Windows запрещены.

    Валидное имя приходит как есть. Невалидное — чинится по правилам DL:
    кириллица транслитерируется в латиницу, пробелы и прочие
    недопустимые символы → ``_``, длина режется до лимита с сохранением
    расширения. Если после чистки имя всё равно невалидно (пустая основа)
    — возвращается ``None``, и отправка идёт БЕЗ поля ``fileName``
    (DL сам назовёт файл ``api_solution_<случайное>.<ext>``).

    Расширение подставляется из ``file_extension`` (тот же, что уходит в
    ``fileExtension``) — иначе DL отвергнет запрос («несовпадение расширения»).
    """
    ext = (file_extension or "").strip()
    if not ext.startswith("."):
        ext = f".{ext}" if ext else ""
    ext = ext[1:]  # без точки
    if not ext or not re.fullmatch(r"[A-Za-z0-9]+", ext):
        return None

    base = (base_name or "").strip()
    # Отрезаем расширение, если оператор уже написал «имя.расширение».
    if "." in base:
        base = base.rsplit(".", 1)[0]
    # Кириллица → латиница (иначе имя превратится в «_»/пустую основу).
    base = _dl_transliterate(base)
    # Только разрешённые символы: латиница, цифры, дефис, подчёркивание.
    base = re.sub(r"[^A-Za-z0-9_-]+", "_", base)
    base = base.strip("_-")
    if not base:
        return None
    if base.upper() in _DL_RESERVED_FILE_NAMES:
        base = f"{base}_file"

    # Лимит 200 символов на ВСЁ имя (основа + точка + расширение).
    max_base = _DL_FILE_NAME_MAX_LEN - len(ext) - 1
    if max_base < 1:
        return None
    base = base[:max_base]
    name = f"{base}.{ext}"
    if len(name) > _DL_FILE_NAME_MAX_LEN:
        return None
    if not _DL_FILE_NAME_RE.match(name):
        return None
    return name


def fetch_deltas() -> list[dict[str, Any]]:
    """Список тестирующих машин (Дельт) — ``GET /restapi/get-deltas``.

    Ответ: ``{"deltas": [{"id": N, "name": "...", "displayName": "...",
    "isWorking": bool}, …]}``. Эндпоинт открытый (sessionId не требуется) —
    это перечисление всего парка машин DL без авторизации, о чём стоит
    сказать разработчикам DL.

    Возвращает список словарей. При ошибке — пустой список (выбор машины
    не обязан ломать страницу: список машин — справочный, необязательный).
    """
    try:
        response = _dl_request("GET", "/restapi/get-deltas", timeout=15)
        _raise_for_status(response)
        data = _decode_response_json(response)
    except DLApiError as exc:
        logger.warning("Не удалось получить список тестирующих машин DL: %s", exc)
        return []
    deltas = data.get("deltas") if isinstance(data, dict) else None
    if not isinstance(deltas, list):
        return []
    result = []
    for item in deltas:
        if not isinstance(item, dict):
            continue
        try:
            delta_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        result.append({
            "id": delta_id,
            "name": str(item.get("name") or ""),
            "display_name": str(item.get("displayName") or ""),
            "is_working": bool(item.get("isWorking")),
        })
    return result


def ensure_course_session(session_id: str, course_id: int, node_id: int) -> bool:
    """Activate a course in the DL session so send-solution works.

    DL ties ``courseID`` to the DLSID session: send-solution returns 400 when
    ``courseID=0`` (user logged in but hasn't "entered" a course). The REST API
    has no endpoint to set courseID — it's set by visiting a classic DL page.

    ``GET /task.jsp?cid=<course_id>&nid=<node_id>`` with the DLSID cookie
    activates the course in the session. After this call, get-user-info returns
    ``courseID=<course_id>`` and send-solution accepts submissions.

    Returns True if the course was activated (or already active), False on
    failure (network error, 4xx/5xx). Best-effort: callers proceed regardless.
    """
    if not course_id or not node_id:
        return False
    base_url = _get_dl_base_url()
    url = urljoin(base_url, f"/task.jsp?cid={course_id}&nid={node_id}")
    cookies = {"DLSID": session_id}
    request_kwargs = {
        "cookies": cookies,
        "verify": _get_verify_ssl(),
        "timeout": 15,
        "allow_redirects": True,
    }
    proxies = _get_proxies()
    if proxies is not None:
        request_kwargs["proxies"] = proxies
    try:
        resp = requests.get(url, **request_kwargs)
        return resp.status_code == 200
    except Exception:
        return False


def send_solution_to_dl(
    session_id: str,
    node_id: int,
    code: str,
    file_extension: str,
    course_id: int = 0,
    file_name: str = "",
    delta: str = "",
) -> dict[str, Any]:
    """Submit code to DL for automated testing.

    POST /restapi/send-solution → {"queueId": N, "message": "..."}

    ``course_id`` — обязательное по контракту REST API поле (Id курса, в котором
    находится нода). Передаётся в теле как ``courseId``. Без него DL отдаёт 400
    Bad Request; ручная отправка через веб-форму работает, т.к. курс уже в URL
    страницы (``cid``). По умолчанию 0 — только для обратной совместимости
    сигнатуры, все in-tree вызовы должны передавать реальный course_id.

    ``file_name`` (контракт v1.4.2, ``SendSolutionRequest.fileName``) — необязательное
    имя временного файла решения. Формат жёсткий:
    ``^[A-Za-z0-9_-]+\\.[A-Za-z0-9]+$``, ≤ 200 символов, расширение ТОЧНО как в
    ``fileExtension``. Здесь имя нормализуется (``build_dl_file_name``); если
    привести к контракту нельзя — поле не отправляется, DL назовёт файл сам
    (``api_solution_<случайное>.<ext>``).

    ``delta`` (контракт v1.4.2) — машина проверки: её ID ("5") или ТОЧНОЕ
    название ("DelTA4 at NIT1 Win10 x64"). Пусто — машина назначается DL.

    Returns the raw JSON response. Raises DLUnauthorizedError (401),
    DLServerError (500), DLApiUnavailable on network failure.
    """
    payload = {
        "sessionId": session_id,
        "nodeId": node_id,
        "code": code,
        "fileExtension": file_extension,
        "courseId": course_id,
    }
    # Имя файла — по контракту; невалидное/пустое → поле не отправляется
    # (DL назовёт файл сам — это безопаснее, чем 400 из-за имени).
    resolved_name = build_dl_file_name(file_name, file_extension) if file_name else ""
    if resolved_name:
        payload["fileName"] = resolved_name
    # Машина проверки: ID или точное название (контракт: "delta").
    delta_value = str(delta or "").strip()
    if delta_value:
        payload["delta"] = delta_value

    response = _dl_request(
        "POST",
        "/restapi/send-solution",
        json=payload,
    )

    # 400/413/5xx: DL отдаёт голое «Bad Request» без указания поля — прикладываем
    # контекст payload (nodeId/courseId/extension/имя файла/длина кода/начало кода)
    # для диагностики. 401/403/404 оставляем типизированными (не оборачиваем).
    try:
        _raise_for_status(response)
        return _decode_response_json(response)
    except DLServerError as exc:
        raise DLServerError(
            f"{_send_solution_payload_context(node_id, code, file_extension, course_id, resolved_name)}: {exc}"
        ) from exc


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


def get_failed_tests_from_dl(
    session_id: str,
    queue_id: int,
) -> list[dict[str, Any]]:
    """Подтверждённые проваленные тесты решения (контракт DL v1.4.2).

    POST /restapi/get-failed-tests {sessionId, queueId} →
    ``{"isFinished": bool, "tests": [{stepId, testNumber, result, message,
    canGetData}]}``

    Ошибки DL НЕ поднимаются наружу: данные теста — необязательное
    дополнение к отчёту (не условие прогона). Типичные отказы — 403
    (выдача тестов запрещена настройками курса), 404 (решение не найдено),
    409 (проверка ещё идёт).
    """
    try:
        response = _dl_request(
            "POST",
            "/restapi/get-failed-tests",
            json={"sessionId": session_id, "queueId": int(queue_id)},
        )
        _raise_for_status(response)
        data = _decode_response_json(response)
    except Exception as exc:  # noqa: BLE001 — данные теста не обязательны
        logger.info("get-failed-tests не ответил (queueId=%s): %s", queue_id, exc)
        return []
    tests = data.get("tests") if isinstance(data, dict) else None
    if not isinstance(tests, list):
        return []
    return [t for t in tests if isinstance(t, dict)]


def get_test_data_from_dl(
    session_id: str,
    queue_id: int,
    test_number: int,
) -> dict[str, str]:
    """Входные данные и эталонный ответ проваленного теста.

    POST /restapi/get-test-data {sessionId, queueId, testNumber} →
    ``{"input": "...", "expectedOutput": "..."}``

    ``expectedOutput`` — эталонный ответ, а НЕ вывод проверяемой программы.
    Ошибки DL не поднимаются наружу (см. get_failed_tests_from_dl) — при
    отказе возвращается пустой словарь.
    """
    try:
        response = _dl_request(
            "POST",
            "/restapi/get-test-data",
            json={
                "sessionId": session_id,
                "queueId": int(queue_id),
                "testNumber": int(test_number),
            },
        )
        _raise_for_status(response)
        data = _decode_response_json(response)
    except Exception as exc:  # noqa: BLE001 — данные теста не обязательны
        logger.info(
            "get-test-data не ответил (queueId=%s, test=%s): %s",
            queue_id, test_number, exc,
        )
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        "input": str(data.get("input") or ""),
        "expected_output": str(data.get("expectedOutput") or ""),
    }


# --- Вердикт по завершённому DL-комментарию ---------------------------------

# Маркеры провала в ЗАВЕРШЁННОМ DL-комментарии. Проверяются ПЕРВЫМИ и
# перекрывают success-маркеры: раньше голая подстрока «ок» матчилась внутри
# «строке»/«token»/«broken», и комментарий вида «Ошибка компиляции в строке 5»
# ложно помечал проваленную DL-проверку как solved.
DL_FAILURE_MARKERS = (
    "неверн", "неправильн", "ошибк", "не совп", "не прош", "не пройден",
    "не все", "не всё", "провал", "не принят", "не зачт", "отклон", "не ок",
    "wrong", "error", "failed", "incorrect",
)
# Success-маркеры с границами слов: «ок»/«ok» как отдельное слово, а не
# подстрока внутри «строке»/«token». «все тесты успешно» покрывается «все тесты».
_DL_SUCCESS_MARKERS_RE = re.compile(
    r"\b(?:все тесты|ок|ok|accepted|correct|пройдены|успешно пройден)\b",
    re.IGNORECASE,
)

DL_VERDICT_SOLVED = "solved"
DL_VERDICT_FAILED = "failed"

# Маркеры ОШИБКИ КОМПИЛЯЦИИ. Проверяются ПЕРВЫМИ: комментарий «Ошибка
# компиляции в строке 5» — это про компиляцию, а не про неверный ответ,
# хотя содержит общий маркер «ошибк».
DL_COMPILE_MARKERS = (
    "ошибк компиляц", "компиляц", "compile error", "compilation error",
    "syntax error", "не компилир", "не компилируется", "ошибка синтаксис",
    "синтаксическ", "не собирает", "сборк", "build failed",
)
# УСПЕШНАЯ компиляция. Маркеры выше — «стебли» без окончаний, поэтому
# срабатывают и на успехе: «Компиляция успешна», «Сборка прошла успешно»
# содержат «компиляц»/«сборк» и без него давали ложный «ошибка
# компиляции». Здесь ловим сам факт успешной сборки — тогда причина
# определяется остальными маркерами (обычно «неверный ответ»).
_DL_COMPILE_OK_RE = re.compile(
    r"(?:компиляц|сборк|build|compile)[^\n]{0,40}?"
    r"(?:успешн|прошл|без ошибок|удачн|ok\b)"
    r"|(?:успешн|прошл|удачн)[^\n]{0,40}?(?:компиляц|сборк|build|compile)",
    re.IGNORECASE,
)
# Маркеры НЕПРАВИЛЬНОГО ОТВЕТА (программа собралась, но результат не тот).
# Основы — «стебли» без окончаний: «неправильн» покрывает «неправильный/
# неправильно», «неверн» — «неверный/неверно». Иначе комментарий
# «Неправильный ответ на тесте 2» не распознавался (в «неправильн ответ»
# между основой и словом «ответ» стоит «ый»).
DL_WRONG_ANSWER_MARKERS = (
    "неправильн", "неверн", "wrong answer", "не совп", "не прош",
    "не все тесты", "не все проверки", "rejected", "не зачт", "не принят",
    "отклон",
)
# Маркеры ОШИБКИ ПЕРЕМЕННОЙ/ИДЕНТИФИКАТОРА. Это ОШИБКА КОМПИЛЯЦИИ, а не
# неверный ответ: «Неправильная переменная 'x'», «Не правильная переменная»,
# «неизвестный идентификатор», «переменная не объявлена». Раньше такие
# комментарии уезжали в wrong_answer: маркер «неправильн» из
# DL_WRONG_ANSWER_MARKERS матчится в «Неправильная переменная».
DL_VARIABLE_MARKERS = (
    "переменн", "идентификатор", "identifier", "не объявлен", "необъявлен",
    "undeclared",
)
# «Сильные» маркеры неверного ответа — фразы, которые однозначно говорят:
# программа СОБРАЛАСЬ, но результат не тот. Используются как «стоп» для
# DL_VARIABLE_MARKERS: комментарий «Неправильный ответ: значение переменной
# не равно ожидаемому» — это wrong_answer, а не ошибка компиляции.
DL_WRONG_ANSWER_STRONG_MARKERS = (
    "неправильный ответ", "неправильн ответ", "неверный ответ", "неверн ответ",
    "неверно", "неправильно", "не совпад", "не совп", "не прош",
    "не все тесты", "не все проверки", "rejected", "не зачт", "не принят",
    "отклон", "wrong answer", "answer is wrong", "не равно", "не соответств",
)

# Причины сбоя (ортогональны вердикту: вердикт — «прошёл ли DL», причина —
# «почему нет»). Значения совпадают с AIModelTestResult.FAILURE_REASON_CHOICES.
DL_REASON_COMPILE_ERROR = "compile_error"
DL_REASON_WRONG_ANSWER = "wrong_answer"
DL_REASON_UNDEFINED = "undefined"


def dl_verdict_from_comment(comment: str) -> str:
    """Вердикт по завершённому DL-комментарию: solved / failed.

    DL REST API не отдаёт структурированный вердикт (только isFinished +
    comment), поэтому решаем по тексту. Провальные маркеры приоритетны: любой
    комментарий провала («Ошибка…», «Неверный ответ…», «не все тесты…») —
    failed, даже если в нём по случайности встретилось слово «ок».
    Нераспознанный комментарий — failed (solved ставится только при явном
    подтверждении).
    """
    comment_lower = (comment or "").lower().strip()
    if any(m in comment_lower for m in DL_FAILURE_MARKERS):
        return DL_VERDICT_FAILED
    if _DL_SUCCESS_MARKERS_RE.search(comment_lower):
        return DL_VERDICT_SOLVED
    return DL_VERDICT_FAILED


def dl_failure_reason_from_comment(comment: str) -> str:
    """Причина провала по тексту DL-комментария.

    Канонический классификатор (рядом с ``dl_verdict_from_comment``): разбирает
    ТОТ ЖЕ комментарий, что и вердикт, но отвечает на вопрос «почему пара не
    решена». Порядок проверки:

    1. маркеры компиляции → ``compile_error`` — но только если компиляция
       НЕ признана успешной (``_DL_COMPILE_OK_RE``): «Компиляция успешна,
       все тесты пройдены» — это успех, а не ошибка компиляции;
    2. маркеры переменной/идентификатора → ``compile_error`` (тоже ошибка
       компиляции: «Неправильная переменная 'x' не объявлена»); но если в
       комментарии есть СИЛЬНЫЙ маркер неверного ответа («Неправильный
       ответ: значение переменной не равно ожидаемому») — это
       ``wrong_answer``;
    3. маркеры неверного ответа → ``wrong_answer``;
    4. иначе, если комментарий содержит любой провальный маркер →
       ``undefined`` (провал DL, причина не распознана);
    5. иначе (успех) → ``""`` (пустая строка — причина нет).

    Провал НИКОГДА не переименовывается в успех: нераспознанный провал =
    ``undefined``, а не пустая строка.
    """
    comment_lower = (comment or "").lower().strip()
    if not comment_lower:
        return DL_REASON_UNDEFINED
    compile_ok = bool(_DL_COMPILE_OK_RE.search(comment_lower))
    if not compile_ok and any(m in comment_lower for m in DL_COMPILE_MARKERS):
        return DL_REASON_COMPILE_ERROR
    if not compile_ok and any(m in comment_lower for m in DL_VARIABLE_MARKERS):
        # «Неправильная переменная» — это про компиляцию, а не про ответ.
        # Отступаем только при явном «неправильный ответ».
        if not any(m in comment_lower for m in DL_WRONG_ANSWER_STRONG_MARKERS):
            return DL_REASON_COMPILE_ERROR
    if any(m in comment_lower for m in DL_WRONG_ANSWER_MARKERS):
        return DL_REASON_WRONG_ANSWER
    if any(m in comment_lower for m in DL_FAILURE_MARKERS):
        return DL_REASON_UNDEFINED
    # Успех (или пустой комментарий при отсутствии провальных маркеров) —
    # причина сбоя отсутствует.
    if _DL_SUCCESS_MARKERS_RE.search(comment_lower):
        return ""
    return DL_REASON_UNDEFINED


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
