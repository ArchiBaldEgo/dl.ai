"""Автодополнение промптов по провалам DL — сервис ARM «Отладка промтов».

Единственный владелец двух вещей:

* текст задания «редактору» (``build_debug_message``) — модель получает
  промпт, проваленный код, комментарий DL и данные теста и возвращает
  короткое дополнение промпта;
* запись дополнений в промпт (``commit_prompt_additions``) — append-only,
  под ``select_for_update`` и с ACL ``can_edit_prompt``.

Раннер (``ai/arm_runner.py``) только оркестрирует: спрашивает редактора,
вызывает коммит, складывает отчёт в ``AIModelTestResult.prompt_debug``.
Бизнес-логики правки промпта в раннере нет (CLAUDE.md, SRP/DRY).
"""

from __future__ import annotations

import re

from django.db import transaction

from ..i18n import get_localized_name, get_localized_text
from ..models import Prompt

#: Заголовок блока дополнений в тексте промпта (добавляется один раз).
PROMPT_DEBUG_HEADER = "Дополнительные требования (отладка промтов):"

#: Потолок одного дополнения — промпт должен оставаться коротким.
PROMPT_DEBUG_ADDITION_MAX = 1200

#: Минимум осмысленного дополнения после чистки.
PROMPT_DEBUG_ADDITION_MIN = 15

#: Бюджет входных блоков задания редактору (символов суммарно): промпт и
#: данные теста бывают очень длинными, а ответ нужен короткий.
DEBUG_MESSAGE_BUDGET = 6000

_HEADER_RE = re.compile(
    r"^\s*(?:текст\s+дополнения|дополнение\s+промпта|дополнение|"
    r"дополнительные\s+требования|требования\s+к\s+промпту)\s*[:：]\s*",
    re.IGNORECASE,
)
_FENCE_RE = re.compile(r"```[a-zA-Z0-9_-]*\n?(.*?)```", re.DOTALL)
_WS_RE = re.compile(r"\s+")


def empty_payload(*, prompt=None, max_attempts=0, skipped=False, skip_reason="") -> dict:
    """Пустой контракт ``AIModelTestResult.prompt_debug`` (единый для UI/БД)."""
    return {
        "prompt_id": getattr(prompt, "pk", None),
        "prompt_name": (
            get_localized_name(prompt, "Русский", "prompt_name") if prompt is not None else ""
        ),
        "max_attempts": int(max_attempts or 0),
        "attempts": [],
        "committed": False,
        "commit_error": "",
        "usage": {"duration": 0.0, "tokens": 0, "prompt_tokens": 0},
        "verification": {},
        "skipped": bool(skipped),
        "skip_reason": skip_reason or "",
    }


def payload_state(payload) -> dict:
    """Компактная сводка payload для лёгких снапшотов (без тяжёлого тела).

    Сами попытки/дополнения/комментарии DL живут только в ``prompt_debug``
    строки и отдаются body-эндпоинтом при раскрытии карточки; снапшот
    страницы/поллинга несёт лишь эти поля — по ним рендерер понимает, что
    блок «Отладка промпта» появился или изменился (проверка приходит позже).
    """
    if not isinstance(payload, dict) or not payload:
        return {}
    verification = payload.get("verification") or {}
    return {
        "committed": bool(payload.get("committed")),
        "skipped": bool(payload.get("skipped")),
        "attempts": len(payload.get("attempts") or []),
        "verification": verification.get("verdict") or "",
    }


def _normalized(text: str) -> str:
    """Ключ сравнения дополнений: регистр и пробелы не значимы."""
    return _WS_RE.sub(" ", (text or "").casefold()).strip()


def _contains_addition(haystack: str, addition: str) -> bool:
    """Есть ли дополнение в тексте (с точностью до регистра/пробелов)."""
    needle = _normalized(addition)
    return bool(needle) and needle in _normalized(haystack)


def sanitize_addition(text) -> str:
    """Привести ответ модели к чистому тексту дополнения.

    Снимает markdown-ограждения и служебные метки («ТЕКСТ ДОПОЛНЕНИЯ:»),
    схлопывает пробелы (дополнение — связный текст, а не список) и режет по
    ``PROMPT_DEBUG_ADDITION_MAX``. Короткий/пустой ответ — пустая строка.
    """
    raw = str(text or "").strip()
    if not raw:
        return ""
    fenced = _FENCE_RE.findall(raw)
    if fenced:
        raw = "\n\n".join(part.strip() for part in fenced if part.strip()) or raw
    raw = _HEADER_RE.sub("", raw)
    # Ответ бывает обёрнут в кавычки или начинается с буллета.
    raw = raw.strip(" \t\r\n\"'«»")
    raw = re.sub(r"^[-–—•*]\s*", "", raw)
    raw = _WS_RE.sub(" ", raw).strip()
    if len(raw) > PROMPT_DEBUG_ADDITION_MAX:
        raw = raw[: PROMPT_DEBUG_ADDITION_MAX].rstrip()
    if len(raw) < PROMPT_DEBUG_ADDITION_MIN:
        return ""
    return raw


def _clip(text, limit: int) -> str:
    """Обрезка блока с многоточием (не рвём посреди «слова» без нужды)."""
    value = (text or "").strip()
    if limit <= 0:
        return ""
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"


def format_test_data(test_data, limit: int = 1400) -> str:
    """Данные проваленных тестов текстом (вход + ожидаемый ответ)."""
    entries = []
    for entry in test_data or []:
        if not isinstance(entry, dict):
            continue
        number = entry.get("test_number")
        chunk = []
        if number not in (None, ""):
            chunk.append(f"Тест {number}")
        input_text = _clip(entry.get("input"), 500)
        expected = _clip(entry.get("expected_output"), 500)
        if input_text:
            chunk.append("Входные данные:\n" + input_text)
        if expected:
            chunk.append("Ожидаемый ответ:\n" + expected)
        if chunk:
            entries.append("\n".join(chunk))
    return _clip("\n\n".join(entries), limit)


def build_debug_message(
    *,
    current_prompt: str,
    task_statement: str = "",
    code: str = "",
    dl_comment: str = "",
    raw_response: str = "",
    test_data=None,
) -> str:
    """Задание модели-«редактору»: допиши промпт, устранив именно эту ошибку."""
    intro = (
        "Ты помогаешь улучшать промпт, по которому другая модель решает учебную "
        "задачу на DL. Пара «задача × модель» НЕ прошла проверку DL.\n"
        "Допиши к промпту то, чего в нём не хватает, чтобы модель не повторила "
        "именно эту ошибку.\n\n"
        "Правила:\n"
        "— Не переписывай промпт, ничего не удаляй и не меняй существующие пункты.\n"
        "— Допиши 1–3 коротких предложения (не более "
        f"{PROMPT_DEBUG_ADDITION_MAX} символов).\n"
        "— Только текст дополнения: без markdown, без списков, без примеров кода, "
        "без пояснений и без повторения уже сказанного.\n"
        "— Формулируй как требование к модели («Проверяй…», «Учитывай…»), по-русски.\n\n"
    )
    sections = [
        ("ТЕКУЩИЙ ПРОМПТ", current_prompt, 2200),
        ("КОММЕНТАРИЙ DL (ОШИБКА)", dl_comment, 1200),
        ("ДАННЫЕ ПРОВАЛЕННОГО ТЕСТА", format_test_data(test_data), 1400),
        ("КОД МОДЕЛИ", code, 1300),
        ("УСЛОВИЕ ЗАДАЧИ", task_statement, 800),
        ("ОТВЕТ МОДЕЛИ (ФРАГМЕНТ)", raw_response, 600),
    ]
    remaining = DEBUG_MESSAGE_BUDGET
    body = []
    for title, text, limit in sections:
        chunk = _clip(text, min(limit, remaining))
        if not chunk:
            continue
        body.append(f"=== {title} ===\n{chunk}")
        remaining -= len(chunk)
        if remaining <= 0:
            break
    ending = (
        "\n\nВерни ТОЛЬКО текст дополнения к промпту — без меток, кавычек и пояснений."
    )
    return intro + "\n\n".join(body) + ending


def resolve_edit_target(prompt) -> tuple[str, str]:
    """Куда писать дополнения и от какого текста отталкиваться.

    Приоритет тот же, что у ``Prompt.get_effective_text``: заданный
    ``prompt_text_override`` → ``shared_prompt`` → ``prompt_text_ru``.
    Для общего промпта берётся СЫРОЙ текст (без подстановки плейсхолдеров),
    иначе ``{message}`` первого прогона навсегда застыло бы в override.
    """
    if (prompt.prompt_text_override or "").strip():
        return "prompt_text_override", prompt.prompt_text_override
    if prompt.shared_prompt_id:
        shared = prompt.shared_prompt
        base = get_localized_text(shared, "Русский", "prompt_text") or shared.prompt_text_ru
        return "prompt_text_override", base or ""
    return "prompt_text_ru", prompt.prompt_text_ru or ""


def _can_edit_prompt(user, obj) -> bool:
    from ..admin.permissions import can_edit_prompt

    return can_edit_prompt(user, obj)


def commit_prompt_additions(prompt_id, additions, *, user) -> tuple[bool, str, dict]:
    """Дописать успешные дополнения в промпт. Возвращает ``(ok, error, info)``.

    Append-only и идемпотентно: уже присутствующее дополнение (с точностью до
    регистра и пробелов) не дублируется. Запись под ``select_for_update`` —
    одновременно пишут несколько model-потоков и, возможно, другой прогон.
    """
    cleaned = []
    for addition in additions or []:
        clean = sanitize_addition(addition)
        if clean:
            cleaned.append(clean)
    if not cleaned:
        return False, "Нет текста дополнения для сохранения", {}

    with transaction.atomic():
        prompt = (
            Prompt.objects.select_for_update()
            .select_related("shared_prompt")
            .filter(pk=prompt_id)
            .first()
        )
        if prompt is None:
            return False, "Промпт не найден", {}
        if not _can_edit_prompt(user, prompt):
            name = get_localized_name(prompt, "Русский", "prompt_name")
            return False, f"Нет прав на редактирование промпта «{name}»", {}

        field, base = resolve_edit_target(prompt)
        text = (base or "").rstrip()
        appended, already = [], []
        heading_present = _normalized(PROMPT_DEBUG_HEADER) in _normalized(text)
        for addition in cleaned:
            if _contains_addition(text, addition):
                already.append(addition)
                continue
            # Заголовок — один раз, перед первым дополнением.
            if not heading_present:
                text = (text + "\n\n" + PROMPT_DEBUG_HEADER) if text else PROMPT_DEBUG_HEADER
                heading_present = True
            text += "\n- " + addition
            appended.append(addition)

        if appended:
            setattr(prompt, field, text.rstrip())
            prompt.save(update_fields=[field])

    return True, "", {
        "prompt_id": prompt.pk,
        "prompt_name": get_localized_name(prompt, "Русский", "prompt_name"),
        "target_field": field,
        "appended": appended,
        "already_present": already,
    }
