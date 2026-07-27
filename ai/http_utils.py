"""Небольшие HTTP-хелперы для AI-приложения."""


def safe_relative_url(candidate, fallback):
    """Возвращает относительный URL, если он безопасен, иначе fallback.

    Защита от open-redirect: принимает только пути, начинающиеся с '/'
    и не начинающиеся с '//' (протокол-относительный URL).
    """
    value = (candidate or "").strip()
    if value.startswith("/") and not value.startswith("//"):
        return value
    return fallback
