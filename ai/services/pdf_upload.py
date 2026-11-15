"""Строгая загрузка PDF в чат: валидация + извлечение ТОЛЬКО текста.

Единственный владелец правила «что считается безопасным PDF». В чат
попадает лишь текст, извлечённый сервером; сам файл не сохраняется и не
отдаётся обратно. Загрузка разрешена только моделям со зрением
(``ai/model_clients/registry.py`` :: ``capabilities``; у личных моделей —
``UserModel.supports_vision``) и только в режиме чата.

Почему так: модель не выполняет PDF, но вредоносный документ может нести
JavaScript/OpenAction/встроенные файлы/XFA. Мы их отвергаем ДО извлечения
текста, а в модель уходит только очищенная текстовая выжимка.
"""

import os
import re
from io import BytesIO

from django.conf import settings

from ..model_clients import registry
from .user_models import find_user_model

# Браузеры шлют application/pdf; некоторые — octet-stream. Решает не MIME,
# а magic-байты ниже, поэтому набор — лишь грубый предфильтр.
_ALLOWED_MIME = {
    "application/pdf",
    "application/x-pdf",
    "application/octet-stream",
}

_MAX_PAGES = 50
_MAX_CHARS = 60000
_MAX_NAME = 120

# Активное содержимое PDF: исполняемые действия и вложения. Даже если
# просмотрщик их не исполнит, такой файл в чате не нужен.
_DANGEROUS_ROOT_KEYS = (
    "/OpenAction",
    "/AA",
    "/JavaScript",
    "/JS",
    "/XFA",
    "/Launch",
    "/SubmitForm",
    "/ImportData",
    "/RichMedia",
)
_DANGEROUS_ACTION_TYPES = {
    "/JavaScript",
    "/Launch",
    "/SubmitForm",
    "/ImportData",
    "/RichMedia",
}

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SPACES_RE = re.compile(r"[ \t]+\n")
_BLANKLINES_RE = re.compile(r"\n{3,}")


class PdfValidationError(Exception):
    """Пользовательская ошибка загрузки: code — ключ i18n (pdf_*)."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def max_size_mb():
    try:
        return int(getattr(settings, "AI_PDF_MAX_SIZE_MB", 10))
    except (TypeError, ValueError):
        return 10


def sanitize_filename(name):
    """Безопасная подпись файла: без путей и управляющих символов."""
    base = os.path.basename(str(name or "").replace("\\", "/"))
    base = _CONTROL_RE.sub("", base).strip()
    return base[:_MAX_NAME] or "document.pdf"


def sanitize_text(text):
    """Нормализация извлечённого текста (без исполняемой разметки)."""
    text = str(text or "").replace("\x00", "")
    text = _CONTROL_RE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _SPACES_RE.sub("\n", text)
    text = _BLANKLINES_RE.sub("\n\n", text)
    return text.strip()


def _reject_active_content(reader):
    """Отвергнуть PDF с активным содержимым до извлечения текста."""
    try:
        root = reader.trailer.get("/Root")
    except Exception as exc:  # повреждённый trailer
        raise PdfValidationError("pdf_bad") from exc
    if root is not None:
        for key in _DANGEROUS_ROOT_KEYS:
            if key in root:
                raise PdfValidationError("pdf_active_content")
        names = root.get("/Names")
        if names is not None and "/EmbeddedFiles" in names:
            raise PdfValidationError("pdf_active_content")
        acro = root.get("/AcroForm")
        if acro is not None and "/XFA" in acro:
            raise PdfValidationError("pdf_active_content")
    try:
        # Только страницы, которые реально уйдут в модель: активное содержимое
        # дальше _MAX_PAGES не попадает в извлечённый текст.
        for page in reader.pages[:_MAX_PAGES]:
            annots = page.get("/Annots")
            if not annots:
                continue
            for annot_ref in annots:
                annot = annot_ref.get_object() if hasattr(annot_ref, "get_object") else annot_ref
                if not isinstance(annot, dict):
                    continue
                if "/AA" in annot:
                    raise PdfValidationError("pdf_active_content")
                action = annot.get("/A")
                if action is not None and "/S" in action:
                    if str(action.get("/S")) in _DANGEROUS_ACTION_TYPES:
                        raise PdfValidationError("pdf_active_content")
    except PdfValidationError:
        raise
    except Exception:
        # Несмогли разобрать аннотации — не доверяем файлу.
        raise PdfValidationError("pdf_bad")


def validate_pdf_upload(uploaded_file):
    """Проверить загруженный файл и вернуть ``{"name","pages","text","truncated"}``.

    Бросает ``PdfValidationError`` с кодом ``pdf_only``/``pdf_too_large``/
    ``pdf_bad``/``pdf_encrypted``/``pdf_active_content``/``pdf_no_text``.
    """
    if uploaded_file is None:
        raise PdfValidationError("pdf_only")
    name = sanitize_filename(getattr(uploaded_file, "name", ""))
    if not name.lower().endswith(".pdf"):
        raise PdfValidationError("pdf_only")
    content_type = str(getattr(uploaded_file, "content_type", "") or "").lower()
    if content_type and content_type not in _ALLOWED_MIME:
        raise PdfValidationError("pdf_only")

    limit = max_size_mb() * 1024 * 1024
    try:
        size = int(getattr(uploaded_file, "size", 0) or 0)
    except (TypeError, ValueError):
        size = 0
    if size > limit:
        raise PdfValidationError("pdf_too_large")
    data = uploaded_file.read(limit + 1)
    if not data:
        raise PdfValidationError("pdf_bad")
    if len(data) > limit:
        raise PdfValidationError("pdf_too_large")

    # Magic-байты: настоящий PDF начинается с "%PDF-" и заканчивается %%EOF
    # (в пределах последнего килобайта — там могут быть хвостовые пробелы).
    if not data.startswith(b"%PDF-"):
        raise PdfValidationError("pdf_only")
    if b"%%EOF" not in data[-2048:]:
        raise PdfValidationError("pdf_bad")

    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - зависимость в requirements
        raise PdfValidationError("pdf_bad") from exc

    try:
        reader = PdfReader(BytesIO(data), strict=False)
    except Exception as exc:
        raise PdfValidationError("pdf_bad") from exc

    if getattr(reader, "is_encrypted", False):
        raise PdfValidationError("pdf_encrypted")
    try:
        pages_total = int(reader.get_num_pages())
    except Exception as exc:
        raise PdfValidationError("pdf_bad") from exc
    if pages_total <= 0:
        raise PdfValidationError("pdf_bad")

    _reject_active_content(reader)

    chunks = []
    used_pages = min(pages_total, _MAX_PAGES)
    try:
        for index in range(used_pages):
            page_text = reader.pages[index].extract_text() or ""
            if page_text:
                chunks.append(page_text)
    except Exception as exc:
        raise PdfValidationError("pdf_bad") from exc

    text = sanitize_text("\n".join(chunks))
    if not text:
        raise PdfValidationError("pdf_no_text")
    truncated = len(text) > _MAX_CHARS
    if truncated:
        text = text[:_MAX_CHARS].rstrip()

    return {
        "name": name,
        "pages": pages_total,
        "text": text,
        "truncated": truncated,
    }


def pdf_supports_vision(model_key, user=None):
    """Умеет ли модель принимать изображения (и, значит, PDF-текст-контекст)."""
    key = str(model_key or "").strip()
    if not key:
        return False
    if key.startswith("User_"):
        model = find_user_model(user, key) if user is not None else None
        return bool(model and model.supports_vision)
    return bool(registry.capabilities(key).get("vision"))


def _pdf_labels(language):
    lang = str(language or "").strip()
    if lang in ("English", "Английский"):
        return "PDF file content", "page(s)", "truncated"
    if lang in ("French", "Французский"):
        return "Contenu du fichier PDF", "page(s)", "tronqué"
    return "Содержимое PDF-файла", "стр.", "обрезано"


def build_pdf_block(pdf_info, language=""):
    """Текстовый блок PDF для сообщения модели (или "" если нечего добавить).

    Повторно санитизирует клиентские данные: WS-payload приходит из браузера,
    доверять полю ``pdf.text`` нельзя.
    """
    if not isinstance(pdf_info, dict):
        return ""
    text = sanitize_text(pdf_info.get("text") or "")
    if not text:
        return ""
    if len(text) > _MAX_CHARS:
        text = text[:_MAX_CHARS].rstrip()
    name = sanitize_filename(pdf_info.get("name") or "document.pdf")
    label, pages_word, truncated_word = _pdf_labels(language)
    pages = pdf_info.get("pages")
    try:
        pages = int(pages)
    except (TypeError, ValueError):
        pages = 0
    meta = f"{name}"
    if pages > 0:
        meta += f" ({pages} {pages_word})"
    if pdf_info.get("truncated"):
        meta += f" — {truncated_word}"
    return f"[{label}: {meta}]\n{text}"
