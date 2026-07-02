"""Prompt regression test runner.

Runs a single model over a set of :class:`PromptTestCase` fixtures with a
chosen prompt under test, compares each model response to the case's golden
``expected_text`` via the deterministic comparators in :mod:`ai.grading`, and
persists the per-case verdict in :class:`PromptTestResult`.

Structurally mirrors :mod:`ai.arm_runner` (in-memory live job + DB as source of
truth for completed/evicted runs), but is keyed by ``(run, test_case)`` and the
message is composed through the shared :class:`ai.services.MessageComposer` so a
prompt edit changes the sent message — exactly what a regression suite must
catch. See ``CLAUDE.md`` (DRY/SOLID): no third copy of message-building.
"""

import copy
import threading
import time
import uuid
from time import perf_counter

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.utils import timezone
from django.utils.html import strip_tags

from .grading import compare_response
from .model_clients.exceptions import humanize_model_error
from .model_health import get_runtime_model_handlers
from .models import (
    AIRequestLog,
    ExternalDLAccount,
    PromptTestCase,
    PromptTestResult,
    PromptTestRun,
)
from .services import MessageComposer


User = get_user_model()

_jobs_lock = threading.Lock()
_jobs = {}
_MAX_JOB_AGE_SECONDS = 6 * 60 * 60

# Build the MessageComposer once per process; it is stateless across runs.
_composer = MessageComposer()


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


def _compose_message(case, prompt_id, ui_language):
    """Build the user message for one case via the shared MessageComposer.

    Mirrors how the WebSocket consumer composes messages for chat / solve /
    find-error, so a prompt edit flows through identically here. ``prompt_id``
    is the prompt-under-test (int Prompt id, ``shared_<pk>``, or None).
    """
    pl_name = case.programming_language.language_name if case.programming_language else ""
    topic_name = case.topic.topic_name if case.topic else ""

    if case.mode == "solve":
        data = {
            "type": "2",
            "message": case.input_text,
            "language": ui_language,
            "programming_language_name": pl_name,
            "topic_name": topic_name,
            "preprompt": prompt_id,
        }
    elif case.mode == "find_error":
        data = {
            "type": "3",
            "message": "",
            "code": case.input_text,
            "language": ui_language,
            "programming_language_name": pl_name,
            "topic_name": topic_name,
            "preprompt": prompt_id,
        }
    else:  # chat
        data = {
            "type": "1",
            "message": case.input_text,
            "language": ui_language,
            "preprompt": prompt_id,
        }

    message, _log_mode = async_to_sync(_composer.compose)(data)
    return message, pl_name, topic_name


def _update_job(run_id, **updates):
    with _jobs_lock:
        job = _jobs.get(run_id)
        if not job:
            return
        job.update(updates)
        job["updated_at_ts"] = time.time()


def _build_prompt_test_report(results):
    """Aggregate per-case results into the report shown in the admin / CLI.

    ``mismatches`` carries the full expected/actual pair for every deviation,
    which is exactly what the operator asked for ("в скольких задачах
    отклонения + для каждой — новую и эталонную реакцию").
    """
    if not results:
        return None

    matched = sum(1 for r in results if r.get("verdict") == PromptTestResult.VERDICT_MATCH)
    mismatched = sum(1 for r in results if r.get("verdict") == PromptTestResult.VERDICT_MISMATCH)
    skipped = sum(1 for r in results if r.get("verdict") == PromptTestResult.VERDICT_SKIPPED)
    tokens_total = sum(_to_int(r.get("tokens")) for r in results)

    mismatches = [
        {
            "case_id": r.get("case_id"),
            "case_name": r.get("case_name") or "—",
            "mode": r.get("mode") or "",
            "expected": r.get("expected") or "",
            "actual": r.get("actual") or "",
            "diff_hint": r.get("diff_hint") or "",
            "verdict": r.get("verdict"),
        }
        for r in results
        if r.get("verdict") == PromptTestResult.VERDICT_MISMATCH
    ]

    per_mode = _per_mode_bucket(results)

    return {
        "total": len(results),
        "matched": matched,
        "mismatched": mismatched,
        "skipped": skipped,
        "tokens_total": tokens_total,
        "mismatches": mismatches,
        "per_mode": per_mode,
    }


def _per_mode_bucket(results):
    buckets = {}
    for r in results:
        mode = r.get("mode") or "—"
        bucket = buckets.setdefault(
            mode, {"mode": mode, "total": 0, "matched": 0, "mismatched": 0, "skipped": 0, "tokens": 0}
        )
        bucket["total"] += 1
        bucket["tokens"] += _to_int(r.get("tokens"))
        verdict = r.get("verdict")
        if verdict == PromptTestResult.VERDICT_MATCH:
            bucket["matched"] += 1
        elif verdict == PromptTestResult.VERDICT_MISMATCH:
            bucket["mismatched"] += 1
        elif verdict == PromptTestResult.VERDICT_SKIPPED:
            bucket["skipped"] += 1
    rows = list(buckets.values())
    rows.sort(key=lambda row: (-row["mismatched"], row["mode"]))
    return rows


def _run_job_worker(run_id, cases, model, user_id, *, prompt_id=None, ui_language="Русский"):
    test_run = None
    log = None
    start_time = timezone.now()
    try:
        user, username, external_id, full_name = _resolve_user(user_id)
        prompt_name = ""
        if prompt_id:
            from .models import Prompt, SharedPrompt
            from .services.prompt_resolver import parse_shared_prompt_id

            shared_pk = parse_shared_prompt_id(str(prompt_id))
            try:
                if shared_pk is not None:
                    prompt_obj = SharedPrompt.objects.get(id=shared_pk)
                else:
                    prompt_obj = Prompt.objects.filter(id=int(prompt_id)).first()
                if prompt_obj:
                    prompt_name = getattr(prompt_obj, "prompt_name", "") or str(prompt_obj)
            except (ValueError, Prompt.DoesNotExist, SharedPrompt.DoesNotExist):
                prompt_name = ""

        test_run = PromptTestRun.objects.create(
            run_id=run_id,
            status=PromptTestRun.STATUS_RUNNING,
            model_key=model["key"],
            model_title=model["title"],
            prompt_id=int(prompt_id) if (str(prompt_id).isdigit() if prompt_id else False) else None,
            prompt_name=prompt_name,
            ui_language=ui_language,
            user=user,
            started_at=start_time,
            total_cases=len(cases),
        )
        log = AIRequestLog.objects.create(
            user=user,
            username=username,
            external_user_id=external_id,
            user_full_name=full_name,
            source=AIRequestLog.SOURCE_ARM,
            mode=AIRequestLog.MODE_ARM,
            sent_at=start_time,
            model_names=[model["title"]],
            message=f"Prompt regression run {run_id}",
        )

        if not cases:
            _update_job(
                run_id,
                status="failed",
                error_message="Нет активных тест-кейсов для запуска.",
                current_case_name="",
            )
            end_time = timezone.now()
            AIRequestLog.objects.filter(pk=log.pk).update(
                received_at=end_time, duration_seconds=0,
                status=AIRequestLog.STATUS_ERROR, error_message="Нет активных тест-кейсов",
            )
            PromptTestRun.objects.filter(pk=test_run.pk).update(
                status=PromptTestRun.STATUS_FAILED, finished_at=end_time,
                error_message="Нет активных тест-кейсов для запуска",
            )
            return

        total = len(cases)
        _update_job(
            run_id,
            total_cases=total,
            completed_cases=0,
            current_case_name=cases[0].name,
        )

        completed = 0
        for case in cases:
            started = perf_counter()
            verdict = PromptTestResult.VERDICT_SKIPPED
            status = "error"
            actual_text = ""
            short_response = ""
            tokens = 0
            diff_hint = ""

            try:
                message, pl_name, topic_name = _compose_message(case, prompt_id, ui_language)
                response = async_to_sync(model["handler"])(
                    message,
                    f"prompt-test-{user_id}-{model['key']}-{run_id}-{case.id}",
                )
                response_text, tokens = _extract_model_response(response)
                cleaned_text = strip_tags(response_text).strip()
                friendly, detailed = humanize_model_error(cleaned_text, include_detail=True)

                verdict, diff_hint, _missing = compare_response(
                    cleaned_text,
                    case.expected_text,
                    comparator=case.comparator,
                    threshold=case.match_threshold,
                )
                is_ok = bool(friendly) and "ошибка" not in friendly.lower()[:25]
                status = "ok" if is_ok else "error"
                actual_text = detailed or cleaned_text
                short_response = (friendly or cleaned_text)[:300] + (
                    "..." if len(friendly or cleaned_text) > 300 else ""
                )
            except Exception as exc:
                exc_text = str(exc)
                friendly, detailed = humanize_model_error(exc_text, include_detail=True)
                verdict = PromptTestResult.VERDICT_SKIPPED
                status = "error"
                actual_text = detailed or exc_text
                short_response = friendly or f"Ошибка вызова модели: {exc_text}"
                diff_hint = "ошибка вызова модели"

            duration = round(perf_counter() - started, 2)
            pl_name_snap = case.programming_language.language_name if case.programming_language else ""
            topic_name_snap = case.topic.topic_name if case.topic else ""

            PromptTestResult.objects.update_or_create(
                run=test_run,
                test_case=case,
                defaults={
                    "model_key": model["key"],
                    "model_title": model["title"],
                    "status": status,
                    "verdict": verdict,
                    "actual_response": (actual_text or "")[:8000],
                    "expected_snapshot": (case.expected_text or "")[:8000],
                    "diff_hint": (diff_hint or "")[:255],
                    "duration_seconds": duration,
                    "tokens": tokens,
                    "case_name_snapshot": case.name or f"Тест-кейс #{case.id}",
                    "mode_snapshot": case.mode,
                    "topic_name_snapshot": topic_name_snap,
                    "prog_lang_snapshot": pl_name_snap,
                },
            )

            result_item = {
                "case_id": case.id,
                "case_name": case.name or f"Тест-кейс #{case.id}",
                "mode": case.mode,
                "model_key": model["key"],
                "model_title": model["title"],
                "status": status,
                "verdict": verdict,
                "actual": actual_text or "",
                "expected": case.expected_text or "",
                "diff_hint": diff_hint or "",
                "duration": duration,
                "tokens": tokens,
                "topic_name": topic_name_snap,
                "prog_lang_name": pl_name_snap,
            }

            completed += 1
            with _jobs_lock:
                job = _jobs.get(run_id)
                if job is None:
                    pass  # evicted mid-run; terminal state rebuilt from DB below
                else:
                    job.setdefault("results", []).append(result_item)
                    job["completed_cases"] = completed
                    if completed < total:
                        nxt = cases[completed]
                        job["current_case_name"] = nxt.name
                    else:
                        job["current_case_name"] = ""
                    job["updated_at_ts"] = time.time()

        # Finalize.
        with _jobs_lock:
            job = _jobs.get(run_id)
            evicted = job is None
            if not evicted:
                job["report"] = _build_prompt_test_report(job.get("results") or [])
                job["status"] = "completed"
                job["updated_at_ts"] = time.time()
                results = list(job.get("results") or [])
                report = job["report"]

        if evicted:
            results = _results_from_db(test_run)
            report = _build_prompt_test_report(results)

        end_time = timezone.now()
        AIRequestLog.objects.filter(pk=log.pk).update(
            received_at=end_time,
            duration_seconds=(end_time - start_time).total_seconds(),
            status=AIRequestLog.STATUS_SUCCESS,
        )
        PromptTestRun.objects.filter(pk=test_run.pk).update(
            status=PromptTestRun.STATUS_COMPLETED,
            finished_at=end_time,
            report=report or {},
        )
        _update_job(run_id, status="completed")

    except Exception as exc:
        _update_job(
            run_id,
            status="failed",
            error_message=f"Регрессионный прогон завершился с ошибкой: {exc}",
            current_case_name="",
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
            PromptTestRun.objects.filter(pk=test_run.pk).update(
                status=PromptTestRun.STATUS_FAILED,
                finished_at=end_time,
                error_message=str(exc)[:2000],
            )


def _results_from_db(test_run):
    """Rebuild in-memory result items from persisted PromptTestResult rows."""
    results = []
    for r in test_run.results.all().order_by("case_name_snapshot"):
        results.append({
            "case_id": r.test_case_id,
            "case_name": r.case_name_snapshot,
            "mode": r.mode_snapshot,
            "model_key": r.model_key,
            "model_title": r.model_title,
            "status": r.status,
            "verdict": r.verdict or PromptTestResult.VERDICT_SKIPPED,
            "actual": r.actual_response,
            "expected": r.expected_snapshot,
            "diff_hint": r.diff_hint,
            "duration": r.duration_seconds or 0.0,
            "tokens": r.tokens or 0,
            "topic_name": r.topic_name_snapshot,
            "prog_lang_name": r.prog_lang_snapshot,
        })
    return results


def start_prompt_test_run(case_ids, model_key, user_id, *, prompt_id=None, ui_language="Русский"):
    """Start a prompt-regression run over the given cases × one model.

    Returns ``(run_id, error_message)``. Empty ``case_ids`` means "all active
    cases". A missing/unknown model returns an error without starting a run.
    """
    handlers = get_runtime_model_handlers()
    model_info = handlers.get(model_key)
    if not model_info:
        return None, "Выбранная модель недоступна. Обновите состояние моделей и попробуйте снова."

    cases_qs = PromptTestCase.objects.filter(active=True).select_related(
        "topic", "programming_language"
    ).order_by("id")
    if case_ids:
        cases_qs = cases_qs.filter(pk__in=case_ids)
    cases = list(cases_qs)
    if not cases:
        return None, "Нет активных тест-кейсов для запуска."

    run_id = uuid.uuid4().hex
    now_ts = time.time()
    job = {
        "run_id": run_id,
        "status": "running",
        "error_message": "",
        "total_cases": len(cases),
        "completed_cases": 0,
        "current_case_name": cases[0].name,
        "results": [],
        "report": None,
        "created_at_ts": now_ts,
        "updated_at_ts": now_ts,
    }
    with _jobs_lock:
        _prune_old_jobs(now_ts)
        _jobs[run_id] = job

    model = {"key": model_key, "title": model_info["title"], "handler": model_info["handler"]}
    worker = threading.Thread(
        target=_run_job_worker,
        args=(run_id, cases, model, user_id),
        kwargs={"prompt_id": prompt_id, "ui_language": ui_language},
        name=f"prompt-test-run-{run_id[:8]}",
        daemon=True,
    )
    worker.start()
    return run_id, ""


def _snapshot_from_test_run(test_run):
    status_map = {
        PromptTestRun.STATUS_RUNNING: "running",
        PromptTestRun.STATUS_COMPLETED: "completed",
        PromptTestRun.STATUS_FAILED: "failed",
    }
    results = _results_from_db(test_run)
    report = _build_prompt_test_report(results)
    current_case_name = ""
    with _jobs_lock:
        job = _jobs.get(test_run.run_id)
        if job:
            current_case_name = job.get("current_case_name", "")

    return {
        "run_id": test_run.run_id,
        "status": status_map.get(test_run.status, test_run.status),
        "error_message": test_run.error_message or "",
        "model_key": test_run.model_key,
        "model_title": test_run.model_title,
        "prompt_id": test_run.prompt_id,
        "prompt_name": test_run.prompt_name,
        "ui_language": test_run.ui_language,
        "total_cases": test_run.total_cases or len(results),
        "completed_cases": len(results),
        "current_case_name": current_case_name,
        "results": results,
        "report": report,
        "created_at_ts": test_run.started_at.timestamp() if test_run.started_at else 0.0,
        "updated_at_ts": test_run.finished_at.timestamp() if test_run.finished_at else 0.0,
    }


def get_prompt_test_run_snapshot(run_id):
    if not run_id:
        return None

    # Live in-memory job takes precedence while the run is in flight.
    with _jobs_lock:
        job = _jobs.get(run_id)
        if job:
            return copy.deepcopy(job)

    # Source of truth for completed/evicted runs: the database.
    try:
        test_run = PromptTestRun.objects.get(run_id=run_id)
    except PromptTestRun.DoesNotExist:
        return None
    return _snapshot_from_test_run(test_run)