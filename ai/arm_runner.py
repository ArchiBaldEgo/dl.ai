import copy
import json
import threading
import time
import uuid
from time import perf_counter

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.utils import timezone
from django.utils.html import strip_tags

from .model_clients.exceptions import humanize_model_error
from .model_health import get_runtime_model_handlers
from .models import AIRequestLog, ExternalDLAccount


User = get_user_model()

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

        start_time = timezone.now()
        models_titles = [m["title"] for m in ordered_models]
        user = None
        username = ""
        external_id = ""
        full_name = ""
        try:
            user = User.objects.get(pk=user_id)
            username = user.username
            full_name = (user.get_full_name() or "").strip() or username
            try:
                external_id = user.external_dl_account.external_user_id
            except (ExternalDLAccount.DoesNotExist, AttributeError):
                external_id = username
        except User.DoesNotExist:
            pass

        log = AIRequestLog.objects.create(
            user=user,
            username=username,
            external_user_id=external_id,
            user_full_name=full_name,
            source=AIRequestLog.SOURCE_ARM,
            mode=AIRequestLog.MODE_ARM,
            sent_at=start_time,
            model_names=models_titles,
            message=message,
            programming_language_id=programming_language_id,
            programming_language_name=programming_language_name or "",
            topic_id=topic_id,
            topic_name=topic_name or "",
            prompt_id=prompt_id,
            prompt_name=prompt_name or "",
        )

        if not ordered_models:
            _update_job(
                run_id,
                status="failed",
                error_message="Выбранные модели недоступны. Обновите список и попробуйте снова.",
                current_model_key="",
                current_model_title="",
            )
            AIRequestLog.objects.filter(pk=log.pk).update(
                received_at=timezone.now(),
                duration_seconds=0,
                status=AIRequestLog.STATUS_ERROR,
                error_message="Выбранные модели недоступны",
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
                friendly_text, detailed_text = humanize_model_error(cleaned_text, include_detail=True)
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
                friendly_text, detailed_text = humanize_model_error(exc_text, include_detail=True)
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

        end_time = timezone.now()
        results = job.get("results") or []
        response_summary = json.dumps(
            [
                {
                    "model": r.get("model_title"),
                    "status": r.get("status"),
                    "duration": r.get("duration"),
                    "tokens": r.get("tokens"),
                }
                for r in results
            ],
            ensure_ascii=False,
        )
        AIRequestLog.objects.filter(pk=log.pk).update(
            received_at=end_time,
            duration_seconds=(end_time - start_time).total_seconds(),
            response_text=response_summary[:5000],
            status=AIRequestLog.STATUS_SUCCESS,
        )

    except Exception as exc:
        _update_job(
            run_id,
            status="failed",
            error_message=f"ARM процесс завершился с ошибкой: {exc}",
            current_model_key="",
            current_model_title="",
        )
        if "log" in locals():
            end_time = timezone.now()
            AIRequestLog.objects.filter(pk=log.pk).update(
                received_at=end_time,
                duration_seconds=(end_time - start_time).total_seconds() if "start_time" in locals() else None,
                status=AIRequestLog.STATUS_ERROR,
                error_message=str(exc)[:2000],
            )


def start_arm_sequential_run(
    message,
    selected_model_keys,
    user_id,
    *,
    programming_language_id=None,
    programming_language_name="",
    topic_id=None,
    topic_name="",
    prompt_id=None,
    prompt_name="",
):
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
