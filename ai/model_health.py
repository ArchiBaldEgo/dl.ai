import re
import threading
import time
import logging
from datetime import time as dtime, timedelta
from time import perf_counter
from asgiref.sync import async_to_sync
from django.db import close_old_connections, transaction
from django.utils import timezone

from .constants import MOSCOW_TZ
from .models import AIModelAvailability, AIModelHealthRun
HEALTHCHECK_PROMPT = "Ответь только одной цифрой без пояснений: 1+1=?"

logger = logging.getLogger(__name__)
_scheduler_lock = threading.Lock()
_scheduler_started = False
_manual_refresh_lock = threading.Lock()
_manual_refresh_started = False

from .model_clients import registry

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

_ERROR_MARKERS = (
    "ошибка",
    "не авторизован",
    "таймаут",
    "недоступ",
    "превышен лимит",
    "подключени",
    "неправильный запрос",
    "пустой ответ",
    "not found",
    "unauthorized",
    "timeout",
    "rate limit",
)

_TWO_RE = re.compile(r"(^|\D)2(\D|$)")
_HTTP_CODE_RE = re.compile(r"\b(\d{3})\b")

_HTTP_CODE_LABELS = {
    200: "OK",
    400: "Неправильный запрос",
    401: "Не авторизован",
    402: "Требуется оплата",
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
    if not response_text:
        return False

    low = response_text.lower().strip()
    if any(marker in low for marker in _ERROR_MARKERS):
        return False

    if _TWO_RE.search(response_text):
        return True

    return low in {"два", "two"}


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


def run_model_health_check(force=False):
    window_date = get_health_window_date()
    now = timezone.now()

    with transaction.atomic():
        run = (
            AIModelHealthRun.objects.select_for_update()
            .filter(window_date=window_date)
            .first()
        )

        if run and run.status == AIModelHealthRun.STATUS_COMPLETED and not force:
            return False

        if (
            run
            and run.status == AIModelHealthRun.STATUS_RUNNING
            and run.started_at
            and run.started_at > now - timedelta(minutes=45)
            and not force
        ):
            return False

        if run is None:
            run = AIModelHealthRun(window_date=window_date)

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
                continue

            started = perf_counter()
            try:
                result = async_to_sync(handler_info["handler"])(
                    HEALTHCHECK_PROMPT,
                    f"health-{window_date.isoformat()}-{key}",
                )
                elapsed_ms = int((perf_counter() - started) * 1000)
                response_text = _extract_response_text(result)
                is_available = _is_healthy_response(response_text)
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
            except Exception as exc:
                elapsed_ms = int((perf_counter() - started) * 1000)
                _save_availability(
                    window_date=window_date,
                    key=key,
                    title=title,
                    is_available=False,
                    response_time_ms=elapsed_ms,
                    last_message=f"Health check exception: {exc}",
                    last_http_code=None,
                )

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


def ensure_model_health_for_current_window():
    window_date = get_health_window_date()
    is_ready = AIModelHealthRun.objects.filter(
        window_date=window_date,
        status=AIModelHealthRun.STATUS_COMPLETED,
    ).exists()

    if not is_ready:
        run_model_health_check(force=False)


def _seconds_until_next_4am_moscow(now=None):
    current = (now or timezone.now()).astimezone(MOSCOW_TZ)
    next_run = current.replace(hour=4, minute=0, second=0, microsecond=0)
    if current >= next_run:
        next_run += timedelta(days=1)
    return max(int((next_run - current).total_seconds()), 1)


def _scheduler_loop():
    while True:
        try:
            run_model_health_check(force=False)
            wait_seconds = _seconds_until_next_4am_moscow()
        except Exception:
            logger.exception("Model health scheduler iteration failed")
            # DB/network can be temporarily unavailable during startup.
            # Retry sooner instead of waiting for the next daily window.
            wait_seconds = 300

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
    return [{"key": key, "title": registry.title(key)} for key in MODEL_CATALOG_KEYS]


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
        {"key": key, "title": titles[key]}
        for key in ordered_keys
        if key in available_rows
    ]
