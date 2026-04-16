import copy
import threading
import time
import uuid
from time import perf_counter

from asgiref.sync import async_to_sync
from django.utils.html import strip_tags

from .model_health import get_runtime_model_handlers

_jobs_lock = threading.Lock()
_jobs = {}
_MAX_JOB_AGE_SECONDS = 6 * 60 * 60


def _prune_old_jobs(now_ts):
    stale_job_ids = []
    for run_id, job in _jobs.items():
        if job.get("status") not in {"completed", "failed"}:
            continue
        updated_at_ts = float(job.get("updated_at_ts") or now_ts)
        if now_ts - updated_at_ts > _MAX_JOB_AGE_SECONDS:
            stale_job_ids.append(run_id)

    for run_id in stale_job_ids:
        _jobs.pop(run_id, None)


def _to_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _extract_model_response(response):
    if isinstance(response, tuple):
        response_text = response[0] if len(response) > 0 else ""
        tokens = _to_int(response[1] if len(response) > 1 else 0)
        return str(response_text or ""), tokens

    return str(response or ""), 0


def _humanize_arm_model_error(raw_text):
    text = (raw_text or "").strip()
    if not text:
        return "", ""

    low = text.lower()

    # Long natural-language responses from models should not be interpreted as transport errors.
    if len(low) > 350 and not low.startswith(("ошибка", "error", "exception", "traceback")):
        return text, text

    def _pack(friendly_text):
        detailed_text = friendly_text
        if text and text != friendly_text:
            detailed_text += f"\n\nТехническая деталь: {text}"
        return friendly_text, detailed_text

    is_bad_request = (
        low == "неправильный запрос"
        or "код 400" in low
        or "error 400" in low
        or "status 400" in low
        or "bad request" in low
    )

    is_unauthorized = (
        "код 401" in low
        or "error 401" in low
        or "status 401" in low
        or "unauthorized" in low
        or "не авториз" in low
    )

    is_forbidden = (
        "код 403" in low
        or "error 403" in low
        or "status 403" in low
        or "forbidden" in low
        or "доступ запрещ" in low
    )

    is_not_found = (
        "код 404" in low
        or "error 404" in low
        or "status 404" in low
        or "not found" in low
        or "не найден" in low
    )

    is_rate_limited = (
        "код 429" in low
        or "error 429" in low
        or "status 429" in low
        or "rate limit" in low
        or "превышен лимит" in low
        or "все боты заняты" in low
    )

    is_timeout = (
        "таймаут" in low
        or "timeout" in low
        or "timed out" in low
        or "код 408" in low
        or "status 408" in low
    )

    is_server_error = (
        "код 500" in low
        or "код 502" in low
        or "код 503" in low
        or "код 504" in low
        or "error 500" in low
        or "error 502" in low
        or "error 503" in low
        or "error 504" in low
        or "status 500" in low
        or "status 502" in low
        or "status 503" in low
        or "status 504" in low
        or "bad gateway" in low
        or "gateway" in low
        or "server error" in low
        or "ошибка сервера" in low
        or "временно недоступ" in low
        or "инициализируется слишком долго" in low
    )

    is_connection_error = (
        "отсутствует подключение к интернету" in low
        or "отсутствует интернет" in low
        or "connectionerror" in low
        or "failed to resolve" in low
        or "name resolution" in low
        or "max retries exceeded" in low
        or "httpsconnectionpool" in low
        or "не удалось подключ" in low
    )

    if is_bad_request:
        return _pack(
            "Ошибка запроса к модели (400). Обычно это значит, что запрос слишком длинный, "
            "содержит неподдерживаемый формат или лишние спецсимволы. "
            "Попробуйте сократить условие/код и отправить снова."
        )

    if is_unauthorized:
        return _pack(
            "Ошибка авторизации модели (401). Проверьте API-ключ/токен и права доступа к модели."
        )

    if is_forbidden:
        return _pack(
            "Доступ к модели запрещен (403). У текущего ключа нет нужных прав или доступ ограничен политикой сервиса."
        )

    if is_not_found:
        return _pack(
            "Модель не найдена (404). Возможно, имя модели устарело или эта модель сейчас недоступна у провайдера."
        )

    if is_rate_limited:
        return _pack(
            "Сервис модели ограничил частоту запросов (429). Подождите немного и запустите проверку снова."
        )

    if is_timeout:
        return _pack(
            "Модель не ответила вовремя (таймаут). Попробуйте повторить запрос позже или сократить объем задачи/кода."
        )

    if is_server_error:
        return _pack(
            "Сервис модели временно недоступен (5xx). Это серверная ошибка провайдера, попробуйте позже."
        )

    if is_connection_error:
        return _pack(
            "Ошибка подключения к сервису модели. Проверьте сеть/прокси и доступность внешнего API."
        )

    if low.startswith("ошибка api") or "api (код" in low:
        return _pack(
            "Сервис модели вернул ошибку API. Проверьте параметры запроса и повторите попытку чуть позже."
        )

    return text, text


def _build_report(results):
    if not results:
        return None

    success_count = sum(1 for item in results if item.get("status") == "ok")
    error_count = len(results) - success_count
    tokens_total = sum(_to_int(item.get("tokens"), 0) for item in results)
    fastest = min(results, key=lambda item: float(item.get("duration") or 0.0))

    return {
        "models_total": len(results),
        "success_count": success_count,
        "error_count": error_count,
        "tokens_total": tokens_total,
        "fastest_model": fastest.get("model_title") or "-",
        "fastest_duration": float(fastest.get("duration") or 0.0),
    }


def _update_job(run_id, **updates):
    with _jobs_lock:
        job = _jobs.get(run_id)
        if not job:
            return

        job.update(updates)
        job["updated_at_ts"] = time.time()


def _run_job_worker(run_id, message, selected_model_keys, user_id):
    try:
        handlers = get_runtime_model_handlers()
        ordered_models = []
        for model_key in selected_model_keys:
            model_info = handlers.get(model_key)
            if model_info:
                ordered_models.append(
                    {
                        "key": model_key,
                        "title": model_info["title"],
                        "handler": model_info["handler"],
                    }
                )

        if not ordered_models:
            _update_job(
                run_id,
                status="failed",
                error_message="Выбранные модели недоступны. Обновите список и попробуйте снова.",
                current_model_key="",
                current_model_title="",
            )
            return

        total = len(ordered_models)
        _update_job(
            run_id,
            total_models=total,
            current_model_key=ordered_models[0]["key"],
            current_model_title=ordered_models[0]["title"],
        )

        for index, model in enumerate(ordered_models, start=1):
            started = perf_counter()

            try:
                response = async_to_sync(model["handler"])(
                    message,
                    f"admin-{user_id}-{model['key']}-{run_id}",
                )
                response_text, tokens = _extract_model_response(response)

                cleaned_text = strip_tags(response_text).strip()
                friendly_text, detailed_text = _humanize_arm_model_error(cleaned_text)
                short_response = friendly_text[:300] + ("..." if len(friendly_text) > 300 else "")
                is_ok = bool(friendly_text) and "ошибка" not in friendly_text.lower()[:25]
                result_item = {
                    "model_key": model["key"],
                    "model_title": model["title"],
                    "duration": round(perf_counter() - started, 2),
                    "tokens": tokens,
                    "short_response": short_response,
                    "status": "ok" if is_ok else "error",
                    "raw_response": detailed_text,
                }
            except Exception as exc:
                exc_text = str(exc)
                friendly_text, detailed_text = _humanize_arm_model_error(exc_text)
                result_item = {
                    "model_key": model["key"],
                    "model_title": model["title"],
                    "duration": round(perf_counter() - started, 2),
                    "tokens": 0,
                    "short_response": friendly_text or f"Ошибка вызова модели: {exc_text}",
                    "status": "error",
                    "raw_response": detailed_text,
                }

            with _jobs_lock:
                job = _jobs.get(run_id)
                if not job:
                    return

                job.setdefault("results", []).append(result_item)
                job["completed_models"] = index
                if index < total:
                    next_model = ordered_models[index]
                    job["current_model_key"] = next_model["key"]
                    job["current_model_title"] = next_model["title"]
                else:
                    job["current_model_key"] = ""
                    job["current_model_title"] = ""
                job["updated_at_ts"] = time.time()

        with _jobs_lock:
            job = _jobs.get(run_id)
            if not job:
                return

            job["report"] = _build_report(job.get("results") or [])
            job["status"] = "completed"
            job["updated_at_ts"] = time.time()

    except Exception as exc:
        _update_job(
            run_id,
            status="failed",
            error_message=f"ARM процесс завершился с ошибкой: {exc}",
            current_model_key="",
            current_model_title="",
        )


def start_arm_sequential_run(message, selected_model_keys, user_id):
    handlers = get_runtime_model_handlers()
    valid_model_keys = [key for key in selected_model_keys if key in handlers]
    if not valid_model_keys:
        return None, "Выберите хотя бы одну доступную модель"

    run_id = uuid.uuid4().hex
    now_ts = time.time()
    job = {
        "run_id": run_id,
        "status": "running",
        "error_message": "",
        "total_models": len(valid_model_keys),
        "completed_models": 0,
        "current_model_key": valid_model_keys[0],
        "current_model_title": handlers[valid_model_keys[0]]["title"],
        "results": [],
        "report": None,
        "created_at_ts": now_ts,
        "updated_at_ts": now_ts,
    }

    with _jobs_lock:
        _prune_old_jobs(now_ts)
        _jobs[run_id] = job

    worker = threading.Thread(
        target=_run_job_worker,
        args=(run_id, message, valid_model_keys, user_id),
        name=f"arm-sequential-run-{run_id[:8]}",
        daemon=True,
    )
    worker.start()

    return run_id, ""


def get_arm_run_snapshot(run_id):
    if not run_id:
        return None

    with _jobs_lock:
        job = _jobs.get(run_id)
        if not job:
            return None
        return copy.deepcopy(job)
