"""Shared solution/answer grading helpers for ARM and prompt regression tests.

Centralizes the text normalization and comparison logic so that batch-solve ARM
(``ai/arm_runner.py``) and prompt-regression runs (``ai/prompt_test_runner.py``)
share a single implementation (DRY). All comparators are deterministic: no
LLM-as-judge here.
"""

from __future__ import annotations

import difflib
import re
from typing import List, Tuple

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
    """Strip comments, lowercase and collapse whitespace for fuzzy comparison."""
    if not text:
        return ""
    out = _PASCAL_BRACE_COMMENT.sub(" ", text)
    out = _PASCAL_PAREN_COMMENT.sub(" ", out)
    out = _C_BLOCK_COMMENT.sub(" ", out)
    out = _LINE_COMMENT.sub(" ", out)
    out = _WHITESPACE.sub(" ", out).strip().lower()
    return out


def grade_solution(model_text: str, sample_text: str, threshold: float = SOLVE_RATIO_THRESHOLD) -> str:
    """Approximate ARM verdict: solved / failed / skipped.

    - empty sample  -> SKIPPED (no oracle to compare against)
    - empty model   -> FAILED (model produced nothing)
    - normalized equal -> SOLVED
    - difflib ratio >= threshold -> SOLVED, else FAILED
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


# ---------------------------------------------------------------------------
# Prompt-regression comparators.
#
# ``compare_response`` is the single entry point used by the prompt-regression
# runner. It returns (verdict, hint, missing):
#   verdict  -> "match" | "mismatch" | "skipped"
#   hint     -> short human-readable reason (shown in the report / diff row)
#   missing  -> list of expected items not found (only meaningful for contains_all)
# ---------------------------------------------------------------------------

COMPARATOR_RATIO = "ratio"
COMPARATOR_CONTAINS_ALL = "contains_all"
COMPARATOR_EXACT = "exact"
COMPARATOR_SET = "set"

COMPARATOR_CHOICES = (
    (COMPARATOR_RATIO, "ratio (difflib)"),
    (COMPARATOR_CONTAINS_ALL, "contains_all (все строки эталона)"),
    (COMPARATOR_EXACT, "exact (нормализованное равенство)"),
    (COMPARATOR_SET, "set (равенство множеств строк)"),
)

VERDICT_MATCH = "match"
VERDICT_MISMATCH = "mismatch"
VERDICT_SKIPPED = "skipped"


def _split_lines(text: str) -> List[str]:
    """Split text into non-empty, normalized lines."""
    if not text:
        return []
    return [line for line in (normalize_solution(line) for line in text.splitlines()) if line]


def _ratio(actual_norm: str, expected_norm: str) -> float:
    if not actual_norm or not expected_norm:
        return 0.0
    return difflib.SequenceMatcher(None, actual_norm, expected_norm).ratio()


def compare_response(
    actual: str,
    expected_text: str,
    comparator: str = COMPARATOR_RATIO,
    threshold: float | None = None,
) -> Tuple[str, str, List[str]]:
    """Compare a model response to the golden expected text.

    Returns ``(verdict, hint, missing)``. An empty ``expected_text`` always
    yields ``skipped`` (no oracle). An empty model response is a ``mismatch``
    (except when the expected text is also empty -> skipped).
    """
    expected_norm = normalize_solution(expected_text)
    if not expected_norm:
        return VERDICT_SKIPPED, "нет эталона", []

    actual_norm = normalize_solution(actual)
    if not actual_norm:
        return VERDICT_MISMATCH, "пустой ответ модели", []

    if comparator == COMPARATOR_EXACT:
        if actual_norm == expected_norm:
            return VERDICT_MATCH, "", []
        return VERDICT_MISMATCH, "точное несовпадение", []

    if comparator == COMPARATOR_SET:
        actual_set = set(_split_lines(actual))
        expected_set = set(_split_lines(expected_text))
        if actual_set == expected_set:
            return VERDICT_MATCH, "", []
        missing = sorted(expected_set - actual_set)
        extra = sorted(actual_set - expected_set)
        hint = "множества не равны"
        if missing:
            hint += f"; отсутствуют: {', '.join(missing[:8])}"
        if extra:
            hint += f"; лишнее: {', '.join(extra[:8])}"
        return VERDICT_MISMATCH, hint, missing

    if comparator == COMPARATOR_CONTAINS_ALL:
        # Each non-empty normalized line of the expected text must appear as a
        # substring of the normalized model response.
        expected_lines = _split_lines(expected_text)
        missing = [line for line in expected_lines if line not in actual_norm]
        if not missing:
            return VERDICT_MATCH, "", []
        hint = f"отсутствуют: {', '.join(missing[:8])}"
        return VERDICT_MISMATCH, hint, missing

    # Default: ratio.
    thr = SOLVE_RATIO_THRESHOLD if threshold is None else threshold
    if actual_norm == expected_norm:
        return VERDICT_MATCH, "", []
    ratio = _ratio(actual_norm, expected_norm)
    if ratio >= thr:
        return VERDICT_MATCH, f"ratio {ratio:.2f} >= {thr}", []
    return VERDICT_MISMATCH, f"ratio {ratio:.2f} < {thr}", []