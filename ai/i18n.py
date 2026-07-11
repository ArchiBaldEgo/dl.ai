"""UI-language localization helpers for AI app models."""

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
    """Return a localized name for the object, falling back to base fields.

    Looks for ``{default_attr}_{suffix}`` first, then the base field, then the
    *_ru field.  Returns the object's string representation as a last resort.
    """
    suffix = get_ui_language_suffix(ui_language)
    candidates = [
        f"{default_attr}_{suffix}",
        default_attr,
        f"{default_attr}_ru",
    ]
    for attr in candidates:
        value = getattr(obj, attr, None)
        if value:
            return str(value)
    return str(obj)


def get_localized_text(obj: Any, ui_language: str, default_attr: str = "text") -> str:
    """Return a localized text for the object, falling back to base fields."""
    suffix = get_ui_language_suffix(ui_language)
    candidates = [
        f"{default_attr}_{suffix}",
        default_attr,
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
        return ". Разговаривай со мной только по-русски"
    if ui_language == "Français":
        return ". Communiquez avec moi uniquement en français"
    if ui_language == "English":
        return ". Communicate with me only in English"
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
