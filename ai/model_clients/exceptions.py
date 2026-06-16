"""Shared error-handling helpers for AI model clients."""

from typing import Tuple, Optional


def classify_network_error(error: Exception) -> str:
    """Return a user-facing Russian error string for network-related issues."""
    error_str = str(error)
    if "NameResolutionError" in error_str or "Failed to resolve" in error_str:
        return "Отсутствует подключение к интернету."
    if "Max retries exceeded" in error_str:
        return "Отсутствует интернет-соединение."
    return "Ошибка подключения. Ваш контекст сохранен, попробуйте позже."


def is_network_error(error: Exception) -> bool:
    """Return True when the exception looks like a network/timeout problem."""
    error_str = str(error)
    error_type = type(error).__name__
    network_phrases = (
        "NameResolutionError",
        "Failed to resolve",
        "Max retries exceeded",
        "HTTPSConnectionPool",
        "Name or service not known",
        "ConnectionError",
        "timeout",
        "Timeout",
    )
    return any(phrase in error_str for phrase in network_phrases) or "ConnectionError" in error_type


def is_missing_choices_error(error: Exception) -> bool:
    """Return True when the response appears to lack the expected choices key."""
    error_type = type(error).__name__
    error_str = str(error)
    return error_type == "KeyError" and ("'choices'" in error_str or "choices" in error_str)


def safe_parse_response(response_text: str) -> Tuple[Optional[dict], str]:
    """Try to parse JSON response; return (obj, error_message)."""
    import json

    if not response_text:
        return None, "Пустой ответ от сервера."
    try:
        return json.loads(response_text), ""
    except json.JSONDecodeError as e:
        return None, f"Что-то пошло не так с обработкой JSON: {e}"


def extract_choice_content(obj: dict) -> str:
    """Extract assistant content from a SambaNova-style completion response."""
    choices = obj.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message", {})
    content = message.get("content")
    if not content:
        content = message.get("reasoning_content") or ""
    if not content:
        content = message.get("reasoning") or ""
    if not content:
        content = "Пустой ответ от модели."
    return content


def extract_api_error_text(response_text: str) -> str:
    """Return a short Russian error based on common HTTP/API failure patterns."""
    low = response_text.lower()
    if "rate limit" in low or "превышен лимит" in low:
        return "Превышен лимит запросов. Попробуйте позже."
    if response_text.startswith("5") or response_text.startswith("http 5"):
        return "Ошибка сервера API. Попробуйте позже."
    return f"Ошибка API (код {response_text})."


_MODEL_ERROR_PATTERNS = (
    # (sequence of substrings, friendly message)
    (
        ("неправильный запрос", "код 400", "error 400", "status 400", "bad request"),
        "Ошибка запроса к модели (400). Обычно это значит, что запрос слишком длинный, "
        "содержит неподдерживаемый формат или лишние спецсимволы. "
        "Попробуйте сократить условие/код и отправить снова.",
    ),
    (
        ("код 401", "error 401", "status 401", "unauthorized", "не авториз"),
        "Ошибка авторизации модели (401). Проверьте API-ключ/токен и права доступа к модели.",
    ),
    (
        ("код 403", "error 403", "status 403", "forbidden", "доступ запрещ"),
        "Доступ к модели запрещен (403). У текущего ключа нет нужных прав или доступ ограничен политикой сервиса.",
    ),
    (
        ("код 404", "error 404", "status 404", "not found", "не найден"),
        "Модель не найдена (404). Возможно, имя модели устарело или эта модель сейчас недоступна у провайдера.",
    ),
    (
        ("код 429", "error 429", "status 429", "rate limit", "превышен лимит", "все боты заняты"),
        "Сервис модели ограничил частоту запросов (429). Подождите немного и запустите проверку снова.",
    ),
    (
        ("таймаут", "timeout", "timed out", "код 408", "status 408"),
        "Модель не ответила вовремя (таймаут). Попробуйте повторить запрос позже или сократить объем задачи/кода.",
    ),
    (
        (
            "код 500", "error 500", "status 500",
            "код 502", "error 502", "status 502",
            "код 503", "error 503", "status 503",
            "код 504", "error 504", "status 504",
            "bad gateway", "gateway", "server error", "ошибка сервера",
            "временно недоступ", "инициализируется слишком долго",
        ),
        "Сервис модели временно недоступен (5xx). Это серверная ошибка провайдера, попробуйте позже.",
    ),
    (
        (
            "отсутствует подключение к интернету", "отсутствует интернет",
            "connectionerror", "failed to resolve", "name resolution",
            "max retries exceeded", "httpsconnectionpool", "не удалось подключ",
        ),
        "Ошибка подключения к сервису модели. Проверьте сеть/прокси и доступность внешнего API.",
    ),
)


def humanize_model_error(raw_text: str, include_detail: bool = False) -> tuple[str, str]:
    """Return a user-friendly error message and, optionally, a detailed variant.

    The detailed message keeps the original technical text after the friendly
    explanation.  When ``include_detail`` is False both returned values are the
    same friendly string.
    """
    text = (raw_text or "").strip()
    if not text:
        return "", ""

    low = text.lower()

    # Long natural-language responses from models should not be interpreted as transport errors.
    if len(low) > 350 and not low.startswith(("ошибка", "error", "exception", "traceback")):
        return text, text

    for markers, friendly_text in _MODEL_ERROR_PATTERNS:
        if any(marker in low for marker in markers):
            if not include_detail:
                return friendly_text, friendly_text
            detailed_text = friendly_text
            if text and text != friendly_text:
                detailed_text += f"\n\nТехническая деталь: {text}"
            return friendly_text, detailed_text

    if low.startswith("ошибка api") or "api (код" in low:
        friendly_text = "Сервис модели вернул ошибку API. Проверьте параметры запроса и повторите попытку чуть позже."
        if not include_detail:
            return friendly_text, friendly_text
        detailed_text = f"{friendly_text}\n\nТехническая деталь: {text}"
        return friendly_text, detailed_text

    return text, text
