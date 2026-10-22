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
