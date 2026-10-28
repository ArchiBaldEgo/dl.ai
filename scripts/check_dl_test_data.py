#!/usr/bin/env python3
"""Проверить, отдаёт ли dl.gsu.by входные/эталонные данные проваленного теста.

Отвечает на вопрос «почему в карточке пары нет блока „Входные данные /
Выходные данные (эталон)“»: DL решает это сам, флагом ``canGetData`` в
``/restapi/get-failed-tests``. Если флаг ``false`` (или ``/get-test-data``
отвечает 403) — данные не выдаются настройками курса для этой учётки, и
наша сторона тут ничего не сделает.

Зачем отдельный скрипт: то же самое делает приложение
(``ai/services/dl_test_data.py::fetch_failed_test_data``), но для проверки
не нужно поднимать Django/контейнер и не нужны зависимости — только
стандартная библиотека Python.

Примеры:

    # проваленный тест по queueId из карточки пары
    python3 scripts/check_dl_test_data.py --session '{DLSID}' --queue 15246074

    # проверить конкретный номер теста, не спрашивая get-failed-tests
    python3 scripts/check_dl_test_data.py --session '{DLSID}' --queue 15246074 --test 2

    # другой хост и без проверки сертификата (локальная отладка)
    python3 scripts/check_dl_test_data.py --session '{DLSID}' --queue 15246074 \
        --base http://127.0.0.1:8080 --insecure

Код выхода: 0 — хотя бы по одному тесту данные получены, 1 — DL не отдал
ничего (или запрос не удался), 2 — ошибка аргументов.

DLSID: кука ``DLSID`` с dl.gsu.by (F12 → Application → Cookies). Сессия
должна быть та же, под которой решение отправлялось в DL: данные отдаются
по правам владельца решения.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request
from urllib.parse import urlparse

DEFAULT_BASE = "https://dl.gsu.by"
TIMEOUT = 30


def resolve_base(explicit: str | None) -> str:
    """Базовый адрес DL — как в приложении (ai/dl_api_client.py).

    Приложение выводит его из ``EXTERNAL_AUTH_API_URL`` (локальная отладка
    может смотреть на другой хост), поэтому и здесь сначала env, потом
    аргумент, потом https://dl.gsu.by.
    """
    if explicit:
        return explicit.rstrip("/")
    auth_url = (os.getenv("EXTERNAL_AUTH_API_URL") or "").strip()
    if auth_url:
        parsed = urlparse(auth_url)
        if parsed.netloc:
            return f"{parsed.scheme or 'https'}://{parsed.netloc}"
    return DEFAULT_BASE


def post_json(base: str, path: str, payload: dict, *, insecure: bool) -> tuple[int, dict]:
    """POST JSON → (HTTP-статус, тело). Тело всегда dict (даже у ошибки)."""
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        base + path,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    context = ssl._create_unverified_context() if insecure else None
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT, context=context) as response:
            status = response.status
            raw = response.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw = exc.read()
    except urllib.error.URLError as exc:
        raise SystemExit(f"DL недоступен ({base}): {exc.reason}")
    try:
        body = json.loads(raw.decode("utf-8", errors="replace") or "{}")
    except json.JSONDecodeError:
        # Не JSON — значит HTML-страница ошибки IIS/DL. Рвём её только при
        # печати (short_body): здесь обрезка ломает парные теги.
        body = {"raw": raw.decode("utf-8", errors="replace")}
    return status, body if isinstance(body, dict) else {"data": body}


def short_body(body: dict) -> str:
    """Компактное описание ошибки для человека.

    IIS/DL на отказ отдаёт HTML-страницу на 2 КБ — в консоли она бесполезна,
    поэтому теги снимаем и оставляем одну строку с текстом.
    """
    text = json.dumps(body, ensure_ascii=False)
    if "raw" in body and isinstance(body["raw"], str):
        text = body["raw"] or text
    # <style>/<script> — не текст страницы: их содержимое (CSS-комментарии)
    # осталось бы после снятия тегов.
    text = re.sub(r"<(style|script)\b.*?</\1>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = " ".join(text.split())
    return text[:300] or "(пустой ответ)"


def print_test_data(body: dict) -> bool:
    """Напечатать input/expectedOutput. True — данные непустые."""
    text_in = str(body.get("input") or "")
    text_out = str(body.get("expectedOutput") or "")
    if not (text_in or text_out):
        return False
    # DL отдаёт CRLF — показываем как есть, но одной строкой в превью.
    preview_in = text_in.replace("\r\n", "\n")[:400]
    preview_out = text_out.replace("\r\n", "\n")[:400]
    print(f"    input: {len(text_in)} симв.")
    if preview_in:
        print("      " + preview_in.replace("\n", "\n      "))
    print(f"    expectedOutput: {len(text_out)} симв.")
    if preview_out:
        print("      " + preview_out.replace("\n", "\n      "))
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Проверить выдачу входных/эталонных данных проваленного теста DL.",
    )
    parser.add_argument("--session", required=True, help="кука DLSID")
    parser.add_argument("--queue", required=True, type=int, help="queueId решения")
    parser.add_argument(
        "--test", type=int, default=0,
        help="номер теста: проверить только его, минуя get-failed-tests",
    )
    parser.add_argument("--base", default="", help=f"адрес DL (по умолчанию {DEFAULT_BASE})")
    parser.add_argument(
        "--insecure", action="store_true", help="не проверять TLS-сертификат (локальная отладка)",
    )
    parser.add_argument(
        "--json", action="store_true", help="печатать тела ответов целиком",
    )
    args = parser.parse_args()

    base = resolve_base(args.base.strip() or None)
    session_id = args.session.strip()
    print(f"DL: {base} · queueId: {args.queue}")

    numbers: list[int] = []
    if args.test:
        numbers = [args.test]
        print(f"Тесты: {numbers} (задан --test, get-failed-tests не спрашиваем)")
    else:
        status, body = post_json(
            base, "/restapi/get-failed-tests",
            {"sessionId": session_id, "queueId": args.queue},
            insecure=args.insecure,
        )
        if status != 200:
            print(f"get-failed-tests → HTTP {status}: {short_body(body)}")
            return 1
        if args.json:
            print(json.dumps(body, ensure_ascii=False, indent=2))
        tests = body.get("tests") if isinstance(body.get("tests"), list) else []
        print(f"get-failed-tests → HTTP 200 · isFinished={body.get('isFinished')} · тестов: {len(tests)}")
        for test in tests:
            if not isinstance(test, dict):
                continue
            number = int(test.get("testNumber") or 0)
            print(
                f"  тест {number or '—'} · canGetData={test.get('canGetData')}"
                f" · {(test.get('message') or '').strip()[:160]}"
            )
            if test.get("canGetData") and number:
                numbers.append(number)
        if not numbers:
            print(
                "Итог: DL не разрешил выдачу данных (canGetData=false). "
                "Это решение dl.gsu.by по настройкам курса/правам учётки, не наш код."
            )
            return 1

    got_any = False
    for number in numbers:
        status, body = post_json(
            base, "/restapi/get-test-data",
            {"sessionId": session_id, "queueId": args.queue, "testNumber": number},
            insecure=args.insecure,
        )
        print(f"get-test-data (тест {number}) → HTTP {status}")
        if args.json:
            print(json.dumps(body, ensure_ascii=False, indent=2))
        if status != 200:
            print(f"    отказ: {short_body(body)}")
            continue
        got_any = print_test_data(body) or got_any

    if got_any:
        print("Итог: данные получены — карточка пары покажет блок «Входные/Выходные данные».")
        return 0
    print("Итог: пустой ответ — данных нет (DL не отдал input/expectedOutput).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
