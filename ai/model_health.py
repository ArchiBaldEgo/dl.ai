import re
import threading
import time
import logging
from datetime import time as dtime, timedelta
from time import perf_counter
from zoneinfo import ZoneInfo

from asgiref.sync import async_to_sync
from django.db import close_old_connections, transaction
from django.utils import timezone

from .models import AIModelAvailability, AIModelHealthRun

MOSCOW_TZ = ZoneInfo("Europe/Moscow")
HEALTHCHECK_PROMPT = "Ответь только одной цифрой без пояснений: 1+1=?"

logger = logging.getLogger(__name__)
_scheduler_lock = threading.Lock()
_scheduler_started = False
_manual_refresh_lock = threading.Lock()
_manual_refresh_started = False

MODEL_CATALOG = (
    {
        "key": "DeepSeek_R1_Distill_Llama_70B",
        "title": "DeepSeek-R1-Distill-Llama-70B",
        "handler_name": "ask_DeepSeek_R1_Distill_Llama_70B_async",
    },
    {
        "key": "DeepSeek_V3_1",
        "title": "DeepSeek-V3.1",
        "handler_name": "ask_DeepSeek_V3_1_async",
    },
    {
        "key": "DeepSeek_V3_1_cb",
        "title": "DeepSeek-V3.1-cb",
        "handler_name": "ask_DeepSeek_V3_1_cb_async",
    },
    {
        "key": "DeepSeek_V3_2",
        "title": "DeepSeek-V3.2",
        "handler_name": "ask_DeepSeek_V3_2_async",
    },
    {
        "key": "Llama_4_Maverick_17B_128E_Instruct",
        "title": "Llama-4-Maverick-17B-128E-Instruct",
        "handler_name": "ask_Llama_4_Maverick_17B_128E_Instruct_async",
    },
    {
        "key": "Meta_Llama_3_3_70B_Instruct",
        "title": "Meta-Llama-3.3-70B-Instruct",
        "handler_name": "ask_Meta_Llama_3_3_70B_Instruct_async",
    },
    {
        "key": "MiniMax_M2_5",
        "title": "MiniMax-M2.5",
        "handler_name": "ask_MiniMax_M2_5_async",
    },
    {
        "key": "Gemma_3_12b_it",
        "title": "gemma-3-12b-it",
        "handler_name": "ask_Gemma_3_12b_it_async",
    },
    {
        "key": "Gpt_oss_120b",
        "title": "gpt-oss-120b",
        "handler_name": "ask_Gpt_oss_120b_async",
    },
    {
        "key": "Web_DeepSeek",
        "title": "Web DeepSeek",
        "handler_name": "ask_Web_DeepSeek_async",
    },
    {
        "key": "Web_DeepSeek_Thinking",
        "title": "Web DeepSeek Thinking",
        "handler_name": "ask_Web_DeepSeek_Thinking_async",
    },
)

MODEL_ALIASES = {
    # Legacy values kept for backward compatibility with stale browser cache.
    "DeepSeek_R1": "DeepSeek_R1_Distill_Llama_70B",
    "DeepSeek-R1": "DeepSeek_R1_Distill_Llama_70B",
    "DeepSeek-R1-Distill-Llama-70B": "DeepSeek_R1_Distill_Llama_70B",
    "DeepSeek-V3.1": "DeepSeek_V3_1",
    "DeepSeek-V3.1-cb": "DeepSeek_V3_1_cb",
    "DeepSeek-V3.2": "DeepSeek_V3_2",
    "Llama_3_1_Tulu_3_405B": "Meta_Llama_3_3_70B_Instruct",
    "Meta_Llama_3_1_70B_Instruct": "Meta_Llama_3_3_70B_Instruct",
    "Meta-Llama-3.3-70B-Instruct": "Meta_Llama_3_3_70B_Instruct",
    "Llama-4-Maverick-17B-128E-Instruct": "Llama_4_Maverick_17B_128E_Instruct",
    "MiniMax-M2.5": "MiniMax_M2_5",
    "gemma-3-12b-it": "Gemma_3_12b_it",
    "gpt-oss-120b": "Gpt_oss_120b",
    "QwQ_32B": "DeepSeek_R1_Distill_Llama_70B",
    "Mixtral_8x7B": "Llama_4_Maverick_17B_128E_Instruct",
    "Mixtral_8x22b": "Llama_4_Maverick_17B_128E_Instruct",
}

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


def _load_runtime_handler(handler_name):
    from . import utils as ai_utils

    return getattr(ai_utils, handler_name, None)


def get_runtime_model_handlers():
    handlers = {}
    for spec in MODEL_CATALOG:
        handler = _load_runtime_handler(spec["handler_name"])
        if handler:
            handlers[spec["key"]] = {
                "title": spec["title"],
                "handler": handler,
            }
    return handlers


def _save_availability(window_date, key, title, is_available, response_time_ms, last_message):
    AIModelAvailability.objects.update_or_create(
        model_key=key,
        window_date=window_date,
        defaults={
            "model_title": title,
            "is_available": is_available,
            "response_time_ms": response_time_ms,
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

        for spec in MODEL_CATALOG:
            key = spec["key"]
            title = spec["title"]
            handler_info = handlers.get(key)

            if not handler_info:
                _save_availability(
                    window_date=window_date,
                    key=key,
                    title=title,
                    is_available=False,
                    response_time_ms=None,
                    last_message="Handler not found",
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

                _save_availability(
                    window_date=window_date,
                    key=key,
                    title=title,
                    is_available=is_available,
                    response_time_ms=elapsed_ms,
                    last_message=response_text,
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
    return [{"key": item["key"], "title": item["title"]} for item in MODEL_CATALOG]


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

        result.append(
            {
                "key": key,
                "title": item["title"],
                "is_active": is_active,
                "status_label": "Активна" if is_active else "Неактивна",
                "window_date": row.window_date if row else None,
                "checked_at": row.checked_at if row else None,
                "is_current_window": bool(row and row.window_date == current_window),
            }
        )

    return result


def get_available_model_options():
    ensure_model_health_for_current_window()

    ordered_keys = [item["key"] for item in MODEL_CATALOG]
    titles = {item["key"]: item["title"] for item in MODEL_CATALOG}

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
