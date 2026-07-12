"""ARM (multi-model check) admin views."""

from .site import ai_admin_site
from django.http import HttpResponseForbidden, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import redirect
from django.template.response import TemplateResponse

from ..arm_runner import get_arm_run_snapshot, start_arm_sequential_run, start_batch_solve_run
from ..i18n import get_language_instruction, get_localized_name
from ..model_health import (
    get_available_model_options,
    get_health_window_date,
)
from ..models import ProgrammingLanguage, Prompt, SharedPrompt, Task, Topic
from ..serializers import programming_language as serialize_programming_language, prompt as serialize_prompt, topic as serialize_topic
from .permissions import can_access_arm


def _resolve_session_id(request):
    """Resolve the caller's DL session id (DLSID flow), mirroring get_task_info_view."""
    import os

    session_id = request.session.get("external_session_id", "").strip()
    if not session_id:
        cookie_name = os.getenv("EXTERNAL_SESSION_COOKIE_NAME", "DLSID")
        session_id = request.COOKIES.get(cookie_name, "").strip()
    return session_id


def _build_find_error_message(task_text, code_text, prog_lang_name, topic_name, prompt_text, ui_language):
    try:
        default_prompt = SharedPrompt.objects.get(mode="find_error")
        message = default_prompt.get_effective_text(
            ui_language, prog_lang_name, topic_name, task_text, code_text
        )
    except SharedPrompt.DoesNotExist:
        message = (
            "У меня есть задача по программированию, я написал для нее код на языке "
            f"{prog_lang_name}, код не работает, найди пожалуйста ошибку. "
            f"Задача: {task_text}. Код: {code_text}."
        )
    if prompt_text:
        message += f"\n\nПрепромпт: {prompt_text}"
    message += get_language_instruction(ui_language)
    return message


def _collect_arm_form_state(request):
    return {
        "selected_models": request.POST.getlist("models"),
        "selected_language_ui": request.POST.get("interface_language", "Русский"),
        "selected_prog_lng": request.POST.get("programming_language", ""),
        "selected_topic": request.POST.get("topic", ""),
        "selected_prompt": request.POST.get("prompt", ""),
        "task_text": (request.POST.get("task_text") or "").strip(),
        "code_text": (request.POST.get("code_text") or "").strip(),
    }


def _prepare_arm_run_payload(form_state, user=None):
    selected_models = form_state["selected_models"]
    task_text = form_state["task_text"]
    code_text = form_state["code_text"]

    if not selected_models:
        return None, "Выберите хотя бы одну модель"

    if not task_text and not code_text:
        return None, "Заполните условие задачи или код"

    prog_lng_name = ProgrammingLanguage.objects.filter(
        id=form_state["selected_prog_lng"]
    ).values_list("language_name", flat=True).first() or "Python"

    topic = None
    if form_state["selected_topic"]:
        topic = Topic.objects.filter(id=form_state["selected_topic"]).first()

    prompt_obj = (
        Prompt.objects.filter(id=form_state["selected_prompt"])
        .select_related("shared_prompt")
        .first()
    )
    topic_name_localized = (
        get_localized_name(topic, form_state["selected_language_ui"], "topic_name")
        if topic else ""
    )
    prompt_text = (
        prompt_obj.get_effective_text(
            form_state["selected_language_ui"], prog_lng_name, topic_name_localized
        )
        if prompt_obj else ""
    )

    message = _build_find_error_message(
        task_text=task_text,
        code_text=code_text,
        prog_lang_name=prog_lng_name,
        topic_name=topic_name_localized,
        prompt_text=prompt_text,
        ui_language=form_state["selected_language_ui"],
    )

    return {
        "selected_models": selected_models,
        "message": message,
        "programming_language_id": form_state["selected_prog_lng"] or None,
        "programming_language_name": prog_lng_name,
        "topic_id": form_state["selected_topic"] or None,
        "topic_name": topic.topic_name if topic else "",
        "topic_name_localized": get_localized_name(topic, form_state["selected_language_ui"], "topic_name") if topic else "",
        "prompt_id": form_state["selected_prompt"] or None,
        "prompt_name": prompt_obj.prompt_name if prompt_obj else "",
        "prompt_name_localized": get_localized_name(prompt_obj, form_state["selected_language_ui"], "prompt_name") if prompt_obj else "",
    }, ""


def _start_arm_from_payload(run_payload, user_id):
    return start_arm_sequential_run(
        run_payload["message"],
        run_payload["selected_models"],
        user_id,
        programming_language_id=run_payload.get("programming_language_id"),
        programming_language_name=run_payload.get("programming_language_name"),
        topic_id=run_payload.get("topic_id"),
        topic_name=run_payload.get("topic_name_localized") or run_payload.get("topic_name"),
        prompt_id=run_payload.get("prompt_id"),
        prompt_name=run_payload.get("prompt_name_localized") or run_payload.get("prompt_name"),
    )


def admin_arm_find_error_view(request):
    if not can_access_arm(request):
        return HttpResponseForbidden("Access denied")

    selected_language_ui = "Русский"
    languages = [
        serialize_programming_language(lang)
        for lang in ProgrammingLanguage.objects.all()
    ]
    topics = [
        serialize_topic(t, selected_language_ui)
        for t in Topic.objects.all()
    ]
    prompts = [
        serialize_prompt(p, selected_language_ui)
        for p in Prompt.objects.select_related("topic").order_by("prompt_name", "id")
    ]

    selected_models = []
    selected_prog_lng = ""
    selected_topic = ""
    selected_prompt = ""
    task_text = ""
    code_text = ""
    results = []
    report = None
    error_message = ""
    active_run_id = (request.GET.get("run_id") or "").strip()
    active_run_snapshot = None

    if request.method == "POST":
        form_state = _collect_arm_form_state(request)
        selected_models = form_state["selected_models"]
        selected_language_ui = form_state["selected_language_ui"]
        selected_prog_lng = form_state["selected_prog_lng"]
        selected_topic = form_state["selected_topic"]
        selected_prompt = form_state["selected_prompt"]
        task_text = form_state["task_text"]
        code_text = form_state["code_text"]

        run_payload, error_message = _prepare_arm_run_payload(form_state, request.user)
        if not error_message:
            run_id, start_error = _start_arm_from_payload(run_payload, request.user.id)
            if run_id:
                return redirect(f"/ai/admin/arm/find-error/?run_id={run_id}")
            error_message = start_error or "Не удалось запустить ARM процесс"

    if active_run_id:
        active_run_snapshot = get_arm_run_snapshot(active_run_id)
        if active_run_snapshot:
            results = active_run_snapshot.get("results") or []
            report = active_run_snapshot.get("report")
            if active_run_snapshot.get("status") == "failed":
                error_message = active_run_snapshot.get("error_message") or "ARM процесс завершился с ошибкой"
        else:
            error_message = "ARM процесс не найден или уже завершен"

    from ..http_utils import safe_relative_url
    arm_back_url = safe_relative_url(request.session.get("ai_testpanel_back_url"), "/")
    context = {
        **ai_admin_site.each_context(request),
        "title": "ARM: В чем ошибка",
        "health_window_date": get_health_window_date().strftime("%d.%m.%Y"),
        "arm_back_url": arm_back_url,
        "languages": languages,
        "topics": topics,
        "prompts": prompts,
        "model_options": get_available_model_options(),
        "selected_models": selected_models,
        "selected_language_ui": selected_language_ui,
        "selected_prog_lng": selected_prog_lng,
        "selected_topic": selected_topic,
        "selected_prompt": selected_prompt,
        "task_text": task_text,
        "code_text": code_text,
        "results": results,
        "report": report,
        "error_message": error_message,
        "arm_find_error_start_url": "/ai/admin/arm/find-error/start/",
        "arm_find_error_status_url": "/ai/admin/arm/find-error/status/",
        "active_run_id": active_run_id,
        "active_run_snapshot": active_run_snapshot or {},
    }
    return TemplateResponse(request, "admin/ai/arm_find_error.html", context)


def admin_arm_find_error_start_view(request):
    if not can_access_arm(request):
        return HttpResponseForbidden("Access denied")

    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    form_state = _collect_arm_form_state(request)
    run_payload, error_message = _prepare_arm_run_payload(form_state, request.user)
    if error_message:
        return JsonResponse({"ok": False, "message": error_message}, status=400)

    run_id, start_error = _start_arm_from_payload(run_payload, request.user.id)
    if not run_id:
        return JsonResponse(
            {"ok": False, "message": start_error or "Не удалось запустить ARM процесс"},
            status=400,
        )

    return JsonResponse({"ok": True, "run_id": run_id, "run": get_arm_run_snapshot(run_id)})


def admin_arm_find_error_status_view(request):
    if not can_access_arm(request):
        return HttpResponseForbidden("Access denied")

    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])

    run_id = (request.GET.get("run_id") or "").strip()
    if not run_id:
        return JsonResponse({"ok": False, "message": "run_id is required"}, status=400)

    run_snapshot = get_arm_run_snapshot(run_id)
    if not run_snapshot:
        return JsonResponse(
            {"ok": False, "message": "ARM процесс не найден или уже завершен"},
            status=404,
        )

    return JsonResponse({"ok": True, "run": run_snapshot})


# ---------------------------------------------------------------------------
# Batch-solve ARM: send each available model the statement of every active
# Task, grade against the DL sample solution, report per-model / per-topic.
# ---------------------------------------------------------------------------

def admin_arm_solve_view(request):
    if not can_access_arm(request):
        return HttpResponseForbidden("Access denied")

    active_run_id = (request.GET.get("run_id") or "").strip()
    active_run_snapshot = None
    results = []
    report = None
    error_message = ""

    if active_run_id:
        active_run_snapshot = get_arm_run_snapshot(active_run_id)
        if active_run_snapshot:
            results = active_run_snapshot.get("results") or []
            report = active_run_snapshot.get("report")
            if active_run_snapshot.get("status") == "failed":
                error_message = active_run_snapshot.get("error_message") or "Batch solve завершился с ошибкой"
        else:
            error_message = "Процесс не найден или уже завершен"

    tasks = [
        {
            "id": t.id,
            "node_id": t.node_id,
            "task_id": t.task_id,
            "name": t.name,
            "topic_name": t.topic.topic_name if t.topic else "",
            "prog_lang_name": t.programming_language.language_name if t.programming_language else "",
            "file_extension": t.file_extension,
            "has_statement": bool(t.statement),
            "has_sample_inputs": bool(t.task_id and t.file_extension),
        }
        for t in Task.objects.filter(active=True).select_related("topic", "programming_language").order_by("-created_at")
    ]

    from ..http_utils import safe_relative_url
    from ..models import SharedPrompt, Prompt
    arm_back_url = safe_relative_url(request.session.get("ai_testpanel_back_url"), "/")

    context = {
        **ai_admin_site.each_context(request),
        "title": "ARM: Пакетное решение",
        "health_window_date": get_health_window_date().strftime("%d.%m.%Y"),
        "arm_back_url": arm_back_url,
        "tasks": tasks,
        "model_options": get_available_model_options(),
        "prompt_options": [],
        "arm_solve_prompts_url": "/ai/admin/arm/solve/prompts/",
        "results": results,
        "report": report,
        "error_message": error_message,
        "arm_solve_start_url": "/ai/admin/arm/solve/start/",
        "arm_solve_status_url": "/ai/admin/arm/solve/status/",
        "active_run_id": active_run_id,
        "active_run_snapshot": active_run_snapshot or {},
    }
    return TemplateResponse(request, "admin/ai/arm_solve.html", context)


def admin_arm_solve_add_task_view(request):
    """Add a DL task by node_id — fetch from DL and create/update in DB."""
    if not can_access_arm(request):
        return HttpResponseForbidden("Access denied")

    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    node_id_raw = request.POST.get("node_id", "").strip()
    try:
        node_id = int(node_id_raw)
    except (ValueError, TypeError):
        return JsonResponse({"ok": False, "message": "node_id должен быть числом"}, status=400)

    session_id = _resolve_session_id(request)
    if not session_id:
        return JsonResponse(
            {"ok": False, "message": "Нет DLSID — требуется авторизация на dl.gsu.by."},
            status=400,
        )

    from ..services.task_registry import ensure_task
    task = ensure_task(node_id, session_id=session_id)
    if task is None:
        return JsonResponse({"ok": False, "message": "Не удалось создать/найти задачу."}, status=500)

    return JsonResponse({
        "ok": True,
        "message": f"Задача #{task.node_id} «{task.name or '—'}» — {'создана' if not task.active else 'обновлена'}",
        "task": {
            "id": task.id,
            "node_id": task.node_id,
            "name": task.name,
            "task_id": task.task_id,
        },
    })


def admin_arm_solve_prompts_view(request):
    """Return filtered prompt options based on selected task IDs."""
    if not can_access_arm(request):
        return HttpResponseForbidden("Access denied")

    task_ids = request.GET.getlist("task_ids")
    task_id_ints = []
    for raw in task_ids:
        try:
            task_id_ints.append(int(raw))
        except (ValueError, TypeError):
            continue

    from ..models import SharedPrompt, Prompt, Task

    tasks = Task.objects.filter(pk__in=task_id_ints) if task_id_ints else []
    topic_ids = set(t.topic_id for t in tasks if t.topic_id)
    prog_lang_ids = set(t.programming_language_id for t in tasks if t.programming_language_id)

    # If multiple distinct topics or languages → only SharedPrompts (no topic-specific Prompts).
    multi_topic = len(topic_ids) > 1
    multi_lang = len(prog_lang_ids) > 1

    prompt_options = []

    # SharedPrompts: include if no language restriction OR language matches.
    for sp in SharedPrompt.objects.all().order_by("id"):
        restricted_langs = list(sp.programming_languages.values_list("id", flat=True))
        if not restricted_langs:
            # No restriction → always available.
            prompt_options.append({"id": f"shared_{sp.id}", "name": f"[Общий] {sp.prompt_name}"})
        elif not multi_lang and prog_lang_ids:
            # Single language → check if this prompt allows it.
            if prog_lang_ids.issubset(set(restricted_langs)):
                prompt_options.append({"id": f"shared_{sp.id}", "name": f"[Общий] {sp.prompt_name}"})

    # Prompts (topic-specific): only if single topic match.
    if not multi_topic and topic_ids:
        for p in Prompt.objects.filter(topic_id__in=topic_ids).order_by("id"):
            prompt_options.append({
                "id": str(p.id),
                "name": p.prompt_name or f"Prompt #{p.id}",
            })

    return JsonResponse({"ok": True, "prompt_options": prompt_options})


def admin_arm_solve_start_view(request):
    if not can_access_arm(request):
        return HttpResponseForbidden("Access denied")

    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    task_ids = request.POST.getlist("task_ids")
    model_keys = request.POST.getlist("models")
    ui_language = request.POST.get("interface_language", "Русский")
    dl_test = request.POST.get("dl_test") == "1"
    prompt_id = request.POST.get("prompt_id", "").strip() or None

    session_id = _resolve_session_id(request)
    if dl_test and not session_id:
        return JsonResponse(
            {"ok": False, "message": "Нет DLSID — требуется авторизация на dl.gsu.by для тестирования решений."},
            status=400,
        )
    if not session_id:
        return JsonResponse(
            {"ok": False, "message": "Нет DLSID — требуется авторизация на dl.gsu.by для получения образцовых решений."},
            status=400,
        )

    # Normalize to ints; empty list means "all active tasks".
    task_id_ints = []
    for raw in task_ids:
        try:
            task_id_ints.append(int(raw))
        except (ValueError, TypeError):
            continue

    run_id, start_error = start_batch_solve_run(
        task_id_ints or None,
        model_keys,
        request.user.id,
        session_id,
        ui_language=ui_language,
        dl_test=dl_test,
        prompt_id=prompt_id,
    )
    if not run_id:
        return JsonResponse(
            {"ok": False, "message": start_error or "Не удалось запустить batch solve"},
            status=400,
        )

    return JsonResponse({"ok": True, "run_id": run_id, "run": get_arm_run_snapshot(run_id)})


def admin_arm_solve_status_view(request):
    if not can_access_arm(request):
        return HttpResponseForbidden("Access denied")

    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])

    run_id = (request.GET.get("run_id") or "").strip()
    if not run_id:
        return JsonResponse({"ok": False, "message": "run_id is required"}, status=400)

    run_snapshot = get_arm_run_snapshot(run_id)
    if not run_snapshot:
        return JsonResponse(
            {"ok": False, "message": "Процесс не найден или уже завершен"},
            status=404,
        )

    return JsonResponse({"ok": True, "run": run_snapshot})
