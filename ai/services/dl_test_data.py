"""Входные и эталонные данные проваленных тестов DL — единый владелец.

DL REST API отдаёт их парой эндпоинтов: ``get-failed-tests`` (какие тесты
подтверждённо провалены и для каких разрешена выдача) + ``get-test-data``
(собственно ``input`` и ``expectedOutput``). Логика нужна в двух местах:

* ARM-прогон — данные сохраняются в ``AIModelTestResult.dl_test_data`` и
  показываются в карточке пары;
* ручная проверка со страницы «Реши задачу» — новый эндпоинт
  ``/ai/api/dl-test-data/`` отдаёт их фронтенду.

Поэтому fetch/pack/parse живут ЗДЕСЬ (CLAUDE.md, DRY), а не в
``arm_runner``: вьюха не должна тянуть ARM-зависимости.

Данные теста — необязательное дополнение к отчёту: ошибки DL не
поднимаются наружу, при отказе возвращается пустой список.

Переводы строк: DL отдаёт CRLF (``"1\\r\\n2\\r\\n"``). В браузере такой
``\\r`` либо съедается, либо ломает подсветку/копирование, поэтому
нормализуем к ``\\n`` на трёх границах — при получении из DL, при записи в
БД и при чтении старых строк.
"""

from __future__ import annotations

import json

# Предел длины входных данных/эталонного ответа, который сохраняется в БД
# и показывается в карточке пары: файлы тестов в DL бывают до 1 МиБ, а
# отчёт должен оставаться компактным.
TEST_DATA_CHARS_MAX = 8000
# Потолок числа тестов, данные которых сохраняем/показываем: DL отдаёт не
# более одного провала на этап проверки (этапов бывает несколько), но отчёт
# должен оставаться компактным и не ходить в DL десятки раз.
TEST_DATA_MAX_TESTS = 10


def normalize_test_data_text(value) -> str:
    """Привести переводы строк теста к ``\\n`` (CRLF и одиночный CR)."""
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n")


def pack_test_data(test_number, data) -> dict:
    """Компактная запись данных одного теста (обрезаем длинные файлы).

    Нормализация выполняется ДО обрезки: иначе срез мог бы оставить
    висящий ``\\r`` в конце (тесты и старые записи отдают сырой CRLF).
    """
    data = data or {}
    raw_input = normalize_test_data_text(data.get("input") or "")
    raw_expected = normalize_test_data_text(data.get("expected_output") or "")
    return {
        "test_number": test_number,
        "input": raw_input[:TEST_DATA_CHARS_MAX],
        "input_truncated": len(raw_input) > TEST_DATA_CHARS_MAX,
        "expected_output": raw_expected[:TEST_DATA_CHARS_MAX],
        "expected_truncated": len(raw_expected) > TEST_DATA_CHARS_MAX,
    }


def fetch_failed_test_data(session_id, queue_id, comment="") -> list[dict]:
    """Входные/эталонные данные ВСЕХ подтверждённых проваленных тестов.

    DL: ``get-failed-tests`` → список подтверждённых провалов (по одному на
    этап проверки; ``canGetData`` — можно ли забирать данные), затем
    ``get-test-data`` по каждому номеру → ``{input, expectedOutput}``.
    Возвращаем данные КАЖДОГО теста, у которого DL их выдал (до
    ``TEST_DATA_MAX_TESTS``) — не только первого: «не прошло» на разных
    этапах/тестах должно быть видно целиком.

    ФОЛБЭК: если DL не отдал ни одного теста с ``canGetData`` (пустой
    список, нет флага), номер берём из вердикта («не пройден 2-й тест»,
    «[testN]: …») и пробуем по нему — DL сам откажет, если выдача запрещена.
    Отказ по одному тесту (404/403 — например, последний тест курса) не
    мешает забрать данные остальных.

    Пустой список — данных нет (выдача запрещена настройками курса,
    проверка не завершена, ошибка DL). Ошибки не поднимаются: данные теста
    — дополнение к отчёту, а не условие прогона.
    """
    # Ленивый импорт: тесты патчат ``ai.dl_api_client.*`` — импорт в момент
    # вызова видит подменённые функции (как и раньше в arm_runner).
    from ..dl_api_client import (
        dl_failed_test_number_from_comment,
        get_failed_tests_from_dl,
        get_test_data_from_dl,
    )

    numbers = []
    for test in get_failed_tests_from_dl(session_id, queue_id):
        if not test.get("canGetData"):
            continue
        try:
            number = int(test.get("testNumber") or 0)
        except (TypeError, ValueError):
            number = 0
        if number > 0 and number not in numbers:
            numbers.append(number)
        if len(numbers) >= TEST_DATA_MAX_TESTS:
            break
    if not numbers:
        # Номер из вердикта DL — последний шанс узнать, какой тест упал.
        number = dl_failed_test_number_from_comment(comment)
        if number:
            numbers = [number]

    entries = []
    for number in numbers:
        data = get_test_data_from_dl(session_id, queue_id, number)
        if not data or not (data.get("input") or data.get("expected_output")):
            continue
        entries.append(pack_test_data(number, data))
    return entries


def _normalize_entry(entry: dict) -> dict:
    """Копия записи теста с нормализованными переводами строк."""
    normalized = dict(entry)
    if "input" in normalized:
        normalized["input"] = normalize_test_data_text(normalized.get("input"))
    if "expected_output" in normalized:
        normalized["expected_output"] = normalize_test_data_text(
            normalized.get("expected_output")
        )
    return normalized


def parse_dl_test_data(raw) -> list[dict]:
    """Разобрать сохранённые данные проваленных тестов (JSON → список dict).

    Новый формат — список записей по тестам (DL отдаёт по одному провалу на
    этап проверки). Старые строки БД хранят ОДИН dict — нормализуем его в
    одноэлементный список, чтобы UI и тесты не знали про легаси-формат.
    Переводы строк нормализуются при чтении: старые строки с CRLF не требуют
    миграции данных. Пустая/битая строка — пустой список.
    """
    if not raw:
        return []
    if isinstance(raw, (dict, list)):
        data = raw
    else:
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return []
    if isinstance(data, dict):
        return [_normalize_entry(data)]
    if isinstance(data, list):
        return [_normalize_entry(item) for item in data if isinstance(item, dict)]
    return []
