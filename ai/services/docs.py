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


def read_chapter_markdown(slug: str) -> dict:
    """Markdown-текст главы для скачивания: {title, text, filename}."""
    chapter = _CHAPTERS.get(slug)
    if chapter is None:
        raise DocUnavailableError(f"Неизвестная глава: {slug}")
    return {
        "title": chapter.title,
        "text": _chapter_markdown(chapter),
        "filename": f"{chapter.title}.md",
    }


def render_chapter_html(slug: str) -> dict:
    """HTML главы для показа на странице: {title, html}. Markdown → HTML."""
    data = read_chapter_markdown(slug)
    try:
        import markdown  # пакет из requirements.txt; отсутствует — см. fallback
        html = markdown.markdown(
            data["text"], extensions=["tables", "fenced_code"], output_format="html5"
        )
    except ImportError:
        html = f"<pre>{escape(data['text'])}</pre>"
    return {"title": data["title"], "html": html}