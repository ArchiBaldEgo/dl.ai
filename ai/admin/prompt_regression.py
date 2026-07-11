"""Prompt regression tests admin views (page / start / status trio).

Mirrors the ARM admin trio in ai/admin/arm.py: a page view that loads a run
snapshot from ?run_id= and renders the form + report, a POST start endpoint that
launches a run, and a GET status endpoint polled for progress.
"""

from .site import ai_admin_site
from django.http import HttpResponseForbidden, HttpResponseNotAllowed, JsonResponse
from django.template.response import TemplateResponse

from ..model_health import get_available_model_options, get_health_window_date
from ..models import ProgrammingLanguage, Prompt, PromptTestCase, Topic
from ..prompt_test_runner import get_prompt_test_run_snapshot, start_prompt_test_run
from ..serializers import prompt as serialize_prompt
from .permissions import can_access_prompt_regression


def _serialize_test_case(case):
    return {
        "id": case.id,
        "name": case.name,
        "mode": case.mode,
        "comparator": case.comparator,
        "topic_name": case.topic.topic_name if case.topic else "",
        "prog_lang_name": case.programming_language.language_name if case.programming_language else "",
        "has_expected": bool(case.expected_text),
    }


def _serialize_prompt_option(prompt, ui_language="Русский"):
    # Lightweight version of serializers.prompt: only what the run form needs.
    return {
        "id": prompt.id,
        "name": serialize_prompt(prompt, ui_language).get("name") or prompt.prompt_name or f"Prompt #{prompt.id}",
        "topic_id": prompt.topic_id,
    }


def admin_prompt_regression_view(request):
    if not can_access_prompt_regression(request):
        return HttpResponseForbidden("Access denied")

    active_run_id = (request.GET.get("run_id") or "").strip()
    active_run_snapshot = None
    results = []
    report = None
    error_message = ""

    if active_run_id:
        active_run_snapshot = get_prompt_test_run_snapshot(active_run_id)
        if active_run_snapshot:
            results = active_run_snapshot.get("results") or []
            report = active_run_snapshot.get("report")
            if active_run_snapshot.get("status") == "failed":
                error_message = active_run_snapshot.get("error_message") or "Регрессионный прогон завершился с ошибкой"
        else:
            error_message = "Прогон не найден или уже завершен"

    test_cases = [
        _serialize_test_case(c)
        for c in PromptTestCase.objects.filter(active=True).select_related("topic", "programming_language").order_by("id")
    ]
    prompt_options = [
        _serialize_prompt_option(p)
        for p in Prompt.objects.select_related("topic").order_by("prompt_name", "id")
    ]

    context = {
        **ai_admin_site.each_context(request),
        "title": "Регрессионные тесты промптов",
        "health_window_date": get_health_window_date().strftime("%d.%m.%Y"),
        "test_cases": test_cases,
        "prompt_options": prompt_options,
        "model_options": get_available_model_options(),
        "results": results,
        "report": report,
        "error_message": error_message,
        "prompt_regression_start_url": "/ai/admin/prompt-regression/start/",
        "prompt_regression_status_url": "/ai/admin/prompt-regression/status/",
        "active_run_id": active_run_id,
        "active_run_snapshot": active_run_snapshot or {},
    }
    return TemplateResponse(request, "admin/ai/prompt_regression.html", context)


def admin_prompt_regression_start_view(request):
    if not can_access_prompt_regression(request):
        return HttpResponseForbidden("Access denied")

    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    model_keys = request.POST.getlist("models")
    if not model_keys:
        return JsonResponse({"ok": False, "message": "Выберите модель"}, status=400)
    model_key = model_keys[0]

    prompt_id = (request.POST.get("prompt") or "").strip() or None
    ui_language = request.POST.get("interface_language", "Русский") or "Русский"

    case_ids = []
    for raw in request.POST.getlist("cases"):
        try:
            case_ids.append(int(raw))
        except (ValueError, TypeError):
            continue

    run_id, start_error = start_prompt_test_run(
        case_ids,
        model_key,
        request.user.id,
        prompt_id=prompt_id,
        ui_language=ui_language,
    )
    if not run_id:
        return JsonResponse(
            {"ok": False, "message": start_error or "Не удалось запустить регрессионный прогон"},
            status=400,
        )

    return JsonResponse({"ok": True, "run_id": run_id, "run": get_prompt_test_run_snapshot(run_id)})


def admin_prompt_regression_status_view(request):
    if not can_access_prompt_regression(request):
        return HttpResponseForbidden("Access denied")

    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])

    run_id = (request.GET.get("run_id") or "").strip()
    if not run_id:
        return JsonResponse({"ok": False, "message": "run_id is required"}, status=400)

    run_snapshot = get_prompt_test_run_snapshot(run_id)
    if not run_snapshot:
        return JsonResponse(
            {"ok": False, "message": "Прогон не найден или уже завершен"},
            status=404,
        )

    return JsonResponse({"ok": True, "run": run_snapshot})