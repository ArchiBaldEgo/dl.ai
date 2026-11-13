"""Раннер лаборатории промптов режима «В чём ошибка?».

В отличие от ``arm_runner`` здесь нет DL-тестов и выгрузки кода: лаборатория
прогоняет production-сборку сообщения (``MessageComposer``, ``type=3``) по
размеченной базе ошибочных программ и считает детерминированные метрики по
служебной строке ``КОДЫ:`` (``ai/grading.py``).

Модель потоков: один поток на модель + координатор, который их дожидается и
финализирует прогон. ``PromptLabRun``/``PromptLabResult`` — источник правды;
``_LAB_JOBS`` — только live-прогресс (как ``_jobs`` в ARM).
"""

from __future__ import annotations

import logging
import threading
import time
import uuid

from django.db import close_old_connections, transaction
from django.utils import timezone

from .arm_runner import (
    _clean_model_text,
    _extract_model_response,
    _extract_prompt_tokens,
    _is_model_error,
)
from .grading import aggregate_error_code_metrics, compare_error_codes, parse_error_codes
from .models import AIRequestLog, FindErrorCase, Prompt, PromptLabResult, PromptLabRun
from .model_clients.history import ConversationHistory
from .model_health import get_runtime_model_handlers, is_arm_solve_model
from .services.auth import get_user_identity_for_log
from .services.find_error_catalog import build_eval_suffix
from .services.message_composer import MessageComposer
from asgiref.sync import async_to_sync

logger = logging.getLogger(__name__)

PROMPT_LAB_MAX_CASES = 100
PROMPT_LAB_MAX_MODELS = 5
_MODEL_MAX_ATTEMPTS = 2
_ORPHAN_GRACE_SECONDS = 60

_LAB_JOBS: dict[str, dict] = {}
_LAB_LOCK = threading.Lock()
_HISTORY = ConversationHistory()
_UI_LANGUAGE_NAMES = {
    "ru": "Russian",
    "en": "English",
    "fr": "Français",
    "Russian": "Russian",
    "English": "English",
    "Français": "Français",
}


class PromptLabError(ValueError):
    """Некорректные параметры запуска (вью превращает в 400 JSON)."""


def _ui_language_name(value: str) -> str:
    return _UI_LANGUAGE_NAMES.get((value or "ru").strip(), "Russian")


def _update_job(run_id: str, **kwargs):
    with _LAB_LOCK:
        job = _LAB_JOBS.get(run_id)
        if job is None:
            return
        job.update(kwargs)
        job["updated_at_ts"] = time.time()


def _bump_job(run_id: str, *, model_key: str, error: bool = False):
    with _LAB_LOCK:
        job = _LAB_JOBS.get(run_id)
        if job is None:
            return
        job["done"] = job.get("done", 0) + 1
        if error:
            job["errors"] = job.get("errors", 0) + 1
        model = job.get("models", {}).get(model_key)
        if model is not None:
            model["done"] = model.get("done", 0) + 1
            if error:
                model["errors"] = model.get("errors", 0) + 1
        job["updated_at_ts"] = time.time()


def _is_cancel_requested(run_id: str) -> bool:
    with _LAB_LOCK:
        job = _LAB_JOBS.get(run_id)
        return bool(job and job.get("cancel"))


def _resolve_models(model_keys) -> list[dict]:
    handlers = get_runtime_model_handlers()
    models = []
    for key in model_keys or ():
        key = str(key or "").strip()
        if not key or not is_arm_solve_model(key):
            continue
        entry = handlers.get(key)
        if entry is None:
            continue
        models.append({"key": key, "title": entry["title"], "handler": entry["handler"]})
    if not models:
        raise PromptLabError("Не выбрано ни одной доступной модели.")
    if len(models) > PROMPT_LAB_MAX_MODELS:
        raise PromptLabError(
            f"За один прогон можно выбрать не более {PROMPT_LAB_MAX_MODELS} моделей."
        )
    return models


def _resolve_prompt(prompt_id):
    """(prompt_obj | None, prompt_name, prompt_text, base_text, snapshot_id).

    ``prompt_id`` пустой/None — только общий промпт режима. Строка
    ``shared_<pk>`` допустима: так же работает production-резолвер.
    """
    from .models import SharedPrompt

    base = SharedPrompt.objects.filter(mode="find_error").first()
    base_text = (base.prompt_text_ru if base is not None else "") or ""
    if prompt_id in (None, "", 0, "0"):
        return None, "", "", base_text, None
    text_id = str(prompt_id)
    if text_id.startswith("shared_"):
        try:
            shared = SharedPrompt.objects.get(pk=int(text_id.split("_", 1)[1]))
        except (ValueError, SharedPrompt.DoesNotExist):
            raise PromptLabError("Общий промпт не найден.") from None
        if shared.mode and shared.mode != "find_error":
            raise PromptLabError("Общий промпт относится к другому режиму.")
        return None, str(shared), shared.prompt_text_ru or "", base_text, None
    try:
        prompt = Prompt.objects.get(pk=int(prompt_id))
    except (ValueError, TypeError, Prompt.DoesNotExist):
        raise PromptLabError("Промпт не найден.") from None
    if prompt.mode and prompt.mode != "find_error":
        raise PromptLabError("Промпт относится к другому режиму (нужен find_error).")
    return prompt, str(prompt), prompt.prompt_text_ru or "", base_text, prompt.pk


def start_prompt_lab_run(
    *,
    user,
    case_ids,
    model_keys,
    prompt_id=None,
    ui_language="ru",
    include_eval_suffix=True,
    run_params=None,
) -> PromptLabRun:
    """Проверить параметры, создать прогон и запустить фоновые потоки."""
    ids = []
    for value in case_ids or ():
        try:
            pk = int(value)
        except (TypeError, ValueError):
            continue
        if pk not in ids:
            ids.append(pk)
    if not ids:
        raise PromptLabError("Не выбрано ни одного кейса.")
    if len(ids) > PROMPT_LAB_MAX_CASES:
        raise PromptLabError(
            f"За один прогон можно взять не более {PROMPT_LAB_MAX_CASES} кейсов."
        )
    cases = list(
        FindErrorCase.objects.filter(pk__in=ids)
        .exclude(status=FindErrorCase.STATUS_DISABLED)
        .select_related("group", "group__programming_language")
        .prefetch_related("labels")
    )
    if not cases:
        raise PromptLabError("Выбранные кейсы не найдены или отключены.")
    order = {pk: index for index, pk in enumerate(ids)}
    cases.sort(key=lambda case: order.get(case.pk, 0))

    models = _resolve_models(model_keys)
    prompt, prompt_name, prompt_text, base_text, prompt_snapshot_id = _resolve_prompt(prompt_id)

    run = PromptLabRun.objects.create(
        run_id=uuid.uuid4().hex,
        user=user if getattr(user, "is_authenticated", False) else None,
        status=PromptLabRun.STATUS_RUNNING,
        prompt=prompt,
        prompt_id_snapshot=prompt_snapshot_id,
        prompt_name=prompt_name,
        prompt_text=prompt_text,
        base_prompt_text=base_text,
        model_keys=[model["key"] for model in models],
        model_titles=[model["title"] for model in models],
        case_ids=[case.pk for case in cases],
        case_count=len(cases),
        ui_language=_ui_language_name(ui_language),
        include_eval_suffix=bool(include_eval_suffix),
        run_params=run_params or {},
    )
    with _LAB_LOCK:
        _LAB_JOBS[run.run_id] = {
            "run_id": run.run_id,
            "status": "running",
            "total": len(cases) * len(models),
            "done": 0,
            "errors": 0,
            "cancel": False,
            "started_ts": time.time(),
            "updated_at_ts": time.time(),
            "models": {
                model["key"]: {
                    "title": model["title"],
                    "done": 0,
                    "errors": 0,
                    "total": len(cases),
                }
                for model in models
            },
        }
    thread = threading.Thread(
        target=_run_coordinator,
        args=(run.pk, run.run_id, case_ids, [m["key"] for m in models], str(prompt_id or "")),
        name=f"prompt-lab-{run.run_id[:8]}",
        daemon=True,
    )
    thread.start()
    return run


def _run_coordinator(run_pk, run_id, case_ids, model_keys, prompt_id):
    try:
        models = _resolve_models(model_keys)
    except PromptLabError as exc:
        _finalize_run(run_pk, run_id, PromptLabRun.STATUS_FAILED, error_message=str(exc))
        return
    threads = []
    for model_index, model in enumerate(models):
        thread = threading.Thread(
            target=_run_model_job,
            args=(run_pk, run_id, case_ids, model, model_index, prompt_id),
            name=f"prompt-lab-{run_id[:8]}-{model_index}",
            daemon=True,
        )
        thread.start()
        threads.append(thread)
    for thread in threads:
        thread.join()
    if _is_cancel_requested(run_id):
        _finalize_run(run_pk, run_id, PromptLabRun.STATUS_CANCELLED)
    else:
        _finalize_run(run_pk, run_id, PromptLabRun.STATUS_COMPLETED)


def _compose_case_message(case, ui_language, prompt_id, include_eval_suffix):
    group = case.group
    language_name = _ui_language_name(ui_language)
    prog_language_name = (
        group.programming_language.language_name if group.programming_language_id else ""
    )
    data = {
        "type": 3,
        "message": group.statement or "",
        "code": case.code or "",
        "language": language_name,
        "programming_language_name": prog_language_name,
        "topic_name": group.topic_name or "",
        "preprompt": prompt_id or "",
    }
    message, _mode = async_to_sync(MessageComposer().compose)(data)
    if include_eval_suffix:
        message = f"{message}\n\n{build_eval_suffix()}"
    return message


def _call_model(handler, message, client_id):
    """До двух попыток; возвращает (response, final_exception)."""
    last_exc = None
    for attempt in range(_MODEL_MAX_ATTEMPTS):
        try:
            response = async_to_sync(handler)(message, client_id)
            last_exc = None
        except Exception as exc:  # noqa: BLE001 — сбой провайдера, не прогона
            response = None
            last_exc = exc
        if last_exc is None and not _is_model_error(response):
            return response, None
        if attempt + 1 < _MODEL_MAX_ATTEMPTS:
            time.sleep(0.5)
    return None, last_exc or RuntimeError("Модель вернула ошибку.")


def _log_request(run, case, model, *, started_at, finished_at, duration, raw, tokens,
                 prompt_tokens, status, error_message="", client_id=""):
    identity = get_user_identity_for_log(run.user, None)
    group = case.group
    AIRequestLog.objects.create(
        user=identity["user"],
        username=identity["username"],
        external_user_id=identity["external_user_id"],
        user_full_name=identity["user_full_name"],
        client_id=client_id,
        source=AIRequestLog.SOURCE_ARM,
        mode=AIRequestLog.MODE_PROMPT_DEBUG,
        sent_at=started_at,
        received_at=finished_at,
        duration_seconds=duration,
        model_names=[model["title"]],
        message=f"Prompt lab run {run.run_id}: {case.source_ref}",
        response_text=raw or "",
        tokens=tokens,
        tokens_prompt=prompt_tokens,
        batch_run_id="",
        status=status,
        error_message=error_message,
        programming_language_id=group.programming_language_id,
        programming_language_name=(
            group.programming_language.language_name if group.programming_language_id else ""
        ),
        topic_name=group.topic_name or "",
        prompt_id=run.prompt_id_snapshot,
        prompt_name=run.prompt_name or "",
    )


def _run_model_job(run_pk, run_id, case_ids, model, model_index, prompt_id):
    close_old_connections()
    try:
        run = PromptLabRun.objects.select_related("prompt").get(pk=run_pk)
        cases = list(
            FindErrorCase.objects.filter(pk__in=case_ids)
            .exclude(status=FindErrorCase.STATUS_DISABLED)
            .select_related("group", "group__programming_language")
            .prefetch_related("labels")
        )
        order = {pk: index for index, pk in enumerate(case_ids)}
        cases.sort(key=lambda case: order.get(case.pk, 0))
        for case in cases:
            if _is_cancel_requested(run_id):
                break
            client_id = f"lab-{run_id}-{model_index}-{case.pk}"
            message = ""
            raw = ""
            tokens = prompt_tokens = 0
            started_at = timezone.now()
            started_monotonic = time.monotonic()
            try:
                message = _compose_case_message(
                    case, run.ui_language, prompt_id, run.include_eval_suffix,
                )
                response, exc = _call_model(model["handler"], message, client_id)
                duration = round(time.monotonic() - started_monotonic, 2)
                finished_at = timezone.now()
                if exc is not None:
                    error_message = str(exc)[:2000]
                    _save_result(run, case, model, status=PromptLabResult.STATUS_ERROR,
                                 message=message, error_message=error_message,
                                 duration=duration)
                    _log_request(run, case, model, started_at=started_at,
                                 finished_at=finished_at, duration=duration, raw="",
                                 tokens=0, prompt_tokens=0,
                                 status=AIRequestLog.STATUS_ERROR,
                                 error_message=error_message, client_id=client_id)
                    _bump_job(run_id, model_key=model["key"], error=True)
                    continue
                raw_text, tokens = _extract_model_response(response)
                prompt_tokens = _extract_prompt_tokens(response)
                cleaned = _clean_model_text(raw_text)
                parsed = parse_error_codes(cleaned)
                expected = sorted(label.code for label in case.labels.all())
                comparison = compare_error_codes(parsed, expected)
                _save_result(
                    run, case, model, status=PromptLabResult.STATUS_OK,
                    message=message, raw_response=cleaned, comparison=comparison,
                    duration=duration, tokens=tokens, prompt_tokens=prompt_tokens,
                )
                _log_request(run, case, model, started_at=started_at,
                             finished_at=finished_at, duration=duration, raw=cleaned,
                             tokens=tokens, prompt_tokens=prompt_tokens,
                             status=AIRequestLog.STATUS_SUCCESS, client_id=client_id)
                _bump_job(run_id, model_key=model["key"])
            except Exception as exc:  # noqa: BLE001 — один кейс не роняет прогон
                logger.exception("Prompt lab: model %s case %s failed", model["key"], case.pk)
                duration = round(time.monotonic() - started_monotonic, 2)
                error_message = str(exc)[:2000]
                _save_result(run, case, model, status=PromptLabResult.STATUS_ERROR,
                             message=message, error_message=error_message,
                             duration=duration)
                _bump_job(run_id, model_key=model["key"], error=True)
            finally:
                try:
                    _HISTORY.reset(client_id)
                except Exception:  # noqa: BLE001 — кэш истории не критичен
                    pass
    finally:
        close_old_connections()


def _save_result(run, case, model, *, status, message="", raw_response="",
                 error_message="", comparison=None, duration=0.0,
                 tokens=0, prompt_tokens=0):
    values = {
        "case_ref": case.source_ref,
        "case_name": case.name,
        "group_name": case.group.name,
        "model_title": model["title"],
        "status": status,
        "error_message": error_message,
        "message": message,
        "raw_response": raw_response,
        "duration_seconds": duration,
        "tokens": tokens,
        "tokens_prompt": prompt_tokens,
    }
    if comparison is not None:
        values.update({
            "parsed_codes": comparison["parsed"],
            "expected_codes": comparison["expected"],
            "matched_codes": comparison["matched"],
            "missed_codes": comparison["missed"],
            "extra_codes": comparison["extra"],
            "exact_match": comparison["exact"],
            "precision": comparison["precision"],
            "recall": comparison["recall"],
            "f1": comparison["f1"],
        })
    with transaction.atomic():
        PromptLabResult.objects.update_or_create(
            run=run, model_key=model["key"], case=case, defaults=values,
        )


def _build_report(run: PromptLabRun) -> dict:
    results = list(run.results.select_related("case").order_by("case_ref", "model_key"))
    per_model = {}
    all_rows = []
    for result in results:
        bucket = per_model.setdefault(result.model_key, {
            "model_key": result.model_key,
            "model_title": result.model_title,
            "rows": [],
            "errors": 0,
            "duration": 0.0,
            "duration_count": 0,
            "tokens": 0,
            "tokens_prompt": 0,
        })
        if result.status != PromptLabResult.STATUS_OK:
            bucket["errors"] += 1
            continue
        row = {"parsed_codes": result.parsed_codes, "expected_codes": result.expected_codes}
        bucket["rows"].append(row)
        all_rows.append(row)
        bucket["duration"] += result.duration_seconds or 0.0
        bucket["duration_count"] += 1
        bucket["tokens"] += result.tokens or 0
        bucket["tokens_prompt"] += result.tokens_prompt or 0
    models_report = []
    for bucket in per_model.values():
        metrics = aggregate_error_code_metrics(bucket["rows"])
        metrics.update({
            "model_key": bucket["model_key"],
            "model_title": bucket["model_title"],
            "errors": bucket["errors"],
            "avg_duration": (
                bucket["duration"] / bucket["duration_count"]
                if bucket["duration_count"] else 0.0
            ),
            "tokens": bucket["tokens"],
            "tokens_prompt": bucket["tokens_prompt"],
        })
        models_report.append(metrics)
    models_report.sort(key=lambda item: (item["model_title"] or "").lower())
    overall = aggregate_error_code_metrics(all_rows)
    return {
        "models": models_report,
        "overall": overall,
        "results": len(results),
        "errors": sum(item["errors"] for item in models_report),
    }


def _finalize_run(run_pk, run_id, status, error_message=""):
    close_old_connections()
    try:
        run = PromptLabRun.objects.get(pk=run_pk)
        report = _build_report(run)
        PromptLabRun.objects.filter(pk=run_pk).update(
            status=status,
            finished_at=timezone.now(),
            report=report,
            error_message=error_message,
        )
        _update_job(run_id, status=status, error_message=error_message)
        with _LAB_LOCK:
            _LAB_JOBS.pop(run_id, None)
    finally:
        close_old_connections()


def cancel_prompt_lab_run(run_id, user=None) -> bool:
    """Попросить прогон остановиться; True — прогон найден и активен."""
    with _LAB_LOCK:
        job = _LAB_JOBS.get(run_id)
        if job is not None:
            job["cancel"] = True
    run = PromptLabRun.objects.filter(run_id=run_id).first()
    if run is None:
        return False
    if run.status != PromptLabRun.STATUS_RUNNING:
        return False
    if job is None:
        # Процесс уже потерян — финализируем сами, иначе прогон висел бы вечно.
        _finalize_run(run.pk, run_id, PromptLabRun.STATUS_CANCELLED)
    return True


def get_prompt_lab_snapshot(run_id, light=True):
    """Снапшот прогона для поллинга; ``None`` — прогон не найден."""
    run = PromptLabRun.objects.filter(run_id=run_id).first()
    if run is None:
        return None
    if run.status == PromptLabRun.STATUS_RUNNING:
        with _LAB_LOCK:
            has_live_job = run_id in _LAB_JOBS
            live_job = dict(_LAB_JOBS.get(run_id) or {})
        age = (timezone.now() - run.started_at).total_seconds()
        if not has_live_job and age > _ORPHAN_GRACE_SECONDS:
            close_old_connections()
            try:
                report = _build_report(run)
                PromptLabRun.objects.filter(pk=run.pk).update(
                    status=PromptLabRun.STATUS_FAILED,
                    finished_at=timezone.now(),
                    error_message="Процесс потерян (перезапуск сервера?).",
                    report=report,
                )
                run.refresh_from_db()
            finally:
                close_old_connections()
    else:
        live_job = {}

    results = run.results.select_related("case").order_by("case_ref", "model_key")
    items = []
    for result in results:
        item = {
            "id": result.pk,
            "case_ref": result.case_ref,
            "case_name": result.case_name,
            "group_name": result.group_name,
            "model_key": result.model_key,
            "model_title": result.model_title,
            "status": result.status,
            "error_message": result.error_message,
            "parsed_codes": result.parsed_codes,
            "expected_codes": result.expected_codes,
            "matched_codes": result.matched_codes,
            "missed_codes": result.missed_codes,
            "extra_codes": result.extra_codes,
            "exact_match": result.exact_match,
            "precision": result.precision,
            "recall": result.recall,
            "f1": result.f1,
            "duration_seconds": result.duration_seconds,
            "tokens": result.tokens,
            "tokens_prompt": result.tokens_prompt,
        }
        if not light:
            item["message"] = result.message
            item["raw_response"] = result.raw_response
        items.append(item)

    return {
        "run_id": run.run_id,
        "status": run.status,
        "prompt_name": run.prompt_name,
        "prompt_id": run.prompt_id_snapshot,
        "model_keys": run.model_keys,
        "model_titles": run.model_titles,
        "case_count": run.case_count,
        "include_eval_suffix": run.include_eval_suffix,
        "started_at": run.started_at.isoformat() if run.started_at else "",
        "finished_at": run.finished_at.isoformat() if run.finished_at else "",
        "error_message": run.error_message,
        "report": run.report or {},
        "progress": live_job,
        "results": items,
    }
