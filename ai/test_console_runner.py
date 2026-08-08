"""Раннер тестовой консоли админки: запускает пакет ``ai/tests.py`` в изолированном
subprocess и стримит построчные результаты во в-memory job.

Архитектура зеркалит ``ai/arm_runner.py`` / ``ai/prompt_test_runner.py``:
module-level ``_jobs`` dict + ``_jobs_lock`` + daemon-воркер + ``get_*_run_snapshot``.
Отличия:
- Тесты запускаются **сабпроцессом** ``manage.py test ai --settings=DjangoTest.test_settings
  --verbosity=2`` — изоляция от live-Daphne (``setup_test_environment`` патчит global
  settings; в потоке live-сервера это race). Сабпроцесс грузит свой ``manage.py`` со
  своими test-settings (sqlite/locmem) и не трогает прод-БД.
- Single-flight: Django создаёт/удаляет один общий ``test_test_db.sqlite3`` → два
  параллельных прогона столкнутся. Второй старт пока один бежит — отказ.
- In-memory only (без DB-модели); evicted job → status view вернёт 404.

Вывод unittest verbosity=2 парсится построчно: инлайн ``test_method (ai.tests.Class)
... ok|FAIL|ERROR|skipped`` → per-test result; секции ``===/FAIL:/---/traceback`` →
аттач трейсбека; ``Ran N`` + ``OK``/``FAILED(...)`` → summary. Непарсящиеся строки
падают в ``log[]`` (faithful raw-вид). Имена методов — английские (идентификаторы
сценариев), заголовки классов и статусы — русские.
"""

import copy
import re
import subprocess
import sys
import threading
import time
import uuid

_jobs_lock = threading.Lock()
_jobs = {}
_MAX_JOB_AGE_SECONDS = 6 * 60 * 60
_MAX_LOG_LINES = 500


# Русские названия тестовых классов ai/tests.py (45 шт.). Имена методов
# остаются английскими — это идентификаторы сценариев; перевод 174 имён
# ошибкоопасен и избыточен. Классов всего 45 — управляемо.
CLASS_TITLES_RU = {
    "ChatViewTests": "Тесты чат-представления",
    "ExternalAuthMiddlewareTests": "Внешняя auth-мидлвара",
    "AdminExternalAuthTests": "Доступ админки через внешнюю auth",
    "AdminPermissionsTests": "Права доступа в админке",
    "PromptAdminAccessTests": "Доступ к админке промптов",
    "PromptFormTests": "Форма промптов",
    "LocalizationHelpersTests": "Хелперы локализации",
    "PromptEffectiveTextTests": "Эффективный текст промпта",
    "ProblemDataApiUiLanguageTests": "API данных задач: язык интерфейса",
    "AIRequestLogModelTests": "Модель лога запросов",
    "ModelClientRegistryTests": "Реестр модельных клиентов",
    "DLApiClientEncodingTests": "Кодирование запросов к DL API",
    "ConversationHistoryTests": "История диалога",
    "MessageComposerTests": "Компоновщик сообщений",
    "ModelCallerTests": "Вызователь моделей",
    "RateLimiterTests": "Ограничитель частоты запросов",
    "RateLimitMiddlewarePollTests": "Ограничитель: poll-запросы",
    "UserIdentityForLogTests": "Идентификация пользователя для логов",
    "ModelCapabilitiesTests": "Возможности моделей",
    "ArmReportTests": "Отчёты ARM",
    "AutorecoveryTests": "Автовосстановление моделей",
    "ModelHealthGuardTests": "Гард состояния моделей",
    "HealthClassifierTests": "Классификатор состояния",
    "HealthCheckTransientTests": "Проверка: транзитные ошибки",
    "HealthCheckRetryTests": "Повтор проверки состояния",
    "ChatViewSelfHealTests": "Самовосстановление страницы чата",
    "TranslatePromptsCommandTests": "Команда translate_prompts",
    "BatchGradingTests": "Оценка пакетной обработки",
    "HealthCheckOutputTests": "Вывод проверки состояния",
    "BatchReportTests": "Отчёты пакетной обработки",
    "TaskModelTests": "Модель задач",
    "BatchRunnerIntegrationTests": "Интеграция пакетного раннера",
    "SambanovaLoggerTests": "Логгер SambaNova",
    "TaskRegistryTests": "Реестр задач",
    "PromptGradingTests": "Оценка промптов",
    "PromptRegressionRunnerTests": "Раннер регрессии промптов",
    "ModelSortingTests": "Сортировка моделей",
    "ProblemDataApiTests": "API данных задач",
    "UserTopModelKeysTests": "Топ-модели пользователя",
    "UpdateLogAdminTests": "Админка журнала обновлений",
    "UpdateLogModelTests": "Модель журнала обновлений",
    "LastUpdateDateCacheTests": "Кеш даты последнего обновления",
    "AvailableModelOptionsCacheTests": "Кеш доступных моделей",
    "OllamaRegistryTests": "Реестр Ollama",
    "OllamaHandlerTests": "Хендлер Ollama",
}

# status_raw (из unittest) → status_norm; STATUS_RU: status_norm → русский.
STATUS_RU = {
    "ok": "ОК",
    "fail": "Провал",
    "error": "Ошибка",
    "skipped": "Пропущен",
    "expected_failure": "Ожидаемый провал",
    "unexpected_success": "Неожиданный успех",
}


def _normalize_status(raw):
    return {
        "ok": "ok",
        "FAIL": "fail",
        "ERROR": "error",
        "skipped": "skipped",
        "expected failure": "expected_failure",
        "unexpected success": "unexpected_success",
    }.get(raw, raw)


# Парсер unittest verbosity=2. Статус-строки пишутся в stderr (Django runner);
# тесты, логирующие посреди прогона (logger.exception и т.д.), инжектируют свой
# вывод МЕЖДУ заголовком ``test_x (...) ... `` (startTest, без перевода строки) и
# статусом ``ok``/``FAIL`` (stopTest, writeln). Поэтому статус может быть:
#   - инлайн:   ``test_x (dotted) ... ok`` (всё на одной строке — нет инжекции);
#   - bare:     заголовок с инжектированным текстом, затем трейсбек-лог на отд.
#               строках, затем ``ok`` на отдельной строке.
# Заголовок без инлайн-статуса → тест «pending», ждёт bare-строку статуса;
# инжектированный вывод тем временем падает в log[] (faithful raw-вид).
# Заголовок: test_method (dotted)[ ... rest]. ``...`` опционален — тесты с docstring
# в Python 3.13 печатают ``test_method (dotted)`` без ``...`` на строке заголовка, а
# ``<docstring> ... <status>`` — на следующей строке. rest — статус, инжекция или пусто.
HEADER_RE = re.compile(
    r"^(?P<method>test_\w+)\s+\((?P<cls>[^)]+)\)\s*"
    r"(?:\.\.\.\s*(?P<rest>.*))?$"
)
# Закрывающая строка pending-теста: bare ``ok``/``FAIL`` (возможно с reason:
# ``ok 'msg'``) ИЛИ ``<prefix> ... <status>`` (docstring/body + разделитель ``... `` +
# статус). prefix неприменяется к результату — нужен лишь чтобы отличить закрытие
# от инжектированного body-вывода.
PENDING_CLOSE_RE = re.compile(
    r"^(?:(?P<prefix>.*?)\s*\.\.\.\s+)?"
    r"(?P<status>ok|FAIL|ERROR|skipped|expected failure|unexpected success)"
    r"(?:\s+'(?P<reason>.*)')?\s*$"
)
# Итог: Ran N tests in T.Ts
RAN_RE = re.compile(r"^Ran\s+(\d+)\s+tests?\s+in\s+([\d.]+)s\s*$")
# OK может нести счётчики: ``OK (skipped=2)`` / ``OK (expected failures=1)``.
FINAL_OK_RE = re.compile(r"^OK(?:\s*\((?P<rest>.*)\))?\s*$")
FINAL_FAIL_RE = re.compile(
    r"^FAILED\s*(?:\((?P<rest>.*)\))?\s*$"
)
# Секция трейсбека: ===.../FAIL: test (cls)/---.../<tb>
SECTION_RE = re.compile(
    r"^(?P<kind>FAIL|ERROR|SKIPPED|EXPECTED FAILURE|UNEXPECTED SUCCESS):\s+"
    r"(?P<method>test_\w+)\s+\((?P<cls>[^)]+)\)\s*$"
)
SEP_EQ_RE = re.compile(r"^={10,}\s*$")
SEP_DASH_RE = re.compile(r"^-{10,}\s*$")


def _parse_status_rest(rest):
    """Распарсить ``rest`` (текст после ``... `` на строке заголовка) в (status, reason)
    или None, если это не статус (инжектированный лог)."""
    rest = (rest or "").strip()
    if not rest:
        return None
    # reason — жадный до последней ``'`` (unittest эмбиттит ``skipped 'can't run'`` —
    # апостроф внутри reason; нежадный обрезал бы на первой ``'`` и ломал матч).
    m = re.match(
        r"^(?P<status>ok|FAIL|ERROR|skipped|expected failure|unexpected success)"
        r"(?:\s+'(?P<reason>.*)')?\s*$", rest,
    )
    if not m:
        return None
    return m.group("status"), (m.group("reason") or "")


def _parse_final_counts(rest):
    """Разбирает 'FAILED (failures=2, errors=1, skipped=1, expected failures=0,
    unexpected successes=1)' и ``OK (skipped=2, expected failures=1)``."""
    summary = {
        "failures": 0, "errors": 0, "skipped": 0,
        "expected_failures": 0, "unexpected_successes": 0,
    }
    if not rest:
        return summary
    for part in rest.split(","):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            k, v = k.strip(), v.strip()
            try:
                n = int(v)
            except ValueError:
                continue
            if k == "failures":
                summary["failures"] = n
            elif k == "errors":
                summary["errors"] = n
            elif k == "skipped":
                summary["skipped"] = n
            elif k == "expected failures":
                summary["expected_failures"] = n
            elif k == "unexpected successes":
                summary["unexpected_successes"] = n
    return summary


def _extract_class(dotted, method):
    """Класс из скобочного идентификатора теста.

    unittest (Python 3.11+) печатает ``test_method (ai.tests.Class.test_method)``
    — метод повторён внутри скобок. Поэтому класс — предпоследний сегмент, а не
    последний. Если формат старый ``test_method (ai.tests.Class)`` (последний
    сегмент ≠ method) — берём последний.
    """
    parts = dotted.split(".")
    if parts and parts[-1] == method:
        return parts[-2] if len(parts) >= 2 else dotted
    return parts[-1] if parts else dotted


def _parse_test_output(line_iter):
    """Генератор событий парсинга. Yield:
      ("result", {method, class, dotted_class, status, traceback, reason})
      ("traceback", {method, class, traceback})
      ("summary", {ran, seconds})
      ("final", {ok, failures, errors, skipped, expected_failures})
      ("log", line)

    Устойчив к тестам, логирующим посреди прогона: заголовок без инлайн-статуса
    → тест «pending» до bare-строки статуса; инжектированный вывод → log[].
    """
    section = None            # None | "header" | "body"
    current = None            # {method, class, dotted_class} — текущая секция трейсбека
    tb = []
    pending = None            # {method, dotted, class} — тест ждёт bare-статус

    for raw in line_iter:
        line = raw.rstrip("\n")

        # Pending тест: ждём закрывающую строку статуса (bare или docstring-prefixed).
        # Между заголовком и статусом мог быть инжектированный body-вывод → log[].
        if pending is not None:
            m = PENDING_CLOSE_RE.match(line)
            if m:
                yield ("result", {
                    "method": pending["method"],
                    "class": pending["class"],
                    "dotted_class": pending["dotted"],
                    "status": _normalize_status(m.group("status")),
                    "traceback": None,
                    "reason": m.group("reason") or "",
                })
                pending = None
                continue
            # Инжектированный контент pending-теста → raw-лог.
            yield ("log", line)
            continue

        # Секции трейсбеков (в конце прогона, когда ни один тест не pending).
        if section == "header":
            if SEP_DASH_RE.match(line):
                continue
            m = SECTION_RE.match(line)
            if m:
                dotted = m.group("cls")
                method = m.group("method")
                current = {
                    "method": method,
                    "class": _extract_class(dotted, method),
                    "dotted_class": dotted,
                }
                tb = []
                section = "body"
                continue
            section = None
            yield ("log", line)
            continue

        if section == "body":
            if SEP_EQ_RE.match(line):
                if current:
                    yield ("traceback", {**current, "traceback": "\n".join(tb).strip()})
                current = None
                tb = []
                section = "header"
                continue
            m = RAN_RE.match(line)
            if m:
                if current:
                    yield ("traceback", {**current, "traceback": "\n".join(tb).strip()})
                current = None
                yield ("summary", {"ran": int(m.group(1)), "seconds": float(m.group(2))})
                section = None
                continue
            m = FINAL_OK_RE.match(line)
            if m:
                if current:
                    yield ("traceback", {**current, "traceback": "\n".join(tb).strip()})
                current = None
                yield ("final", {"ok": True, **_parse_final_counts(m.group("rest"))})
                section = None
                continue
            m = FINAL_FAIL_RE.match(line)
            if m:
                if current:
                    yield ("traceback", {**current, "traceback": "\n".join(tb).strip()})
                current = None
                counts = _parse_final_counts(m.group("rest"))
                yield ("final", {"ok": False, **counts})
                section = None
                continue
            tb.append(line)
            continue

        # Нормальное состояние: нет pending, нет секции.
        if SEP_EQ_RE.match(line):
            section = "header"
            continue
        m = HEADER_RE.match(line)
        if m:
            method = m.group("method")
            dotted = m.group("cls")
            parsed = _parse_status_rest(m.group("rest"))
            if parsed is not None:
                status, reason = parsed
                yield ("result", {
                    "method": method,
                    "class": _extract_class(dotted, method),
                    "dotted_class": dotted,
                    "status": _normalize_status(status),
                    "traceback": None,
                    "reason": reason,
                })
            else:
                # Заголовок с инжектированным контентом (или пустой) → pending.
                # Саму строку-заголовок в лог не пишем —narратив этого сценария
                # появится, когда придёт закрывающая bare-строка статуса.
                pending = {
                    "method": method,
                    "dotted": dotted,
                    "class": _extract_class(dotted, method),
                }
            continue
        m = RAN_RE.match(line)
        if m:
            yield ("summary", {"ran": int(m.group(1)), "seconds": float(m.group(2))})
            continue
        m = FINAL_OK_RE.match(line)
        if m:
            yield ("final", {"ok": True, **_parse_final_counts(m.group("rest"))})
            continue
        m = FINAL_FAIL_RE.match(line)
        if m:
            counts = _parse_final_counts(m.group("rest"))
            yield ("final", {"ok": False, **counts})
            continue
        yield ("log", line)


def _prune_old_jobs(now_ts):
    """Evict завершённые job'ы старше _MAX_JOB_AGE_SECONDS. Под локом."""
    stale = [
        rid for rid, j in _jobs.items()
        if j.get("status") in ("completed", "failed")
        and (now_ts - j.get("updated_at_ts", 0)) > _MAX_JOB_AGE_SECONDS
    ]
    for rid in stale:
        _jobs.pop(rid, None)


def start_test_run():
    """Запускает прогон тестов. Returns (run_id, error_message).

    Single-flight: если прогон уже выполняется — отказ (общий test_test_db.sqlite3).
    """
    now_ts = time.time()
    with _jobs_lock:
        _prune_old_jobs(now_ts)
        for j in _jobs.values():
            if j.get("status") == "running":
                return None, "Тестовый прогон уже выполняется. Дождитесь его завершения."
        run_id = uuid.uuid4().hex
        _jobs[run_id] = {
            "run_id": run_id,
            "status": "running",
            "error_message": "",
            "total": None,
            "completed": 0,
            "current": "",
            "results": [],
            "summary": None,
            "log": [],
            "created_at_ts": now_ts,
            "updated_at_ts": now_ts,
        }
    worker = threading.Thread(
        target=_run_worker, args=(run_id,),
        name=f"test-console-{run_id[:8]}", daemon=True,
    )
    worker.start()
    return run_id, ""


def _append_log(run_id, text, kind="raw"):
    """Дополнить Журнал вывода одной строкой.

    Каждая запись — ``{"text", "kind"}``. ``kind`` управляет подсветкой на
    фронте: понятный человеческий текст (``stage``/``ok``/``fail``/``skip``/
    ``warn``/``human``/``final-ok``/``final-fail``) рисуется крупно и с цветом,
    технический шум (``raw`` — трейсбеки, инжектированный вывод логгеров) —
    приглушённым моноширинным шрифтом. Так человек видит нарратив, а не сырое
    ``verbosity=2``.
    """
    with _jobs_lock:
        job = _jobs.get(run_id)
        if job is None:
            return
        job["log"].append({"text": str(text), "kind": kind})
        if len(job["log"]) > _MAX_LOG_LINES:
            del job["log"][: len(job["log"]) - _MAX_LOG_LINES]
        job["updated_at_ts"] = time.time()


# Текстовый маркер и вид подсветки по нормализованному статусу теста.
_RESULT_MARK = {
    "ok": ("✓", "ok"),
    "fail": ("✗", "fail"),
    "error": ("✗", "fail"),
    "skipped": ("○", "skip"),
    "expected_failure": ("◐", "skip"),
    "unexpected_success": ("✗", "fail"),
}


def _format_test_line(class_ru, method, status, reason=""):
    """Понятная строка-нарратив одного сценария: маркер + русская группа + метод."""
    mark, kind = _RESULT_MARK.get(status, ("•", "raw"))
    base = f"{mark} {class_ru} · {method}"
    if status == "ok":
        return base, "ok"
    if status in ("skipped", "expected_failure"):
        suffix = STATUS_RU.get(status, status)
        if reason:
            suffix += f": {reason}"
        return f"{base} — {suffix}", kind
    return f"{base} — {STATUS_RU.get(status, status)}", kind


def _run_worker(run_id):
    from django.conf import settings

    cwd = str(settings.BASE_DIR)
    cmd = [
        sys.executable, "manage.py", "test", "ai",
        "--settings=DjangoTest.test_settings", "--verbosity=2",
    ]
    try:
        proc = subprocess.Popen(
            cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
    except Exception as exc:
        with _jobs_lock:
            job = _jobs.get(run_id)
            if job is not None:
                job["status"] = "failed"
                job["error_message"] = f"Не удалось запустить subprocess: {exc}"
                job["updated_at_ts"] = time.time()
        return

    _append_log(run_id, "▶ Запускаем проверки…", kind="stage")


    for kind, payload in _parse_test_output(proc.stdout):
        if kind == "result":
            class_ru = CLASS_TITLES_RU.get(payload["class"], payload["class"])
            line_text, line_kind = _format_test_line(
                class_ru, payload["method"], payload["status"], payload.get("reason", ""),
            )
            _append_log(run_id, line_text, kind=line_kind)
            with _jobs_lock:
                job = _jobs.get(run_id)
                if job is not None:
                    # Компактная запись для «простого» вида (группировка по классу,
                    # без имён методов и трейсбеков) — подробный нарратив живёт в log[].
                    job["results"].append({
                        "class": payload["class"],
                        "class_ru": class_ru,
                        "status": payload["status"],
                        "reason": payload.get("reason", ""),
                    })
                    job["completed"] = (job.get("completed") or 0) + 1
                    job["current"] = f"{class_ru} · {payload['method']}"
                    job["updated_at_ts"] = time.time()
        elif kind == "traceback":
            class_ru = CLASS_TITLES_RU.get(payload["class"], payload["class"])
            # Понятный заголовок-нарратив, под ним — приглушённый трейсбек.
            _append_log(
                run_id,
                f"Подробности ошибки — {class_ru} · {payload['method']}:",
                kind="fail",
            )
            _append_log(run_id, payload["traceback"], kind="raw")
            with _jobs_lock:
                job = _jobs.get(run_id)
                if job is not None:
                    job["updated_at_ts"] = time.time()
        elif kind == "summary":
            with _jobs_lock:
                job = _jobs.get(run_id)
                if job is not None:
                    job["total"] = payload["ran"]
                    job["_seconds"] = payload["seconds"]
                    job["updated_at_ts"] = time.time()
            _append_log(
                run_id,
                f"Пройдено {payload['ran']} сценариев за {payload['seconds']} с.",
                kind="human",
            )
        elif kind == "final":
            with _jobs_lock:
                job = _jobs.get(run_id)
                if job is not None:
                    summary = {
                        "ran": job.get("total") or 0,
                        "seconds": job.pop("_seconds", 0.0),
                        "ok": payload["ok"],
                        "failures": payload["failures"],
                        "errors": payload["errors"],
                        "skipped": payload["skipped"],
                        "expected_failures": payload["expected_failures"],
                        "unexpected_successes": payload.get("unexpected_successes", 0),
                    }
                    job["summary"] = summary
                    job["updated_at_ts"] = time.time()
            if payload["ok"]:
                _append_log(run_id, "✅ ИТОГ: все сценарии прошли успешно.", kind="final-ok")
            else:
                _append_log(
                    run_id,
                    "❌ ИТОГ: есть провалы или ошибки — подробности выше, в журнале.",
                    kind="final-fail",
                )
        elif kind == "log":
            _append_log(run_id, payload, kind="raw")

    proc.wait()

    with _jobs_lock:
        job = _jobs.get(run_id)
        if job is not None:
            if job.get("summary") is None:
                job["status"] = "failed"
                job["error_message"] = (
                    "Тестовый прогон завершился без итоговой строки (возможно, "
                    "упал импорт/сборка). Смотрите журнал вывода."
                )
                _append_log(run_id, "⚠ Не удалось получить итог — проверьте журнал ниже.", kind="warn")
            else:
                job["status"] = "completed"
            job["updated_at_ts"] = time.time()


def get_test_run_snapshot(run_id):
    """Глубокая копия in-memory job или None (evicted → status view 404)."""
    if not run_id:
        return None
    with _jobs_lock:
        job = _jobs.get(run_id)
        if job:
            return copy.deepcopy(job)
    return None