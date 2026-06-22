import re
import threading
import time
import logging
from datetime import time as dtime, timedelta
from time import perf_counter
from asgiref.sync import async_to_sync
from django.db import close_old_connections, transaction
from django.utils import timezone

from django.conf import settings

from .constants import MOSCOW_TZ
from .models import AIModelAvailability, AIModelHealthRun
HEALTHCHECK_PROMPT = "Ответь только одной цифрой без пояснений: 1+1=?"

logger = logging.getLogger(__name__)
_scheduler_lock = threading.Lock()
_scheduler_started = False
_manual_refresh_lock = threading.Lock()
_manual_refresh_started = False

from .model_clients import registry
from .model_clients.web_deepseek import restart_bot_pool

# Web DeepSeek models served by the bot/ pool — candidates for auto-recovery.
WEB_DEEPSEEK_KEYS = ("Web_DeepSeek", "Web_DeepSeek_Thinking")
_AUTORECOVERY_BACKOFF_SECONDS = 8

# Health checks only exercise the models defined in the registry below.
MODEL_CATALOG_KEYS = [
    "Web_DeepSeek",
    "Web_DeepSeek_Thinking",
    "DeepSeek_R1_Distill_Llama_70B",
    "DeepSeek_V3_1",
    "DeepSeek_V3_1_cb",
    "DeepSeek_V3_2",
    "Llama_4_Maverick_17B_128E_Instruct",
    "Meta_Llama_3_3_70B_Instruct",
    "MiniMax_M2_5",
    "MiniMax_M2_7",
    "Gemma_3_12b_it",
    "Gpt_oss_120b",
]

# Concrete error signatures the model clients actually return on a failed call.
# We deliberately do NOT use loose stems like "ошибка"/"недоступ"/"подключени"/
# "таймаут" here: a healthy reasoning-model reply to the one-digit healthcheck
# prompt can still contain those words ("ошибки нет", "если таймаут…"), which
# caused false negatives. These specific phrases never appear in a genuine
# "2"-only reply, so a present correct answer wins unless one of them shows up.
_DEFINITE_ERROR_MARKERS = (
    "ошибка api",
    "таймаут при подключении",
    "бот не авторизован",
    "неправильный запрос",
    "все боты заняты",
    "бот инициализируется слишком долго",
    "закончились кредиты",
    "требуется оплата",
    "код 402",
    "превышен лимит",
    "rate limit",
    "unauthorized",
    "не авторизован",
    "not found",
    "пустой ответ",
    "timeout",
)

# Transient failures worth one retry before marking a model down — a slow
# cold-start of a now-working provider should not flip is_available to False.
_TRANSIENT_MARKERS = (
    "таймаут",
    "timeout",
    "инициализируется",
    "пустой ответ",
    "подключении",
    "temporarily",
    "временно",
)

_TWO_RE = re.compile(r"(^|\D)2(\D|$)")
_WORD_ANSWER_RE = re.compile(r"^(два|two)\b", re.IGNORECASE)
_TRANSIENT_RETRY_DELAY = 5.0
_HTTP_CODE_RE = re.compile(r"\b(\d{3})\b")

_HTTP_CODE_LABELS = {
    200: "OK",
    400: "Неправильный запрос",
    401: "Не авторизован",
    402: "Требуется оплата (закончились кредиты)",
    403: "Доступ запрещён",
    404: "Не найдено",
    408: "Таймаут запроса",
    429: "Слишком много запросов",
    500: "Внутренняя ошибка сервера",
    502: "Ошибка шлюза",
    503: "Сервис недоступен",
    504: "Таймаут шлюза",
}


def get_health_window_date(now=None):
    current = now or timezone.now()
    moscow_now = current.astimezone(MOSCOW_TZ)
    window_date = moscow_now.date()
    if moscow_now.time() < dtime(4, 0):
        window_date -= timedelta(days=1)
    return window_date


def _extract_response_text(result):
    if isinstance(result, tuple):
        if len(result) > 0 and result[0] is not None:
            return str(result[0]).strip()
        return ""
    if result is None:
        return ""
    return str(result).strip()


def _is_healthy_response(response_text):
    """A reply is healthy iff it carries the correct answer, UNLESS it is a
    definite client/API error string.

    The correct answer (digit ``2`` or word form ``два``/``two``) is checked
    AFTER the definite-error markers so that a healthy reply which happens to
    contain an error word (reasoning models narrating "ошибки нет, ответ 2")
    is not marked down. Definite API errors ("Ошибка API (код 402)", "rate
    limit") are still detected first, so a rate-limit message containing "2
    минуты" stays unhealthy.
    """
    if not response_text:
        return False

    low = response_text.lower().strip()
    if any(marker in low for marker in _DEFINITE_ERROR_MARKERS):
        return False

    if _TWO_RE.search(response_text):
        return True

    if _WORD_ANSWER_RE.match(low):
        return True

    return False


def _extract_http_code_from_message(message):
    """Try to recover an HTTP status code from a model error message."""
    if not message:
        return None
    match = _HTTP_CODE_RE.search(message)
    if match:
        code = int(match.group(1))
        if 100 <= code < 600:
            return code
    return None


def get_http_code_label(code):
    """Return a short Russian description for an HTTP status code."""
    if code is None:
        return "Нет ответа / сетевая ошибка"
    if code in _HTTP_CODE_LABELS:
        return _HTTP_CODE_LABELS[code]
    if 200 <= code < 300:
        return "Успешный ответ"
    if 300 <= code < 400:
        return "Перенаправление"
    if 400 <= code < 500:
        return "Ошибка в запросе"
    if 500 <= code < 600:
        return "Ошибка сервера"
    return f"Неизвестный код ({code})"


def get_runtime_model_handlers():
    handlers = {}
    for key in MODEL_CATALOG_KEYS:
        handler = registry.handler(key)
        if handler:
            handlers[key] = {
                "title": registry.title(key),
                "handler": handler,
            }
    return handlers


def _save_availability(window_date, key, title, is_available, response_time_ms, last_message, last_http_code=None):
    AIModelAvailability.objects.update_or_create(
        model_key=key,
        window_date=window_date,
        defaults={
            "model_title": title,
            "is_available": is_available,
            "response_time_ms": response_time_ms,
            "last_http_code": last_http_code,
            "last_message": (last_message or "")[:2000],
        },
    )


def _looks_transient(response_text, exc=None):
    """Heuristic: did the call fail in a way worth one retry before giving up?

    Cold-start timeouts, "bot initializing too long", empty replies and
    connection exceptions all qualify. Definite API errors (401/402/rate
    limit) do NOT — retrying those just burns time and tokens.
    """
    if exc is not None:
        return True
    if not response_text:
        return True
    low = response_text.lower()
    return any(marker in low for marker in _TRANSIENT_MARKERS)


def _invoke_healthcheck(handler, window_date, key):
    """Call a model handler with the healthcheck prompt.

    Returns ``(response_text, elapsed_ms, exc)`` where exactly one of
    ``response_text``/``exc`` is populated.
    """
    started = perf_counter()
    try:
        result = async_to_sync(handler)(
            HEALTHCHECK_PROMPT,
            f"health-{window_date.isoformat()}-{key}",
        )
        elapsed_ms = int((perf_counter() - started) * 1000)
        return _extract_response_text(result), elapsed_ms, None
    except Exception as exc:
        elapsed_ms = int((perf_counter() - started) * 1000)
        return "", elapsed_ms, exc


def _check_one_model(key, title, handler_info, window_date):
    """Run the healthcheck prompt against one model and persist availability.

    Returns a detail dict ``{key, title, is_available, last_http_code,
    last_message, response_time_ms}`` so callers (the management command, the
    auto-recovery path) can surface per-model HTTP code + response text without
    re-reading the DB.

    A first attempt that fails in a transient way (cold-start timeout, empty
    reply, connection error) gets ONE retry after a short backoff, so a
    now-working provider is not marked down because its first call was slow.
    Definite API errors (401/402/rate limit) are not retried.
    """
    if not handler_info:
        _save_availability(
            window_date=window_date,
            key=key,
            title=title,
            is_available=False,
            response_time_ms=None,
            last_message="Handler not found",
            last_http_code=None,
        )
        return {
            "key": key,
            "title": title,
            "is_available": False,
            "last_http_code": None,
            "last_message": "Handler not found",
            "response_time_ms": None,
        }

    handler = handler_info["handler"]
    response_text, elapsed_ms, exc = _invoke_healthcheck(handler, window_date, key)
    is_available = exc is None and _is_healthy_response(response_text)

    if not is_available and _looks_transient(response_text, exc):
        time.sleep(_TRANSIENT_RETRY_DELAY)
        response_text, elapsed_ms, exc = _invoke_healthcheck(handler, window_date, key)
        is_available = exc is None and _is_healthy_response(response_text)

    if exc is not None:
        message = f"Health check exception: {exc}"
        _save_availability(
            window_date=window_date,
            key=key,
            title=title,
            is_available=False,
            response_time_ms=elapsed_ms,
            last_message=message,
            last_http_code=None,
        )
        return {
            "key": key,
            "title": title,
            "is_available": False,
            "last_http_code": None,
            "last_message": message,
            "response_time_ms": elapsed_ms,
        }

    # Model handlers only return text/tokens, not the raw status code.
    # We try to recover the HTTP code from error messages when possible.
    # For healthy responses we assume the HTTP status was 200.
    last_http_code = 200 if is_available else _extract_http_code_from_message(response_text)

    _save_availability(
        window_date=window_date,
        key=key,
        title=title,
        is_available=is_available,
        response_time_ms=elapsed_ms,
        last_message=response_text,
        last_http_code=last_http_code,
    )
    return {
        "key": key,
        "title": title,
        "is_available": is_available,
        "last_http_code": last_http_code,
        "last_message": response_text,
        "response_time_ms": elapsed_ms,
    }


def _maybe_autorecover_web_deepseek(handlers, window_date):
    """Restart the bot pool and re-check Web DeepSeek if it is down.

    Gated by ``AI_WEB_DEEPSEEK_AUTORECOVERY`` (default True). Only acts when at
    least one Web DeepSeek model is unavailable; restarts the pool once, waits
    briefly, re-checks the down models, and annotates ``last_message`` with the
    auto-recovery outcome. Never raises.
    """
    if not getattr(settings, "AI_WEB_DEEPSEEK_AUTORECOVERY", True):
        return

    down_keys = []
    for key in WEB_DEEPSEEK_KEYS:
        row = AIModelAvailability.objects.filter(
            window_date=window_date, model_key=key
        ).first()
        if not row or not row.is_available:
            down_keys.append(key)

    if not down_keys:
        return

    logger.info("Web DeepSeek down (%s); attempting bot-pool auto-recovery", down_keys)
    restarted = restart_bot_pool()
    if not restarted:
        # Annotate the down models, but never let a transient DB error here
        # escape — the docstring says "Never raises", and the per-model rows
        # were already persisted during the sweep; a DB hiccup now must not
        # flip the whole health run to FAILED. Mirrors the re-check branch.
        try:
            for key in down_keys:
                _save_availability(
                    window_date=window_date,
                    key=key,
                    title=registry.title(key),
                    is_available=False,
                    response_time_ms=None,
                    last_message="Автоподъём не удался: бот-пул недоступен",
                    last_http_code=None,
                )
        except Exception:
            logger.warning("Failed to annotate bot-pool restart failure", exc_info=True)
        return

    # Give the freshly spawned bot a moment to come up before re-checking.
    time.sleep(_AUTORECOVERY_BACKOFF_SECONDS)

    for key in down_keys:
        title = registry.title(key)
        handler_info = handlers.get(key)
        detail = _check_one_model(key, title, handler_info, window_date)
        is_up = detail["is_available"]
        # Annotate the result so operators can see auto-recovery happened.
        # The availability row was already persisted by _check_one_model; this
        # is a cosmetic annotation only, so a transient DB error here must never
        # escape and flip the whole health run to FAILED (per docstring).
        try:
            row = AIModelAvailability.objects.filter(
                window_date=window_date, model_key=key
            ).first()
            if row:
                suffix = " [автоподъём: ок]" if is_up else " [автоподъём: модель всё ещё недоступна]"
                row.last_message = (row.last_message or "")[:1900] + suffix
                row.save(update_fields=["last_message"])
        except Exception:
            logger.warning("Failed to annotate auto-recovery outcome for %s", key, exc_info=True)


def run_model_health_check(force=False, on_model_checked=None):
    window_date = get_health_window_date()
    now = timezone.now()

    with transaction.atomic():
        run = (
            AIModelHealthRun.objects.select_for_update()
            .filter(window_date=window_date)
            .first()
        )

        if run is None:
            # Cold start: no row for this window yet. get_or_create on the
            # unique window_date guarantees exactly ONE process wins the create
            # race (a loser's INSERT raises IntegrityError and get_or_create
            # re-gets the winner's row). Only the creator may proceed to the
            # sweep; everyone else observes the winner's RUNNING status below
            # and bails. Without this, N Daphne workers booting at once would
            # all sweep + auto-recover the bot pool concurrently.
            run, created = AIModelHealthRun.objects.get_or_create(
                window_date=window_date,
                defaults={
                    "status": AIModelHealthRun.STATUS_RUNNING,
                    "started_at": now,
                    "finished_at": None,
                    "error_message": "",
                },
            )
        else:
            created = False

        if not created:
            if run.status == AIModelHealthRun.STATUS_COMPLETED and not force:
                return False
            # An actively-running run (started <45min ago) must never be
            # double-run, NOT EVEN with force=True. force is for re-running a
            # COMPLETED or a stale (>45min) run; letting it bypass this guard
            # would let two concurrent --force / admin-refresh invocations
            # (which only do a racy read-only pre-check before calling us) both
            # sweep the 12 models and both call restart_bot_pool concurrently.
            # The row lock we hold here is the real serialization point, so this
            # guard is what actually prevents the cross-process race.
            if (
                run.status == AIModelHealthRun.STATUS_RUNNING
                and run.started_at
                and run.started_at > now - timedelta(minutes=45)
            ):
                return False

        run.status = AIModelHealthRun.STATUS_RUNNING
        run.started_at = now
        run.finished_at = None
        run.error_message = ""
        run.save()

    try:
        handlers = get_runtime_model_handlers()

        for key in MODEL_CATALOG_KEYS:
            title = registry.title(key)
            handler_info = handlers.get(key)
            detail = _check_one_model(key, title, handler_info, window_date)
            if on_model_checked is not None:
                try:
                    on_model_checked(detail)
                except Exception:
                    logger.warning("on_model_checked callback failed for %s", key, exc_info=True)

        _maybe_autorecover_web_deepseek(handlers, window_date)

        AIModelHealthRun.objects.filter(pk=run.pk).update(
            status=AIModelHealthRun.STATUS_COMPLETED,
            finished_at=timezone.now(),
            error_message="",
        )
        return True

    except Exception as exc:
        AIModelHealthRun.objects.filter(pk=run.pk).update(
            status=AIModelHealthRun.STATUS_FAILED,
            finished_at=timezone.now(),
            error_message=str(exc)[:1000],
        )
        return False


def _has_recent_running_run(window_date):
    return AIModelHealthRun.objects.filter(
        window_date=window_date,
        status=AIModelHealthRun.STATUS_RUNNING,
        started_at__gt=timezone.now() - timedelta(minutes=45),
    ).exists()


def is_model_health_refresh_running():
    window_date = get_health_window_date()

    if _manual_refresh_started:
        return True

    return _has_recent_running_run(window_date)


def _manual_refresh_worker():
    global _manual_refresh_started

    close_old_connections()
    try:
        run_model_health_check(force=True)
    except Exception:
        logger.exception("Manual model health refresh failed")
    finally:
        close_old_connections()
        with _manual_refresh_lock:
            _manual_refresh_started = False


def trigger_model_health_refresh_async():
    global _manual_refresh_started

    window_date = get_health_window_date()
    with _manual_refresh_lock:
        if _manual_refresh_started or _has_recent_running_run(window_date):
            return False

        thread = threading.Thread(
            target=_manual_refresh_worker,
            name="ai-model-health-manual-refresh",
            daemon=True,
        )
        thread.start()
        _manual_refresh_started = True
        return True


def _seconds_until_next_4am_moscow(now=None):
    current = (now or timezone.now()).astimezone(MOSCOW_TZ)
    next_run = current.replace(hour=4, minute=0, second=0, microsecond=0)
    if current >= next_run:
        next_run += timedelta(days=1)
    return max(int((next_run - current).total_seconds()), 1)


def _scheduler_loop():
    while True:
        # Daemon threads never fire request_started/request_finished, so Django's
        # automatic connection recycling does not run here. CONN_MAX_AGE is unset
        # (default 0), so without this the thread would hold one DB connection
        # across the up-to-24h sleep; if Postgres closed the idle server-side
        # connection, the next 04:00 MSK iteration's first query would raise
        # OperationalError. Start each iteration with a fresh connection,
        # mirroring _manual_refresh_worker, and close it before sleeping.
        close_old_connections()
        try:
            run_model_health_check(force=False)
            wait_seconds = _seconds_until_next_4am_moscow()
        except Exception:
            logger.exception("Model health scheduler iteration failed")
            # DB/network can be temporarily unavailable during startup.
            # Retry sooner instead of waiting for the next daily window.
            wait_seconds = 300
        close_old_connections()
        time.sleep(wait_seconds)


def start_model_health_scheduler():
    global _scheduler_started

    with _scheduler_lock:
        if _scheduler_started:
            return

        thread = threading.Thread(
            target=_scheduler_loop,
            name="ai-model-health-scheduler",
            daemon=True,
        )
        thread.start()
        _scheduler_started = True


def get_all_model_options():
    return [
        {"key": key, "title": registry.title(key), "capabilities": registry.capabilities(key)}
        for key in MODEL_CATALOG_KEYS
    ]


def get_model_status_rows():
    ordered = get_all_model_options()
    ordered_keys = [item["key"] for item in ordered]
    current_window = get_health_window_date()

    current_rows = {
        row.model_key: row
        for row in AIModelAvailability.objects.filter(
            window_date=current_window,
            model_key__in=ordered_keys,
        )
    }

    missing_keys = [key for key in ordered_keys if key not in current_rows]
    latest_rows = {}
    if missing_keys:
        for row in AIModelAvailability.objects.filter(model_key__in=missing_keys).order_by(
            "model_key", "-window_date"
        ):
            if row.model_key not in latest_rows:
                latest_rows[row.model_key] = row

    result = []
    for item in ordered:
        key = item["key"]
        row = current_rows.get(key) or latest_rows.get(key)
        is_active = bool(row and row.is_available)
        last_http_code = row.last_http_code if row else None

        result.append(
            {
                "key": key,
                "title": item["title"],
                "capabilities": item.get("capabilities") or registry.capabilities(key),
                "is_active": is_active,
                "status_label": "Активна" if is_active else "Неактивна",
                "window_date": row.window_date if row else None,
                "checked_at": row.checked_at if row else None,
                "is_current_window": bool(row and row.window_date == current_window),
                "last_http_code": last_http_code,
                "last_http_code_label": get_http_code_label(last_http_code),
            }
        )

    return result


def get_available_model_options():
    """Return the list of available models for the current health window.

    This function is intentionally read-only: it does not trigger a synchronous
    health check. The daily scheduler and manual refresh are responsible for
    populating ``AIModelAvailability`` rows. If the current window has no data,
    we fall back to the most recent completed window so users still see a list
    while the scheduler catches up.
    """
    ordered_keys = MODEL_CATALOG_KEYS
    titles = {key: registry.title(key) for key in MODEL_CATALOG_KEYS}

    window_date = get_health_window_date()
    available_rows = {
        row.model_key: row
        for row in AIModelAvailability.objects.filter(
            window_date=window_date,
            is_available=True,
            model_key__in=ordered_keys,
        )
    }

    if not available_rows:
        fallback_date = (
            AIModelAvailability.objects.filter(
                is_available=True,
                model_key__in=ordered_keys,
            )
            .order_by("-window_date")
            .values_list("window_date", flat=True)
            .first()
        )
        if fallback_date:
            available_rows = {
                row.model_key: row
                for row in AIModelAvailability.objects.filter(
                    window_date=fallback_date,
                    is_available=True,
                    model_key__in=ordered_keys,
                )
            }

    return [
        {"key": key, "title": titles[key], "capabilities": registry.capabilities(key)}
        for key in ordered_keys
        if key in available_rows
    ]
