"""Auto-translate localized fields (name_en, name_fr, text_en, text_fr)
using deep-translator (Google Translate) — fast, free, no API key.

Used by the management command ``auto_translate`` and the admin action
``translate_selected`` to fill empty ``*_en`` / ``*_fr`` fields from the
Russian (or base) source.
"""

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

TARGET_LANGUAGES = ("en", "fr")

# deep-translator language codes for Google Translate.
_GOOGLE_LANG_MAP = {
    "en": "en",
    "fr": "fr",
}

# Compiled regex for placeholder protection — matches {anything} including
# Cyrillic placeholder names like {язык}, {тема}.
_PLACEHOLDER_RE = re.compile(r"\{[^}]+\}")

# API error / garbage patterns to reject (kept from AI-based version for safety).
_ERROR_PATTERNS = (
    "ошибка api", "код 429", "код 410", "timeout connecting",
    "rate limit", "unauthorized", "недоступен",
)


def _protect_placeholders(text: str):
    """Replace {placeholder} tokens with @@N@@ markers and return (protected, placeholders)."""
    placeholders = _PLACEHOLDER_RE.findall(text)
    protected = text
    for i, ph in enumerate(placeholders):
        protected = protected.replace(ph, f"@@PH{i}@@", 1)
    return protected, placeholders


def _restore_placeholders(text: str, placeholders: list) -> str:
    """Restore @@N@@ markers back to original placeholders."""
    for i, ph in enumerate(placeholders):
        text = text.replace(f"@@PH{i}@@", ph)
        text = text.replace(f"@@ph{i}@@", ph)  # Google sometimes lowercases
    return text


def _strip_extra(text: str) -> str:
    """Strip surrounding quotes and reject API error messages."""
    s = text.strip()
    if len(s) >= 2:
        for q in ('"', "'", "«", "»", "“", "”"):
            if s.startswith(q) and s.endswith(q):
                s = s[1:-1].strip()
    low = s.lower()
    for pat in _ERROR_PATTERNS:
        if pat in low:
            return ""
    return s


def _translate_chunk(text: str, google_lang: str) -> str:
    """Translate a single chunk (must be <= 5000 chars).

    Temporarily clears proxy env vars so Google Translate is reached directly
    (corporate proxy may truncate long POST bodies).
    """
    import os
    from deep_translator import GoogleTranslator

    saved = {}
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        if var in os.environ:
            saved[var] = os.environ.pop(var)

    try:
        translator = GoogleTranslator(source="ru", target=google_lang)
        return translator.translate(text) or ""
    finally:
        os.environ.update(saved)


def _split_by_paragraphs(text: str, max_len: int = 4500) -> list:
    """Split long text into chunks <= max_len, trying to break at paragraph/line boundaries."""
    if len(text) <= max_len:
        return [text]

    chunks = []
    # Try splitting by double-newline (paragraphs) first.
    paragraphs = text.split("\n\n")
    current = ""
    for para in paragraphs:
        if len(current) + len(para) + 2 <= max_len:
            current = (current + "\n\n" + para) if current else para
        else:
            if current:
                chunks.append(current)
            # If single paragraph is too long, split by single newlines.
            if len(para) > max_len:
                lines = para.split("\n")
                current = ""
                for line in lines:
                    if len(current) + len(line) + 1 <= max_len:
                        current = (current + "\n" + line) if current else line
                    else:
                        if current:
                            chunks.append(current)
                        # If single line is too long, hard-split.
                        if len(line) > max_len:
                            for i in range(0, len(line), max_len):
                                chunks.append(line[i:i + max_len])
                            current = ""
                        else:
                            current = line
                # current will be flushed in next iteration or after loop
            else:
                current = para
    if current:
        chunks.append(current)
    return chunks


def translate_text(text: str, target_lang: str, model_key: Optional[str] = None) -> str:
    """Translate ``text`` to ``target_lang`` (en/fr) using Google Translate.

    Placeholders like {language}, {язык}, {topic} are preserved as-is.
    Long texts (>5000 chars) are split into chunks and translated separately.
    Returns the translated string, or "" on failure.
    """
    if not text or not text.strip():
        return ""

    google_lang = _GOOGLE_LANG_MAP.get(target_lang)
    if not google_lang:
        return ""

    protected, placeholders = _protect_placeholders(text)

    try:
        chunks = _split_by_paragraphs(protected, max_len=2000)
        translated_parts = []
        for chunk in chunks:
            raw = _translate_chunk(chunk, google_lang)
            if not raw:
                # Retry once on transient Google errors.
                import time
                time.sleep(1)
                raw = _translate_chunk(chunk, google_lang)
            if not raw:
                logger.error("Auto-translate chunk failed (%d chars) for lang=%s", len(chunk), target_lang)
                return ""
            translated_parts.append(raw)

        full_translated = "\n\n".join(translated_parts) if len(translated_parts) > 1 else translated_parts[0]
        full_translated = _restore_placeholders(full_translated, placeholders)
        result = _strip_extra(full_translated)
        if result:
            logger.info("Auto-translated [%s] (%d chars → %d chars) via Google Translate",
                        target_lang, len(text), len(result))
        return result
    except Exception as exc:
        logger.error("Auto-translate to %s failed: %s", target_lang, exc)
        return ""


def _get_field(obj, field_base: str, suffix: str) -> str:
    """Get obj.field_base_suffix, falling back to field_base, then field_base_ru."""
    for attr in (f"{field_base}_{suffix}", field_base, f"{field_base}_ru"):
        val = getattr(obj, attr, None)
        if val:
            return str(val)
    return ""


def _set_field(obj, field_base: str, suffix: str, value: str, overwrite: bool) -> bool:
    """Set obj.field_base_suffix if currently empty (or overwrite=True).

    Truncates to fit CharField max_length.
    Returns True if changed.
    """
    attr = f"{field_base}_{suffix}"
    current = getattr(obj, attr, None) or ""
    if current.strip() and not overwrite:
        return False
    if not value.strip():
        return False
    # Truncate to fit CharField max_length.
    try:
        field = obj._meta.get_field(attr)
        max_len = getattr(field, 'max_length', None)
        if max_len and len(value) > max_len:
            value = value[:max_len]
    except Exception:
        pass
    setattr(obj, attr, value)
    return True


# (model_class, field_bases) — which fields to translate per model.
_TRANSLATABLE_MODELS = []


def _register_translatable(model_class, field_bases):
    _TRANSLATABLE_MODELS.append((model_class, field_bases))


def get_translatable_models():
    """Return list of (ModelClass, [field_base, ...]) pairs."""
    if _TRANSLATABLE_MODELS:
        return _TRANSLATABLE_MODELS
    from ai.models import Topic, SharedPrompt, Prompt
    _register_translatable(Topic, ["topic_name"])
    _register_translatable(SharedPrompt, ["prompt_name", "prompt_text"])
    _register_translatable(Prompt, ["prompt_name", "prompt_text"])
    return _TRANSLATABLE_MODELS


def translate_object(obj, field_bases, target_langs=None, overwrite=False) -> dict:
    """Translate all empty ``*_lang`` fields on ``obj`` for the given field bases.

    Args:
        obj: Django model instance with localized fields.
        field_bases: List of base field names (e.g. ``["topic_name"]``).
        target_langs: Tuple of suffixes to translate to. Default: ("en", "fr").
        overwrite: If True, overwrite existing non-empty translations too.

    Returns:
        Dict with per-field results: ``{"topic_name_en": "translated", ...}``
    """
    if target_langs is None:
        target_langs = TARGET_LANGUAGES

    results = {}
    changed = False

    for field_base in field_bases:
        source = _get_field(obj, field_base, "ru")
        if not source:
            continue

        for lang in target_langs:
            attr = f"{field_base}_{lang}"
            current = getattr(obj, attr, None) or ""
            if current.strip() and not overwrite:
                results[attr] = "skipped (already set)"
                continue

            translated = translate_text(source, lang)
            if translated and _set_field(obj, field_base, lang, translated, overwrite):
                results[attr] = translated
                changed = True
            else:
                results[attr] = "failed"

    if changed:
        obj.save()

    return results