"""Перевод markdown-документации (глава «Инструкция для пользователя»)
на язык интерфейса (en/fr) через deep-translator (Google Translate).

Используется ai/services/docs.py для модалки «?» в чате. Админ-страницы
документации остаются на русском. Структура документа сохраняется:

- fenced code blocks (``` … ```, ~~~ … ~~~) НЕ переводятся;
- таблицы (подряд идущие строки с ведущим «|», минимум 2) НЕ переводятся;
- в текстовых блоках защищаются {placeholder}-ы, инлайн-код `…`,
  markdown-ссылки [текст](url) и маркеры заголовков «## »;
- блок, перевод которого не удался (ошибка Google/сети), остаётся
  в оригинале (русском) — документ целиком не теряется.

Возврат (текст, failed_count): failed_count > 0 означает частичный перевод —
такой результат не кэшируется (см. docs.py).
"""

import logging
import re
import time
from typing import Optional

from .auto_translate import (
    _ERROR_PATTERNS,
    _GOOGLE_LANG_MAP,
    _protect_placeholders,
    _restore_placeholders,
    _translate_chunk,
)

logger = logging.getLogger(__name__)

# Текстовый блок без кириллицы переводить не нужно (пустые разделители,
# чисто английские фрагменты) — экономим запросы к Google и не рискуем.
_CYRILLIC_RE = re.compile(r"[а-яё]", re.IGNORECASE)

# Начало/конец fenced code block (``` или ~~~, возможен отступ).
_CODE_FENCE_RE = re.compile(r"^\s*(```|~~~)")
# Строка GFM-таблицы (ведущий «|»).
_TABLE_LINE_RE = re.compile(r"^\s*\|")

# Защищаемые конструкции текстового блока: инлайн-код, markdown-ссылка
# целиком (Google может сломать структуру скобок), маркер заголовка.
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
_MD_LINK_RE = re.compile(r"\[[^\]\n]*\]\([^)\n]*\)")
_HEADING_MARK_RE = re.compile(r"(?m)^(#{1,6} )")

# Ограничение на кусок для Google: deep-translator/Google надёжны до ~5k,
# берём консервативные 2000, как в auto_translate.
_MAX_CHUNK_LEN = 2000

_CHUNK_RETRY_SLEEP_SECONDS = 1


def split_markdown_blocks(text: str) -> list[tuple[str, str]]:
    """Разбить markdown на блоки: [("code"|"table"|"text", блок), ...].

    Блоки покрывают весь текст подряд (конкатенация блоков через "\n"
    восстанавливает исходный текст ровно).
    """
    lines = text.split("\n")
    n = len(lines)
    blocks: list[tuple[str, str]] = []
    i = 0
    while i < n:
        fence = _CODE_FENCE_RE.match(lines[i])
        if fence:
            closing = fence.group(1)
            j = i + 1
            while j < n and not lines[j].strip().startswith(closing):
                j += 1
            j = min(j + 1, n)  # захватываем закрывающую ограду (или EOF)
            blocks.append(("code", "\n".join(lines[i:j])))
            i = j
            continue
        if _TABLE_LINE_RE.match(lines[i]):
            j = i
            while j < n and _TABLE_LINE_RE.match(lines[j]):
                j += 1
            if j - i >= 2:  # минимум 2 строки — иначе это просто текст с «|»
                blocks.append(("table", "\n".join(lines[i:j])))
                i = j
                continue
        # Текст (в т.ч. одиночная «|»-строка): копим до ограды или таблицы.
        # Текущая строка уже не ограда и не таблица ≥2 строк — стартуем с i+1.
        j = i + 1
        while j < n and not _CODE_FENCE_RE.match(lines[j]) and not _TABLE_LINE_RE.match(lines[j]):
            j += 1
        blocks.append(("text", "\n".join(lines[i:j])))
        i = j
    return blocks


def _protect_markdown(text: str) -> tuple[str, list]:
    """Защитить переводимую разметку от Google.

    Тот же контракт токенов, что у auto_translate._protect_placeholders:
    @@PHi@@ (Google иногда понижает регистр — _restore_placeholders
    обрабатывает и это). Порядок важен: инлайн-код раньше ссылок и заголовков.
    """
    protected, placeholders = _protect_placeholders(text)
    for pattern in (_INLINE_CODE_RE, _MD_LINK_RE, _HEADING_MARK_RE):
        for match in pattern.findall(protected):
            index = len(placeholders)
            placeholders.append(match)
            protected = protected.replace(match, f"@@PH{index}@@", 1)
    return protected, placeholders


def _chunk_ok(raw: str) -> bool:
    low = raw.lower()
    return not any(pattern in low for pattern in _ERROR_PATTERNS)


def _split_text_block(text: str, max_len: int = _MAX_CHUNK_LEN) -> list[tuple[str, str]]:
    """Разбить текстовый блок на куски ≤ max_len с сохранением структуры.

    Возвращаются пары (разделитель_перед_куском, кусок); конкатенация
    "joiner + chunk" восстанавливает исходный текст ровно (в отличие от
    auto_translate._split_by_paragraphs, теряющего разделители).
    """
    parts: list[tuple[str, str]] = []

    def flush_paragraph(para: str, joiner: str) -> None:
        buf = None
        for line in para.split("\n"):
            if buf is None:
                buf = line
            elif len(buf) + 1 + len(line) <= max_len:
                buf += "\n" + line
            else:
                parts.append((joiner, buf))
                joiner = "\n"
                buf = line
        if buf is None:
            return
        if len(buf) > max_len:
            # Одиночная строка длиннее лимита — жёсткий разрез, склейка "".
            for k in range(0, len(buf), max_len):
                parts.append((joiner, buf[k:k + max_len]))
                joiner = ""
        elif buf:
            parts.append((joiner, buf))

    for index, para in enumerate(text.split("\n\n")):
        joiner = "\n\n" if index else ""
        if len(para) <= max_len:
            parts.append((joiner, para))
        else:
            flush_paragraph(para, joiner)
    return parts


def _translate_chunk_safe(chunk: str, google_lang: str) -> str:
    """Перевести кусок с одним ретраем после сбоя/паузы (как в auto_translate)."""
    try:
        raw = _translate_chunk(chunk, google_lang)
    except Exception as exc:
        logger.error("Doc translation chunk error (%d chars, lang=%s): %s",
                     len(chunk), google_lang, exc)
        raw = ""
    if raw and _chunk_ok(raw):
        return raw
    time.sleep(_CHUNK_RETRY_SLEEP_SECONDS)
    try:
        raw = _translate_chunk(chunk, google_lang)
    except Exception as exc:
        logger.error("Doc translation chunk retry error (%d chars, lang=%s): %s",
                     len(chunk), google_lang, exc)
        raw = ""
    return raw if (raw and _chunk_ok(raw)) else ""


def _translate_text_block(block: str, google_lang: str) -> Optional[str]:
    """Перевести один текстовый markdown-блок; None — перевод не удался."""
    protected, placeholders = _protect_markdown(block)
    try:
        translated_parts = []
        for joiner, chunk in _split_text_block(protected):
            if not chunk.strip():
                translated_parts.append(joiner + chunk)
                continue
            raw = _translate_chunk_safe(chunk, google_lang)
            if not raw:
                logger.error("Doc translation chunk failed (%d chars, lang=%s)",
                             len(chunk), google_lang)
                return None
            translated_parts.append(joiner + raw)
        return _restore_placeholders("".join(translated_parts), placeholders)
    except Exception as exc:
        logger.error("Doc translation of text block failed: %s", exc)
        return None


def translate_markdown(text: str, target_lang: str) -> tuple[str, int]:
    """Перевести markdown-документ на target_lang (en/fr).

    Returns (текст, failed_count): failed_count > 0 — часть блоков осталась
    на русском (перевод не удался). Для "ru"/пустого текста — без изменений.
    """
    if not text or not text.strip():
        return text, 0
    google_lang = _GOOGLE_LANG_MAP.get(target_lang)
    if not google_lang or target_lang == "ru":
        return text, 0

    translated_blocks = []
    failed = 0
    for kind, block in split_markdown_blocks(text):
        if kind != "text" or not _CYRILLIC_RE.search(block):
            translated_blocks.append(block)
            continue
        translated = _translate_text_block(block, google_lang)
        if translated is None:
            failed += 1
            translated_blocks.append(block)
        else:
            translated_blocks.append(translated)
    return "\n".join(translated_blocks), failed