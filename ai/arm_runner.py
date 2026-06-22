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
from .models import AIModelTestResult, AIModelTestRun, AIRequestLog, ExternalDLAccount


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


def _to_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _extract_model_response(response):
    if isinstance(response, tuple):
        response_text = response[0] if len(response) > 0 else ""
        tokens = _to_int(response[1] if len(response) > 1 else 0)
        return str(response_text or ""), tokens

    return str(response or ""), 0


def _build_summary(results):
    """Per-model aggregation: % solved (desc), avg duration (asc).

    For a single-message run each model has one result, so percent_solved is
    binary (100/0). The structure scales to batch runs (one result per task per
    model): solved = ok-count, total = result-count for that model.
    """
    by_model = {}
    for item in results:
        key = item.get("model_key") or item.get("model_title") or "?"
        bucket = by_model.setdefault(
            key,
            {
                "model_key": item.get("model_key", ""),
                "model_title": item.get("model_title", ""),
                "solved": 0,
                "total": 0,
                "durations": [],
                "tokens": 0,
            },
        )
        bucket["total"] += 1
        if item.get("status") == "ok":
            bucket["solved"] += 1
        bucket["durations"].append(_to_float(item.get("duration")))
        bucket["tokens"] += _to_int(item.get("tokens"))

    summary = []
    for bucket in by_model.values():
        total = bucket["total"] or 1
        durations = bucket["durations"] or [0.0]
        summary.append(
            {
                "model_key": bucket["model_key"],
                "model_title": bucket["model_title"],
                "solved": bucket["solved"],
                "total": bucket["total"],
                "percent_solved": round(bucket["solved"] / total * 100, 1),
                "avg_duration": round(sum(durations) / len(durations), 2),
                "tokens": bucket["tokens"],
            }
        )

    summary.sort(key=lambda row: (-row["percent_solved"], row["avg_duration"]))
    return summary


def _build_report(results):
    if not results:
        return None

    success_count = sum(1 for item in results if item.get("status") == "ok")
    error_count = len(results) - success_count
    tokens_total = sum(_to_int(item.get("tokens"), 0) for item in results)
    fastest = min(results, key=lambda item: _to_float(item.get("duration")))

    return {
        "models_total": len(results),
        "success_count": success_count,
        "error_count": error_count,
        "tokens_total": tokens_total,
        "fastest_model": fastest.get("model_title") or "-",
        "fastest_duration": _to_float(fastest.get("duration")),
        "summary": _build_summary(results),
    }


def _update_job(run_id, **updates):
    with _jobs_lock:
        job = _jobs.get(run_id)
        if not job:
            return

        job.update(updates)
        job["updated_at_ts"] = time.time()


def _resolve_user(user_id):
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
    return user, username, external_id, full_name


def _run_job_worker(
    run_id,
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
    test_run = None
    log = None
    start_time = timezone.now()
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

        models_titles = [m["title"] for m in ordered_models]
        user, username, external_id, full_name = _resolve_user(user_id)

        test_run = AIModelTestRun.objects.create(
            run_id=run_id,
            user=user,
            status=AIModelTestRun.STATUS_RUNNING,
            started_at=start_time,
            message=message,
            total_models=len(ordered_models),
            programming_language_id=programming_language_id,
            programming_language_name=programming_language_name or "",
            topic_id=topic_id,
            topic_name=topic_name or "",
            prompt_id=prompt_id,
            prompt_name=prompt_name or "",
        )

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
            end_time = timezone.now()
            AIRequestLog.objects.filter(pk=log.pk).update(
                received_at=end_time,
                duration_seconds=0,
                status=AIRequestLog.STATUS_ERROR,
                error_message="Выбранные модели недоступны",
            )
            AIModelTestRun.objects.filter(pk=test_run.pk).update(
                status=AIModelTestRun.STATUS_FAILED,
                finished_at=end_time,
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
                status = "ok" if is_ok else "error"
                duration = round(perf_counter() - started, 2)
                result_item = {
                    "model_key": model["key"],
                    "model_title": model["title"],
                    "duration": duration,
                    "tokens": tokens,
                    "short_response": short_response,
                    "status": status,
                    "raw_response": detailed_text,
                }
            except Exception as exc:
                exc_text = str(exc)
                friendly_text, detailed_text = humanize_model_error(exc_text, include_detail=True)
                duration = round(perf_counter() - started, 2)
                status = "error"
                result_item = {
                    "model_key": model["key"],
                    "model_title": model["title"],
                    "duration": duration,
                    "tokens": 0,
                    "short_response": friendly_text or f"Ошибка вызова модели: {exc_text}",
                    "status": status,
                    "raw_response": detailed_text,
                }

            # Persist the per-model result row (source of truth for reports).
            AIModelTestResult.objects.update_or_create(
                run=test_run,
                model_key=model["key"],
                defaults={
                    "model_title": model["title"],
                    "status": status,
                    "duration_seconds": duration,
                    "tokens": result_item["tokens"],
                    "short_response": result_item["short_response"],
                    "raw_response": (result_item["raw_response"] or "")[:8000],
                },
            )

            with _jobs_lock:
                job = _jobs.get(run_id)
                evicted = job is None
                if not evicted:
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

            if evicted:
                # In-memory job evicted mid-run; mark the persisted run terminal
                # so the DB (source of truth) is not orphaned. DB writes are done
                # outside the lock to avoid holding _jobs_lock across DB round-trips
                # (it is shared with the polling get_arm_run_snapshot).
                end_time = timezone.now()
                AIModelTestRun.objects.filter(pk=test_run.pk).update(
                    status=AIModelTestRun.STATUS_FAILED,
                    finished_at=end_time,
                    error_message="ARM job evicted while running",
                )
                if log is not None:
                    AIRequestLog.objects.filter(pk=log.pk).update(
                        received_at=end_time,
                        status=AIRequestLog.STATUS_ERROR,
                        error_message="ARM job evicted while running",
                    )
                return

        with _jobs_lock:
            job = _jobs.get(run_id)
            evicted = job is None
            if not evicted:
                job["report"] = _build_report(job.get("results") or [])
                job["status"] = "completed"
                job["updated_at_ts"] = time.time()
                results = list(job.get("results") or [])
                report = job["report"]
        # NOTE: results/report captured under the lock; DB writes use them below.

        if evicted:
            # Job evicted after the last model but before the final update:
            # per-model rows are already in the DB. Rebuild the report from the
            # DB and persist COMPLETED so the run is not orphaned in RUNNING.
            # Done outside the lock to avoid holding _jobs_lock across DB reads
            # (test_run.results.all()) and writes.
            end_time = timezone.now()
            db_results = [
                {
                    "model_key": r.model_key,
                    "model_title": r.model_title,
                    "duration": r.duration_seconds or 0.0,
                    "tokens": r.tokens or 0,
                    "short_response": r.short_response,
                    "status": r.status,
                    "raw_response": r.raw_response,
                }
                for r in test_run.results.all()
            ]
            db_report = _build_report(db_results)
            if log is not None:
                AIRequestLog.objects.filter(pk=log.pk).update(
                    received_at=end_time,
                    duration_seconds=(end_time - start_time).total_seconds(),
                    status=AIRequestLog.STATUS_SUCCESS,
                )
            AIModelTestRun.objects.filter(pk=test_run.pk).update(
                status=AIModelTestRun.STATUS_COMPLETED,
                finished_at=end_time,
                report=db_report or {},
            )
            return

        end_time = timezone.now()
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
        AIModelTestRun.objects.filter(pk=test_run.pk).update(
            status=AIModelTestRun.STATUS_COMPLETED,
            finished_at=end_time,
            report=report or {},
        )

    except Exception as exc:
        _update_job(
            run_id,
            status="failed",
            error_message=f"ARM процесс завершился с ошибкой: {exc}",
            current_model_key="",
            current_model_title="",
        )
        end_time = timezone.now()
        if log is not None:
            AIRequestLog.objects.filter(pk=log.pk).update(
                received_at=end_time,
                duration_seconds=(end_time - start_time).total_seconds(),
                status=AIRequestLog.STATUS_ERROR,
                error_message=str(exc)[:2000],
            )
        if test_run is not None:
            AIModelTestRun.objects.filter(pk=test_run.pk).update(
                status=AIModelTestRun.STATUS_FAILED,
                finished_at=end_time,
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
        kwargs={
            "programming_language_id": programming_language_id,
            "programming_language_name": programming_language_name,
            "topic_id": topic_id,
            "topic_name": topic_name,
            "prompt_id": prompt_id,
            "prompt_name": prompt_name,
        },
        name=f"arm-sequential-run-{run_id[:8]}",
        daemon=True,
    )
    worker.start()

    return run_id, ""


def _snapshot_from_test_run(test_run):
    results_qs = test_run.results.all().order_by("model_title")
    results = [
        {
            "model_key": r.model_key,
            "model_title": r.model_title,
            "duration": r.duration_seconds or 0.0,
            "tokens": r.tokens or 0,
            "short_response": r.short_response,
            "status": r.status,
            "raw_response": r.raw_response,
        }
        for r in results_qs
    ]
    report = _build_report(results)
    status_map = {
        AIModelTestRun.STATUS_RUNNING: "running",
        AIModelTestRun.STATUS_COMPLETED: "completed",
        AIModelTestRun.STATUS_FAILED: "failed",
    }
    current_key = ""
    current_title = ""
    if results and test_run.status == AIModelTestRun.STATUS_RUNNING:
        # Best-effort "current model" hint from in-memory job if still present.
        with _jobs_lock:
            job = _jobs.get(test_run.run_id)
            if job:
                current_key = job.get("current_model_key", "")
                current_title = job.get("current_model_title", "")
    is_failed = test_run.status == AIModelTestRun.STATUS_FAILED
    return {
        "run_id": test_run.run_id,
        "status": status_map.get(test_run.status, test_run.status),
        "error_message": test_run.error_message or ("ARM процесс завершился с ошибкой" if is_failed else ""),
        "total_models": test_run.total_models or len(results),
        "completed_models": len(results),
        "current_model_key": current_key,
        "current_model_title": current_title,
        "results": results,
        "report": report,
        "created_at_ts": test_run.started_at.timestamp() if test_run.started_at else 0.0,
        "updated_at_ts": test_run.finished_at.timestamp() if test_run.finished_at else 0.0,
    }


def get_arm_run_snapshot(run_id):
    if not run_id:
        return None

    # Live in-memory job takes precedence while the run is in flight.
    with _jobs_lock:
        job = _jobs.get(run_id)
        if job:
            return copy.deepcopy(job)

    # Source of truth for completed/evicted runs: the database.
    try:
        test_run = AIModelTestRun.objects.get(run_id=run_id)
    except AIModelTestRun.DoesNotExist:
        return None
    return _snapshot_from_test_run(test_run)