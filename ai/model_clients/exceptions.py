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
