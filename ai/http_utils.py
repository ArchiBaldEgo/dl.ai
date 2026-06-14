"""Small HTTP helpers used across the AI app."""


def safe_relative_url(candidate, fallback):
    """Return a relative URL if it is safe, otherwise the fallback."""
    value = (candidate or "").strip()
    if value.startswith("/") and not value.startswith("//"):
        return value
    return fallback
