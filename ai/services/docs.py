"""Сервис документации: главы из DOCX.md и техдока для разработчика.

Единственный источник правды о том, какие главы документации существуют,
кому они видны и как превращаются в HTML/скачиваемый .md. Admin-вью
(`ai/admin/docs.py`) только проверяет права и рендерит; чат-эндпоинт
(`ai/views.py`) берёт главу `user` для модалки в шапке.

Файлы читаются с диска относительно BASE_DIR. В Docker образ *.md и doc/
исключены .dockerignore, но compose монтирует `.:/app`, поэтому файлы
доступны в рантайме; при отсутствии файла сервис отвечает
DocUnavailableError, вью превращает её в дружелюбную 404.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from django.conf import settings
from django.utils.html import escape

# Ранг роли: чем больше, тем шире доступ. Фильтрация глав идёт сравнением
# рангов, без ветвлений по конкретным главам.
_ROLE_RANKS = {"any": 0, "staff": 1, "superuser": 2}

# Разделитель глав DOCX.md — только строки «## Инструкция для …». Внутри
# приложений тестера/сисадмина встречаются одиночные «# …» и нумерованные
# «## 1. …» — по ним резать нельзя.
_DOCX_CHAPTER_RE = re.compile(r"^## Инструкция для ", re.MULTILINE)


class DocUnavailableError(Exception):
    """Файл документации отсутствует (образ без *.md и без bind-mount)."""


@dataclass(frozen=True)
class DocChapter:
    slug: str
    title: str
    # ("docx", heading) — глава DOCX.md по H2-заголовку;
    # ("file", relative_path) — отдельный файл от BASE_DIR.
    source: tuple[str, str]
    # "any" — всем в админке; "staff" — is_staff_or_superuser;
    # "superuser" — только суперюзер. Глава "user" видна только в чате.
    min_role: str = "any"
    in_admin_nav: bool = True

    @property
    def role_rank(self) -> int:
        return _ROLE_RANKS[self.min_role]


# slug → глава. Порядок словаря задаёт порядок в списке глав и навигации.
_CHAPTERS = {
    "user": DocChapter(
        slug="user",
        title="Инструкция для пользователя",
        source=("docx", "Инструкция для пользователя"),
        min_role="any",
        in_admin_nav=False,
    ),
    "prompt-developer": DocChapter(
        slug="prompt-developer",
        title="Инструкция для разработчика промптов",
        source=("docx", "Инструкция для разработчика промптов"),
    ),
    "developer": DocChapter(
        slug="developer",
        title="Документация для разработчика",
        source=("file", "doc/Документация для разработчика.md"),
        min_role="staff",
    ),
    "superuser": DocChapter(
        slug="superuser",
        title="Инструкция для суперадмина",
        source=("docx", "Инструкция для суперадмина"),
        min_role="superuser",
    ),
}


def get_chapter(slug: str) -> Optional[DocChapter]:
    return _CHAPTERS.get(slug)


def all_chapters() -> list[DocChapter]:
    return list(_CHAPTERS.values())


def _base_dir() -> Path:
    return Path(settings.BASE_DIR)


def _read_file(path: Path) -> str:
    if not path.is_file():
        raise DocUnavailableError(f"Файл документации не найден: {path.name}")
    return path.read_text(encoding="utf-8")


def _extract_docx_chapter(heading: str) -> str:
    """Вернуть текст главы DOCX.md от её H2-заголовка до следующего H2 «Инструкция для …»."""
    text = _read_file(_base_dir() / "DOCX.md")
    matches = list(_DOCX_CHAPTER_RE.finditer(text))
    for i, match in enumerate(matches):
        line_end = text.find("\n", match.start())
        if line_end == -1:
            line_end = len(text)
        title_line = text[match.start():line_end].strip()
        if title_line == f"## {heading}":
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            return text[match.start():end].rstrip() + "\n"
    raise DocUnavailableError(f"Глава не найдена в DOCX.md: {heading}")


def _chapter_markdown(chapter: DocChapter) -> str:
    kind, arg = chapter.source
    if kind == "docx":
        return _extract_docx_chapter(arg)
    return _read_file(_base_dir() / arg)


def visible_chapter_slugs(*, is_superuser: bool, is_staff: bool, is_authenticated: bool) -> list[str]:
    """Slugs глав, видимых пользователю с данными флагами, в порядке реестра."""
    if not is_authenticated:
        return []
    rank = _ROLE_RANKS["superuser"] if is_superuser else (_ROLE_RANKS["staff"] if is_staff else _ROLE_RANKS["any"])
    return [c.slug for c in _CHAPTERS.values() if c.role_rank <= rank]


def _chapter_source_path(chapter: DocChapter) -> Path:
    kind, arg = chapter.source
    return (_base_dir() / "DOCX.md") if kind == "docx" else (_base_dir() / arg)


def chapter_fingerprint(slug: str) -> tuple[int, int]:
    """(mtime, size) исходника главы — дешёвая проверка «файл изменился».

    Используется фронтендом (поллинг) и ключом кэша перевода: правка файла
    меняет отпечаток → старый кэш переводов перестаёт использоваться.
    """
    chapter = _CHAPTERS.get(slug)
    if chapter is None:
        raise DocUnavailableError(f"Неизвестная глава: {slug}")
    path = _chapter_source_path(chapter)
    if not path.is_file():
        raise DocUnavailableError(f"Файл документации не найден: {path.name}")
    st = path.stat()
    return (int(st.st_mtime), int(st.st_size))


# Локализованные заголовки главы «user» (модалка «?» в чате); остальные
# главы админки остаются на русском.
_USER_CHAPTER_TITLES = {
    "en": "User Guide",
    "fr": "Guide de l'utilisateur",
}

# Кэш переведённого markdown: правка файла меняет fingerprint → ключ.
_DOC_CACHE_TTL = 7 * 24 * 3600


def _chapter_title(chapter: DocChapter, lang: str = "ru") -> str:
    if chapter.slug == "user" and lang != "ru":
        return _USER_CHAPTER_TITLES.get(lang, chapter.title)
    return chapter.title


def _doc_cache_key(slug: str, lang: str, fingerprint: tuple[int, int]) -> str:
    from ..constants import AI_CACHE_KEY_PREFIX

    return f"{AI_CACHE_KEY_PREFIX}:doc-md:{slug}:{lang}:{fingerprint[0]}:{fingerprint[1]}"


def _translated_chapter_text(chapter: DocChapter, lang: str, fingerprint: tuple[int, int]) -> tuple[str, int]:
    """Переведённый markdown главы с кэшем; (текст, failed_count).

    Частичный перевод (failed_count > 0) не кэшируется — следующий запрос
    попробует перевести его целиком.
    """
    from django.core.cache import cache

    key = _doc_cache_key(chapter.slug, lang, fingerprint)
    cached = cache.get(key)
    if cached is not None:
        return cached, 0
    from .doc_translation import translate_markdown

    text, failed = translate_markdown(_chapter_markdown(chapter), lang)
    if not failed:
        cache.set(key, text, _DOC_CACHE_TTL)
    return text, failed


def read_chapter_markdown(slug: str, lang: str = "ru") -> dict:
    """Markdown-текст главы для скачивания: {title, text, filename}.

    lang != "ru" — текст переводится (ai/services/doc_translation.py);
    блок, который перевести не удалось, остаётся в оригинале.
    """
    chapter = _CHAPTERS.get(slug)
    if chapter is None:
        raise DocUnavailableError(f"Неизвестная глава: {slug}")
    if lang and lang != "ru":
        text, _failed = _translated_chapter_text(chapter, lang, chapter_fingerprint(slug))
    else:
        text = _chapter_markdown(chapter)
    return {
        "title": _chapter_title(chapter, lang),
        "text": text,
        "filename": f"{_chapter_title(chapter, lang)}.md",
    }


def _markdown_to_html(text: str) -> str:
    try:
        import markdown  # пакет из requirements.txt; отсутствует — см. fallback
        return markdown.markdown(
            text, extensions=["tables", "fenced_code"], output_format="html5"
        )
    except ImportError:
        return f"<pre>{escape(text)}</pre>"


def render_chapter_html(slug: str, lang: str = "ru") -> dict:
    """HTML главы: {title, html, lang, fingerprint}.

    lang != "ru" — markdown переводится; перевод кэшируется ключом
    (slug, lang, fingerprint), правка файла меняет ключ → свежий перевод.
    """
    chapter = _CHAPTERS.get(slug)
    if chapter is None:
        raise DocUnavailableError(f"Неизвестная глава: {slug}")
    fingerprint = chapter_fingerprint(slug)
    title = _chapter_title(chapter, lang)
    if lang and lang != "ru":
        text, _failed = _translated_chapter_text(chapter, lang, fingerprint)
    else:
        text = _chapter_markdown(chapter)
    return {"title": title, "html": _markdown_to_html(text), "lang": lang, "fingerprint": fingerprint}