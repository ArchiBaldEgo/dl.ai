"""Хелперы локализации UI для моделей AI-приложения.

Поддерживает три языка интерфейса: Русский, English, Français.
Предоставляет функции для получения локализованных имён и текстов полей
моделей (Prompt, Topic, SharedPrompt) на основе выбранного языка UI.
"""

from typing import Any

UI_LANGUAGES = (
    ("ru", "Русский"),
    ("en", "English"),
    ("fr", "Français"),
)

UI_LANGUAGE_TO_SUFFIX = {
    "Русский": "ru",
    "English": "en",
    "Français": "fr",
}

UI_SUFFIX_TO_LANGUAGE = {v: k for k, v in UI_LANGUAGE_TO_SUFFIX.items()}


def get_ui_language_suffix(ui_language: str) -> str:
    """Return the DB suffix (ru/en/fr) for a UI-language display name.

    Falls back to the first two lowercase characters if the exact name is
    not recognized (e.g. 'Russian' -> 'ru').
    """
    suffix = UI_LANGUAGE_TO_SUFFIX.get(ui_language)
    if suffix:
        return suffix
    low = (ui_language or "").lower()
    if low.startswith("ru"):
        return "ru"
    if low.startswith("en"):
        return "en"
    if low.startswith("fr"):
        return "fr"
    return "ru"


def get_localized_name(obj: Any, ui_language: str, default_attr: str = "name") -> str:
    """Return a localized name for the object, falling back to Russian.

    Looks for ``{default_attr}_{suffix}`` first, then the *_ru field.
    Если оба пусты — НЕ ``str(obj)``: модели Prompt/Topic/SharedPrompt
    реализуют ``__str__`` через этот же хелпер, и пустые *_ru поля уводили
    его в бесконечную рекурсию (RecursionError → 500 на /admin/ai/prompt/add/
    и везде, где строки приводятся к str). Безопасный фолбэк — подпись по pk.
    """
    suffix = get_ui_language_suffix(ui_language)
    candidates = [
        f"{default_attr}_{suffix}",
        f"{default_attr}_ru",
    ]
    for attr in candidates:
        value = getattr(obj, attr, None)
        if value:
            return str(value)
    pk = getattr(obj, "pk", None)
    if pk is None:
        return obj.__class__.__name__
    return f"{obj.__class__.__name__} #{pk}"


def get_localized_text(obj: Any, ui_language: str, default_attr: str = "text") -> str:
    """Return a localized text for the object, falling back to *_ru."""
    suffix = get_ui_language_suffix(ui_language)
    candidates = [
        f"{default_attr}_{suffix}",
        f"{default_attr}_ru",
    ]
    for attr in candidates:
        value = getattr(obj, attr, None)
        if value:
            return str(value)
    return ""


def get_language_instruction(ui_language: str) -> str:
    """Return a language constraint instruction for model prompts."""
    if ui_language == "Русский":
        return "\n\nОтвечай только по-русски."
    if ui_language == "Français":
        return "\n\nRéponds uniquement en français."
    if ui_language == "English":
        return "\n\nRespond only in English."
    return ""


_SHARED_PROMPT_PREFIX = {
    "ru": "[Общий]",
    "en": "[Shared]",
    "fr": "[Partagé]",
}


def get_shared_prompt_prefix(ui_language: str = "") -> str:
    """Return the localized prefix for SharedPrompt.__str__."""
    suffix = get_ui_language_suffix(ui_language)
    return _SHARED_PROMPT_PREFIX.get(suffix, _SHARED_PROMPT_PREFIX["ru"])
