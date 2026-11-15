"""Shared solution/answer grading helpers for ARM batch-solve.

Centralizes the text normalization and comparison logic so that all flows that
verdict model answers against an expected solution share a single
implementation (DRY). All comparators are deterministic: no LLM-as-judge here.
"""

from __future__ import annotations

import difflib
import re

# Default fuzzy-match threshold for the ``ratio`` comparator (solve mode).
SOLVE_RATIO_THRESHOLD = 0.85

# Comment patterns removed during solution normalization. Order matters: block
# comments are stripped before line comments. Pascal uses { ... } and (* ... *);
# C-like uses /* ... */ and //; Python/shell use #.
_PASCAL_BRACE_COMMENT = re.compile(r"\{.*?\}", re.DOTALL)
_PASCAL_PAREN_COMMENT = re.compile(r"\(\*.*?\*\)", re.DOTALL)
_C_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT = re.compile(r"(//|#).*?$", re.MULTILINE)
_WHITESPACE = re.compile(r"\s+")


def normalize_solution(text: str) -> str:
    """Нормализует текст решения: удаляет комментарии, приводит к нижнему регистру, сжимает пробелы.

    Поддерживает комментарии Pascal ({...}, (*...*)), C-стиля (/*...*/, //) и Python/shell (#).
    """
    if not text:
        return ""
    out = _PASCAL_BRACE_COMMENT.sub(" ", text)
    out = _PASCAL_PAREN_COMMENT.sub(" ", out)
    out = _C_BLOCK_COMMENT.sub(" ", out)
    out = _LINE_COMMENT.sub(" ", out)
    out = _WHITESPACE.sub(" ", out).strip().lower()
    return out


def grade_solution(model_text: str, sample_text: str, threshold: float = SOLVE_RATIO_THRESHOLD) -> str:
    """Приблизительная оценка решения для ARM: solved / failed / skipped.

    - пустой эталон → SKIPPED (нет с чем сравнивать)
    - пустой ответ модели → FAILED
    - нормализованное равенство → SOLVED
    - difflib ratio >= threshold → SOLVED, иначе FAILED
    """
    sample_norm = normalize_solution(sample_text)
    if not sample_norm:
        return "skipped"
    model_norm = normalize_solution(model_text)
    if not model_norm:
        return "failed"
    if model_norm == sample_norm:
        return "solved"
    ratio = difflib.SequenceMatcher(None, model_norm, sample_norm).ratio()
    return "solved" if ratio >= threshold else "failed"


# --- «В чём ошибка»: детерминированная проверка строки КОДЫ ----------------

# Последняя строка ответа вида «КОДЫ: 3, 17» / «КОДЫ: error_3» / «КОДЫ: —».
# Регистронезависимо; берём последнее совпадение (в разборе слово «коды»
# может встречаться раньше).
_ERROR_CODES_LINE_RE = re.compile(r"^\s*коды\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_ERROR_CODE_TOKEN_RE = re.compile(r"\d+")
# Явное «ошибок нет» в строке КОДЫ (целиком, а не встречающееся в фразе
# «3, 17 — других нет»: там коды валидны).
_NO_ERRORS_RE = re.compile(
    r"[\s.!]*(?:нет(?:\s+ошибок)?|ошибок\s+нет|отсутств\w*|[-—–]+|none|no\s+errors)[\s.!]*",
    re.IGNORECASE,
)


def parse_error_codes(text: str, valid_codes: frozenset[int] | None = None) -> list[int]:
    """Коды из последней строки ``КОДЫ: …``; без строки — пустой список.

    Принимает ``3, 17``, ``3 17``, ``error_3, error_17``, ``—``/«нет».
    Коды вне каталога отбрасываются; результат отсортирован и без дублей.
    """
    from .services.find_error_catalog import VALID_ERROR_CODES

    allowed = VALID_ERROR_CODES if valid_codes is None else valid_codes
    if not text:
        return []
    matches = list(_ERROR_CODES_LINE_RE.finditer(text))
    if not matches:
        return []
    raw = matches[-1].group(1).strip()
    if not raw or _NO_ERRORS_RE.fullmatch(raw):
        return []
    codes = {
        int(token) for token in _ERROR_CODE_TOKEN_RE.findall(raw)
        if int(token) in allowed
    }
    return sorted(codes)


def compare_error_codes(actual, expected) -> dict:
    """Сравнить найденные коды с метками кейса (множества, без порядка).

    Возвращает matched/missed/extra/exact и precision/recall/F1. Пустые
    множества обрабатываются явно: если и ожиданий, и находок нет — это
    точное совпадение с метриками 1.0; если ожидания есть, а находок нет —
    recall 0; если находок нет, precision не наказывается (0/0 → 0 только
    при непустых ожиданиях).
    """
    parsed = {int(code) for code in actual or ()}
    wanted = {int(code) for code in expected or ()}
    matched = parsed & wanted
    missed = wanted - parsed
    extra = parsed - wanted
    exact = parsed == wanted
    precision = (len(matched) / len(parsed)) if parsed else (1.0 if not wanted else 0.0)
    recall = (len(matched) / len(wanted)) if wanted else (1.0 if not parsed else 0.0)
    if precision + recall:
        f1 = 2 * precision * recall / (precision + recall)
    else:
        f1 = 1.0 if exact else 0.0
    return {
        "parsed": sorted(parsed),
        "expected": sorted(wanted),
        "matched": sorted(matched),
        "missed": sorted(missed),
        "extra": sorted(extra),
        "exact": exact,
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


def aggregate_error_code_metrics(rows, valid_codes: frozenset[int] | None = None) -> dict:
    """Micro-усреднение метрик по парам «модель × кейс» + recall по кодам.

    ``rows`` — итерируемое из словарей с ``parsed_codes``/``expected_codes``
    (результаты ``compare_error_codes`` подходят напрямую). Строки-ошибки
    вызывающая сторона отфильтровывает сама.
    """
    from .services.find_error_catalog import VALID_ERROR_CODES

    allowed = VALID_ERROR_CODES if valid_codes is None else valid_codes
    total_expected = total_parsed = total_matched = total_exact = 0
    cases = 0
    per_code = {
        code: {"expected": 0, "found": 0, "missed": 0, "extra": 0}
        for code in sorted(allowed)
    }
    for row in rows:
        cases += 1
        expected = {int(code) for code in row.get("expected_codes") or row.get("expected") or ()}
        parsed = {int(code) for code in row.get("parsed_codes") or row.get("parsed") or ()}
        matched = expected & parsed
        total_expected += len(expected)
        total_parsed += len(parsed)
        total_matched += len(matched)
        if expected == parsed:
            total_exact += 1
        for code in expected:
            if code in per_code:
                per_code[code]["expected"] += 1
                if code in parsed:
                    per_code[code]["found"] += 1
                else:
                    per_code[code]["missed"] += 1
        for code in parsed - expected:
            if code in per_code:
                per_code[code]["extra"] += 1
    precision = (total_matched / total_parsed) if total_parsed else (1.0 if not total_expected else 0.0)
    recall = (total_matched / total_expected) if total_expected else (1.0 if not total_parsed else 0.0)
    if precision + recall:
        f1 = 2 * precision * recall / (precision + recall)
    else:
        f1 = 1.0 if cases and total_exact == cases else 0.0
    for stats in per_code.values():
        stats["recall"] = (
            stats["found"] / stats["expected"] if stats["expected"] else None
        )
    return {
        "cases": cases,
        "exact": total_exact,
        "exact_rate": (total_exact / cases) if cases else 0.0,
        "matched": total_matched,
        "parsed": total_parsed,
        "expected": total_expected,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "per_code": per_code,
    }
