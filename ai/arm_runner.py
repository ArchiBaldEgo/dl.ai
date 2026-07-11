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
from .models import AIModelTestResult, AIModelTestRun, AIRequestLog, ExternalDLAccount, Task


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


# ---------------------------------------------------------------------------
# Batch-solve ARM: grading + solve-prompt + DL sample extraction.
#
# Grading is intentionally approximate: the model's solution is compared to the
# DL sample solution by normalized text similarity (difflib ratio), NOT by
# running the code. A semantically-correct but stylistically-different solution
# may be marked FAILED. This is reflected in the UI.
# ---------------------------------------------------------------------------

# Grading helpers (normalization + difflib ratio) live in the shared grading
# module and are re-exported here so legacy ``from ai.arm_runner import ...`` stays
# valid (ai/tests.py imports grade_solution / normalize_solution).
from .grading import (
    SOLVE_RATIO_THRESHOLD,
    grade_solution,
    normalize_solution,
)

_VERDICT_SOLVED = "solved"
_VERDICT_FAILED = "failed"
_VERDICT_SKIPPED = "skipped"


def _build_solve_message(task_statement, prog_lang_name, topic_name, ui_language="Русский"):
    """Compose the solve prompt for one task (mirrors _build_find_error_message).

    Uses the SharedPrompt with mode="solve" if one exists, falling back to a
    plain Russian instruction. Placeholders {language}/{язык}/{topic}/{тема} are
    substituted via SharedPrompt.get_effective_text.
    """
    from .i18n import get_language_instruction
    from .models import SharedPrompt

    try:
        default_prompt = SharedPrompt.objects.get(mode="solve")
        message = default_prompt.get_effective_text(
            ui_language, prog_lang_name, topic_name, task_statement, ""
        )
    except SharedPrompt.DoesNotExist:
        message = (
            f"Реши задачу по программированию на языке {prog_lang_name}. "
            f"Выведи только готовое решение (код), без пояснений. "
            f"Условие задачи: {task_statement}."
        )
    message += get_language_instruction(ui_language)
    return message


def _extract_sample_solution(data):
    """Best-effort extraction of the sample solution text from a DL get-solution
    response. The response shape is not documented in dl_api_client.py, so we
    try the common keys and fall back to stringifying."""
    if not data:
        return ""
    if isinstance(data, str):
        return data
    if isinstance(data, list):
        # A list of file objects: pick the first non-empty content.
        for entry in data:
            if isinstance(entry, dict):
                text = entry.get("content") or entry.get("solution") or entry.get("text") or ""
                if text:
                    return str(text)
            elif isinstance(entry, str) and entry:
                return entry
        return ""
    if isinstance(data, dict):
        for key in ("content", "solution", "text", "code", "fileContent", "data"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value
            if isinstance(value, list):
                inner = _extract_sample_solution(value)
                if inner:
                    return inner
    return ""


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


def _per_bucket(results, key_fn, label_fn):
    """Aggregate results into buckets (per-model or per-topic) for batch runs.

    Skipped verdicts are excluded from solved/total (no oracle), but their
    tokens still count. avg_duration is over the non-skipped results. Buckets
    are sorted by % solved desc, avg duration asc (mirrors _build_summary).
    """
    buckets = {}
    for item in results:
        verdict = item.get("verdict")
        key = key_fn(item)
        if key is None:
            continue
        bucket = buckets.setdefault(
            key,
            {"label": label_fn(item), "solved": 0, "total": 0, "durations": [], "tokens": 0},
        )
        bucket["tokens"] += _to_int(item.get("tokens"))
        if verdict == _VERDICT_SKIPPED:
            continue
        bucket["total"] += 1
        bucket["durations"].append(_to_float(item.get("duration")))
        if verdict == _VERDICT_SOLVED:
            bucket["solved"] += 1

    rows = []
    for bucket in buckets.values():
        total = bucket["total"] or 1
        durations = bucket["durations"] or [0.0]
        rows.append({
            "label": bucket["label"],
            "solved": bucket["solved"],
            "total": bucket["total"],
            "percent_solved": round(bucket["solved"] / total * 100, 1) if bucket["total"] else 0.0,
            "avg_duration": round(sum(durations) / len(durations), 2),
            "tokens": bucket["tokens"],
        })
    rows.sort(key=lambda row: (-row["percent_solved"], row["avg_duration"]))
    return rows


def _build_batch_report(results):
    """Batch-solve report: per-model + per-topic tables + overall counters."""
    if not results:
        return None
    solved = sum(1 for r in results if r.get("verdict") == _VERDICT_SOLVED)
    failed = sum(1 for r in results if r.get("verdict") == _VERDICT_FAILED)
    skipped = sum(1 for r in results if r.get("verdict") == _VERDICT_SKIPPED)
    tokens_total = sum(_to_int(r.get("tokens")) for r in results)

    per_model = _per_bucket(
        results,
        key_fn=lambda r: r.get("model_key") or r.get("model_title"),
        label_fn=lambda r: r.get("model_title") or r.get("model_key") or "?",
    )
    per_topic = _per_bucket(
        results,
        key_fn=lambda r: r.get("topic_name") or "Без темы",
        label_fn=lambda r: r.get("topic_name") or "Без темы",
    )
    return {
        "total_pairs": len(results),
        "solved": solved,
        "failed": failed,
        "skipped": skipped,
        "tokens_total": tokens_total,
        "per_model": per_model,
        "per_topic": per_topic,
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


def _run_batch_job_worker(
    run_id,
    tasks_qs,
    ordered_models,
    user_id,
    session_id,
    *,
    ui_language="Русский",
):
    """Daemon worker for a batch-solve run.

    Iterates tasks in the outer loop and models in the inner loop. The DL sample
    solution is fetched once per task (cached) so the number of DL calls is
    len(tasks), not len(tasks)*len(models).
    """
    from .dl_api_client import DLApiError, fetch_task_solution

    test_run = None
    log = None
    start_time = timezone.now()
    try:
        user, username, external_id, full_name = _resolve_user(user_id)
        models_titles = [m["title"] for m in ordered_models]

        test_run = AIModelTestRun.objects.create(
            run_id=run_id,
            run_type=AIModelTestRun.RUN_TYPE_BATCH,
            user=user,
            status=AIModelTestRun.STATUS_RUNNING,
            started_at=start_time,
            message=f"Batch solve: {len(tasks_qs)} задач × {len(ordered_models)} моделей",
            total_models=len(ordered_models),
        )
        log = AIRequestLog.objects.create(
            user=user,
            username=username,
            external_user_id=external_id,
            user_full_name=full_name,
            source=AIRequestLog.SOURCE_ARM,
            mode=AIRequestLog.MODE_SOLVE,
            sent_at=start_time,
            model_names=models_titles,
            message=f"Batch solve run {run_id}",
        )

        tasks = list(tasks_qs)
        total_pairs = len(tasks) * len(ordered_models)
        _update_job(
            run_id,
            total_pairs=total_pairs,
            completed_pairs=0,
            current_task_node_id=tasks[0].node_id if tasks else "",
            current_task_name=tasks[0].name if tasks else "",
            current_model_key=ordered_models[0]["key"] if ordered_models else "",
            current_model_title=ordered_models[0]["title"] if ordered_models else "",
        )

        if not tasks or not ordered_models:
            _update_job(run_id, status="failed", error_message="Нет задач или моделей для запуска.")
            end_time = timezone.now()
            AIModelTestRun.objects.filter(pk=test_run.pk).update(
                status=AIModelTestRun.STATUS_FAILED, finished_at=end_time,
                error_message="Нет задач или моделей для запуска",
            )
            AIRequestLog.objects.filter(pk=log.pk).update(
                received_at=end_time, status=AIRequestLog.STATUS_ERROR,
                error_message="Нет задач или моделей для запуска",
            )
            return

        sample_cache = {}
        completed = 0

        for task in tasks:
            # Fetch the DL sample solution once per task.
            sample_text = sample_cache.get(task.node_id)
            if sample_text is None and task.task_id and task.file_extension:
                try:
                    sample_data = fetch_task_solution(
                        session_id, task.task_id, task.file_extension
                    )
                    sample_text = _extract_sample_solution(sample_data)
                except DLApiError:
                    sample_text = ""
                except Exception:
                    sample_text = ""
                sample_cache[task.node_id] = sample_text

            topic_name = task.topic.topic_name if task.topic else ""
            prog_lang_name = (
                task.programming_language.language_name if task.programming_language else ""
            )

            for model in ordered_models:
                started = perf_counter()
                verdict = _VERDICT_SKIPPED
                status = "error"
                short_response = ""
                raw_response = ""
                tokens = 0

                try:
                    if not sample_text:
                        # No oracle for this task -> skip all models.
                        verdict = _VERDICT_SKIPPED
                        status = "error"
                        short_response = "Нет образцового решения (get-solution)"
                        raw_response = short_response
                    else:
                        message = _build_solve_message(
                            task.statement, prog_lang_name, topic_name, ui_language
                        )
                        response = async_to_sync(model["handler"])(
                            message,
                            f"admin-batch-{user_id}-{model['key']}-{run_id}-{task.node_id}",
                        )
                        response_text, tokens = _extract_model_response(response)
                        cleaned_text = strip_tags(response_text).strip()
                        friendly, detailed = humanize_model_error(cleaned_text, include_detail=True)
                        verdict = grade_solution(cleaned_text, sample_text)
                        status = "ok" if verdict != _VERDICT_SKIPPED else "error"
                        short_response = (friendly or cleaned_text)[:300] + (
                            "..." if len(friendly or cleaned_text) > 300 else ""
                        )
                        raw_response = detailed or cleaned_text
                except Exception as exc:
                    exc_text = str(exc)
                    friendly, detailed = humanize_model_error(exc_text, include_detail=True)
                    verdict = _VERDICT_SKIPPED
                    status = "error"
                    short_response = friendly or f"Ошибка вызова модели: {exc_text}"
                    raw_response = detailed

                duration = round(perf_counter() - started, 2)

                AIModelTestResult.objects.update_or_create(
                    run=test_run,
                    model_key=model["key"],
                    task=task,
                    defaults={
                        "model_title": model["title"],
                        "status": status,
                        "verdict": verdict,
                        "duration_seconds": duration,
                        "tokens": tokens,
                        "short_response": short_response,
                        "raw_response": (raw_response or "")[:8000],
                        "topic_id_snapshot": task.topic_id,
                        "topic_name_snapshot": topic_name,
                        "prog_lang_snapshot": prog_lang_name,
                    },
                )

                result_item = {
                    "task_id": task.id,
                    "task_node_id": task.node_id,
                    "task_name": task.name,
                    "model_key": model["key"],
                    "model_title": model["title"],
                    "duration": duration,
                    "tokens": tokens,
                    "short_response": short_response,
                    "status": status,
                    "verdict": verdict,
                    "raw_response": raw_response,
                    "topic_name": topic_name,
                    "prog_lang_name": prog_lang_name,
                }

                completed += 1
                with _jobs_lock:
                    job = _jobs.get(run_id)
                    if job is None:
                        # Evicted mid-run: persist terminal state from DB later.
                        pass
                    else:
                        job.setdefault("results", []).append(result_item)
                        job["completed_pairs"] = completed
                        job["updated_at_ts"] = time.time()

        # Finalize: build report from in-memory results (or DB if evicted).
        with _jobs_lock:
            job = _jobs.get(run_id)
            evicted = job is None
            if not evicted:
                job["report"] = _build_batch_report(job.get("results") or [])
                job["status"] = "completed"
                job["updated_at_ts"] = time.time()
                results = list(job.get("results") or [])
                report = job["report"]

        if evicted:
            db_results = _batch_results_from_db(test_run)
            report = _build_batch_report(db_results)

        end_time = timezone.now()
        AIRequestLog.objects.filter(pk=log.pk).update(
            received_at=end_time,
            duration_seconds=(end_time - start_time).total_seconds(),
            status=AIRequestLog.STATUS_SUCCESS,
        )
        AIModelTestRun.objects.filter(pk=test_run.pk).update(
            status=AIModelTestRun.STATUS_COMPLETED,
            finished_at=end_time,
            report=report or {},
        )
        _update_job(run_id, status="completed")

    except Exception as exc:
        _update_job(
            run_id, status="failed",
            error_message=f"Batch solve завершился с ошибкой: {exc}",
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


def _batch_results_from_db(test_run):
    """Rebuild batch result items from persisted AIModelTestResult rows."""
    results = []
    for r in test_run.results.select_related("task").order_by("model_title"):
        node_id = r.task.node_id if r.task else ""
        task_name = r.task.name if r.task else ""
        results.append({
            "task_node_id": node_id,
            "task_name": task_name,
            "model_key": r.model_key,
            "model_title": r.model_title,
            "duration": r.duration_seconds or 0.0,
            "tokens": r.tokens or 0,
            "short_response": r.short_response,
            "status": r.status,
            "verdict": r.verdict or _VERDICT_SKIPPED,
            "raw_response": r.raw_response,
            "topic_name": r.topic_name_snapshot,
            "prog_lang_name": r.prog_lang_snapshot,
        })
    return results


def start_batch_solve_run(task_ids, model_keys, user_id, session_id, *, ui_language="Русский"):
    """Start a batch-solve ARM run over the given tasks × models.

    Returns (run_id, error_message). ``task_ids`` / ``model_keys`` empty means
    "all active tasks" / "all currently available models".
    """
    handlers = get_runtime_model_handlers()
    if model_keys:
        valid_model_keys = [k for k in model_keys if k in handlers]
    else:
        valid_model_keys = list(handlers.keys())
    if not valid_model_keys:
        return None, "Нет доступных моделей. Обновите состояние моделей и попробуйте снова."

    tasks_qs = Task.objects.filter(active=True).select_related("topic", "programming_language")
    if task_ids:
        tasks_qs = tasks_qs.filter(pk__in=task_ids)
    if not tasks_qs.exists():
        return None, "Нет активных задач для запуска."
    if not session_id:
        return None, "Нет DLSID — требуется авторизация на dl.gsu.by для получения образцовых решений."

    ordered_models = [
        {"key": k, "title": handlers[k]["title"], "handler": handlers[k]["handler"]}
        for k in valid_model_keys
    ]

    run_id = uuid.uuid4().hex
    now_ts = time.time()
    total_pairs = tasks_qs.count() * len(ordered_models)
    job = {
        "run_id": run_id,
        "run_type": "batch",
        "status": "running",
        "error_message": "",
        "total_models": len(ordered_models),
        "total_pairs": total_pairs,
        "completed_pairs": 0,
        "completed_models": 0,
        "current_model_key": ordered_models[0]["key"],
        "current_model_title": ordered_models[0]["title"],
        "current_task_node_id": "",
        "current_task_name": "",
        "results": [],
        "report": None,
        "created_at_ts": now_ts,
        "updated_at_ts": now_ts,
    }
    with _jobs_lock:
        _prune_old_jobs(now_ts)
        _jobs[run_id] = job

    worker = threading.Thread(
        target=_run_batch_job_worker,
        args=(run_id, tasks_qs, ordered_models, user_id, session_id),
        kwargs={"ui_language": ui_language},
        name=f"arm-batch-run-{run_id[:8]}",
        daemon=True,
    )
    worker.start()
    return run_id, ""


def _snapshot_from_test_run(test_run):
    is_batch = test_run.run_type == AIModelTestRun.RUN_TYPE_BATCH
    status_map = {
        AIModelTestRun.STATUS_RUNNING: "running",
        AIModelTestRun.STATUS_COMPLETED: "completed",
        AIModelTestRun.STATUS_FAILED: "failed",
    }
    current_key = ""
    current_title = ""
    current_task_node_id = ""
    current_task_name = ""
    with _jobs_lock:
        job = _jobs.get(test_run.run_id)
        if job:
            current_key = job.get("current_model_key", "")
            current_title = job.get("current_model_title", "")
            current_task_node_id = job.get("current_task_node_id", "")
            current_task_name = job.get("current_task_name", "")

    if is_batch:
        results = _batch_results_from_db(test_run)
        report = _build_batch_report(results)
        is_failed = test_run.status == AIModelTestRun.STATUS_FAILED
        total_pairs = (test_run.report or {}).get("total_pairs") if test_run.report else None
        return {
            "run_id": test_run.run_id,
            "run_type": "batch",
            "status": status_map.get(test_run.status, test_run.status),
            "error_message": test_run.error_message or ("Batch solve завершился с ошибкой" if is_failed else ""),
            "total_models": test_run.total_models or 0,
            "total_pairs": total_pairs or len(results),
            "completed_pairs": len(results),
            "current_model_key": current_key,
            "current_model_title": current_title,
            "current_task_node_id": current_task_node_id,
            "current_task_name": current_task_name,
            "results": results,
            "report": report,
            "created_at_ts": test_run.started_at.timestamp() if test_run.started_at else 0.0,
            "updated_at_ts": test_run.finished_at.timestamp() if test_run.finished_at else 0.0,
        }

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
    is_failed = test_run.status == AIModelTestRun.STATUS_FAILED
    return {
        "run_id": test_run.run_id,
        "run_type": "single",
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