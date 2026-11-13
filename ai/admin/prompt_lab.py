"""Страница «Лаборатория промптов» и её JSON/CSV-эндпоинты.

Вью только проверяют права и сериализуют; запуск/прогресс/метрики живут в
``ai/prompt_lab_runner.py``. Страница рендерит форму (промпт, модели, кейсы)
и читает статус поллингом.
"""

from __future__ import annotations

import csv
import io
import json

from django.http import HttpResponse, HttpResponseForbidden, HttpResponseNotAllowed, JsonResponse
from django.template.response import TemplateResponse

from ..i18n import get_localized_name
from ..model_health import get_arm_solve_model_options
from ..models import FindErrorCase, FindErrorCaseGroup, Prompt, PromptLabResult, PromptLabRun
from ..prompt_lab_runner import (
    PROMPT_LAB_MAX_CASES,
    PromptLabError,
    cancel_prompt_lab_run,
    get_prompt_lab_snapshot,
    start_prompt_lab_run,
)
from .permissions import can_access_prompt_lab
from .site import ai_admin_site


def _deny():
    return HttpResponseForbidden("Access denied")


def _prompt_options():
    options = [{"id": "", "label": "По умолчанию (только общий препромпт режима)"}]
    prompts = (
        Prompt.objects.filter(mode="find_error")
        .select_related("programming_language", "topic")
        .order_by("prompt_name_ru", "pk")
    )
    for prompt in prompts:
        parts = [get_localized_name(prompt, "", "prompt_name") or f"Промпт #{prompt.pk}"]
        if prompt.programming_language_id:
            parts.append(prompt.programming_language.language_name)
        if prompt.topic_id:
            parts.append(get_localized_name(prompt.topic, "", "topic_name"))
        options.append({"id": prompt.pk, "label": " — ".join(parts)})
    return options


def _case_payload(case):
    return {
        "id": case.pk,
        "name": case.name,
        "ref": case.source_ref,
        "group": case.group.name,
        "group_id": case.group_id,
        "status": case.status,
        "has_statement": bool((case.group.statement or "").strip()),
        "codes": sorted(label.code for label in case.labels.all()),
    }


def _run_payload(run):
    return {
        "run_id": run.run_id,
        "status": run.status,
        "prompt_name": run.prompt_name or "По умолчанию",
        "model_titles": run.model_titles or [],
        "case_count": run.case_count,
        "started_at": run.started_at.strftime("%d.%m.%Y %H:%M") if run.started_at else "",
        "finished_at": run.finished_at.strftime("%d.%m.%Y %H:%M") if run.finished_at else "",
    }


def admin_prompt_lab_view(request):
    if not can_access_prompt_lab(request):
        return _deny()
    cases = (
        FindErrorCase.objects.exclude(status=FindErrorCase.STATUS_DISABLED)
        .select_related("group")
        .prefetch_related("labels")
        .order_by("group__name", "name")
    )
    context = {
        **ai_admin_site.each_context(request),
        "title": "AI: Лаборатория промптов",
        "prompt_options": _prompt_options(),
        "model_options": get_arm_solve_model_options(),
        "groups": FindErrorCaseGroup.objects.order_by("name"),
        "status_choices": FindErrorCase.STATUS_CHOICES,
        "cases_payload": [_case_payload(case) for case in cases],
        "recent_runs": [_run_payload(run) for run in PromptLabRun.objects.all()[:15]],
        "max_cases": PROMPT_LAB_MAX_CASES,
        "start_url": "/ai/admin/prompts/lab/start/",
        "status_url": "/ai/admin/prompts/lab/status/",
        "cancel_url": "/ai/admin/prompts/lab/cancel/",
        "body_url": "/ai/admin/prompts/lab/result/",
        "export_url": "/ai/admin/prompts/lab/export/",
        "cases_admin_url": "/ai/admin/ai/finderrorcasegroup/",
    }
    return TemplateResponse(request, "admin/ai/prompt_lab.html", context)


def _json_body(request):
    try:
        return json.loads(request.body or b"{}")
    except (TypeError, ValueError):
        return {}


def admin_prompt_lab_start_view(request):
    if not can_access_prompt_lab(request):
        return _deny()
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    payload = _json_body(request)
    try:
        run = start_prompt_lab_run(
            user=request.user,
            case_ids=payload.get("case_ids") or [],
            model_keys=payload.get("model_keys") or [],
            prompt_id=payload.get("prompt_id") or None,
            ui_language=payload.get("ui_language") or "ru",
            include_eval_suffix=payload.get("include_eval_suffix", True),
            run_params={"source": "prompt_lab_page"},
        )
    except PromptLabError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    return JsonResponse({"ok": True, "run_id": run.run_id})


def admin_prompt_lab_status_view(request):
    if not can_access_prompt_lab(request):
        return _deny()
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    run_id = (request.GET.get("run_id") or "").strip()
    if not run_id:
        return JsonResponse({"ok": False, "error": "Не указан run_id."}, status=400)
    snapshot = get_prompt_lab_snapshot(run_id, light=True)
    if snapshot is None:
        return JsonResponse({"ok": False, "error": "Прогон не найден."}, status=404)
    return JsonResponse({"ok": True, "snapshot": snapshot})


def admin_prompt_lab_result_body_view(request, result_id):
    if not can_access_prompt_lab(request):
        return _deny()
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    result = PromptLabResult.objects.filter(pk=result_id).select_related("run").first()
    if result is None:
        return JsonResponse({"ok": False, "error": "Результат не найден."}, status=404)
    return JsonResponse({
        "ok": True,
        "result": {
            "id": result.pk,
            "run_id": result.run.run_id,
            "case_ref": result.case_ref,
            "case_name": result.case_name,
            "group_name": result.group_name,
            "model_title": result.model_title,
            "status": result.status,
            "error_message": result.error_message,
            "expected_codes": result.expected_codes,
            "parsed_codes": result.parsed_codes,
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
            "message": result.message,
            "raw_response": result.raw_response,
        },
    })


def admin_prompt_lab_cancel_view(request):
    if not can_access_prompt_lab(request):
        return _deny()
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    run_id = (_json_body(request).get("run_id") or "").strip()
    if not run_id:
        return JsonResponse({"ok": False, "error": "Не указан run_id."}, status=400)
    if not cancel_prompt_lab_run(run_id, user=request.user):
        return JsonResponse(
            {"ok": False, "error": "Прогон не найден или уже завершён."}, status=404,
        )
    return JsonResponse({"ok": True})


def admin_prompt_lab_export_view(request):
    if not can_access_prompt_lab(request):
        return _deny()
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    run_id = (request.GET.get("run_id") or "").strip()
    run = PromptLabRun.objects.filter(run_id=run_id).first()
    if run is None:
        return JsonResponse({"ok": False, "error": "Прогон не найден."}, status=404)
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow([
        "Модель", "Группа", "Кейс", "Ожидаемые коды", "Найденные коды",
        "Совпало", "Пропущено", "Лишние", "Точное совпадение",
        "Precision", "Recall", "F1", "Статус", "Ошибка",
        "Длительность, с", "Токены", "Токены промпта",
    ])
    for result in run.results.order_by("group_name", "case_ref", "model_key"):
        writer.writerow([
            result.model_title or result.model_key,
            result.group_name,
            result.case_name or result.case_ref,
            ", ".join(str(code) for code in result.expected_codes),
            ", ".join(str(code) for code in result.parsed_codes),
            ", ".join(str(code) for code in result.matched_codes),
            ", ".join(str(code) for code in result.missed_codes),
            ", ".join(str(code) for code in result.extra_codes),
            "да" if result.exact_match else "нет",
            "" if result.precision is None else f"{result.precision:.3f}",
            "" if result.recall is None else f"{result.recall:.3f}",
            "" if result.f1 is None else f"{result.f1:.3f}",
            result.status,
            result.error_message,
            f"{result.duration_seconds:.2f}",
            result.tokens,
            result.tokens_prompt,
        ])
    response = HttpResponse(
        "﻿" + buffer.getvalue(), content_type="text/csv; charset=utf-8",
    )
    response["Content-Disposition"] = f'attachment; filename="prompt-lab-{run.run_id}.csv"'
    return response
