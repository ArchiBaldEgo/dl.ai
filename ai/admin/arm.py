"""ARM (AI Run Manager) — admin views для запуска и мониторинга тестирования моделей.

Содержит views для:
- Find-error: запуск одной модели на задачу+код, проверка через DL.
- Batch solve: запуск набора задач × набор моделей с проверкой через DL API.
- Статус прогонов (polling для фронтенда).
"""

import re

from .site import ai_admin_site
from django.core.cache import cache
from django.http import HttpResponse, HttpResponseForbidden, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse

from ..arm_runner import cancel_arm_run, get_arm_run_snapshot, start_arm_sequential_run, start_batch_solve_run
from ..i18n import get_language_instruction, get_localized_name
from ..model_health import (
    get_arm_solve_model_options,
    get_available_model_options,
    get_health_window_date,
)
from ..models import AIModelTestResult, ProgrammingLanguage, Prompt, SharedPrompt, Task, Topic
from ..http_utils import resolve_dl_session_id
from ..querysets import prompt_queryset_for_user
from ..serializers import programming_language as serialize_programming_language, prompt as serialize_prompt, topic as serialize_topic
from ..services.task_registry import EXTENSION_TO_LANG, SOLVE_EXTENSION_CHOICES, extension_to_language_ids
from ..constants import AI_CACHE_KEY_PREFIX
from .permissions import can_access_arm

# TTL кэша дерева задач курса в Redis (см. admin_arm_solve_load_tree_view).
_DL_TREE_CACHE_TTL = 30 * 60


def _resolve_session_id(request):
    """Resolve the caller's DL session id (DLSID flow), mirroring get_task_info_view."""
    session_id = request.session.get("external_session_id", "").strip()
    if not session_id:
        session_id = resolve_dl_session_id(request)
    return session_id


def _resolve_active_course_id_for_session(session_id):
    """Вернуть активный курс пользователя из DL сессии (по sessionId).

    Вызывает get-user-info с DLSID. Если courseID > 0 — возвращает его.
    Если courseID = 0 — пытается активировать последний известный курс
    (из последнего batch-прогона или дефолт 1450) через ensure_course_session,
    затем снова запрашивает get-user-info.

    Возвращает (course_id, error_message). course_id=None если курс не
    удалось определить. Используется как ARM-страницей, так и send_solution_view
    (единая логика резолва courseId для REST send-solution).
    """
    from ..external_auth import fetch_external_user_info
    from ..dl_api_client import ensure_course_session
    from ..models import AIModelTestRun

    if not session_id:
        return None, "Нет DLSID — требуется авторизация на dl.gsu.by."

    try:
        info = fetch_external_user_info(session_id)
    except Exception as exc:
        return None, f"Не удалось получить информацию о пользователе: {exc}"

    course_id = info.get("courseID") or 0
    if course_id:
        return course_id, ""

    # courseID=0 — попробуем активировать последний известный курс.
    # Берём course_id из последнего успешного batch-прогона любого пользователя
    # (дерево задач одного курса стабильно), или дефолт 1450.
    last_run = (
        AIModelTestRun.objects.filter(run_type="batch")
        .exclude(results__task__node_id__isnull=True)
        .order_by("-id")
        .first()
    )
    fallback_course_id = 1450
    if last_run and last_run.results.exists():
        first_result = last_run.results.first()
        if first_result.task and first_result.task.node_id:
            # Активируем курс по первой задаче из последнего прогона.
            # course_id не хранится в БД, но 1450 — единственный курс в DL.
            pass

    # Пытаемся активировать курс 1450 (fallback) через ensure_course_session.
    # Нужен node_id — берём из последнего прогона или дефолт 2606747.
    fallback_node_id = 2606747
    if last_run and last_run.results.exists():
        first_result = last_run.results.first()
        if first_result.task and first_result.task.node_id:
            fallback_node_id = first_result.task.node_id

    ensure_course_session(session_id, fallback_course_id, fallback_node_id)

    # Снова проверяем courseID.
    try:
        info2 = fetch_external_user_info(session_id)
        course_id = info2.get("courseID") or 0
        if course_id:
            return course_id, ""
    except Exception:
        pass

    return fallback_course_id, ""


def _resolve_active_course_id(request):
    """Тонкая обёртка над _resolve_active_course_id_for_session для HTTP-запроса."""
    return _resolve_active_course_id_for_session(_resolve_session_id(request))


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
        # Снимок формы запуска — для восстановления состояния формы при
        # возврате на страницу прогона (?run_id=). См. AIModelTestRun.run_params.
        "run_params": {
            "model_keys": list(selected_models),
            "interface_language": form_state["selected_language_ui"],
            "programming_language": str(form_state["selected_prog_lng"] or ""),
            "topic": str(form_state["selected_topic"] or ""),
            "prompt": str(form_state["selected_prompt"] or ""),
            "task_text": task_text,
            "code_text": code_text,
        },
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
        run_params=run_payload.get("run_params") or {},
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
        for p in prompt_queryset_for_user(
            Prompt.objects.select_related("topic"), request.user
        ).order_by("prompt_name", "id")
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
            # Восстановление формы: перезаполняем поля тем, что пользователь
            # отправлял при запуске (снимок в AIModelTestRun.run_params), чтобы
            # возврат на страницу прогона не требовал ввода заново.
            rp = active_run_snapshot.get("run_params") or {}
            if rp:
                selected_models = rp.get("model_keys") or []
                selected_language_ui = rp.get("interface_language") or "Русский"
                selected_prog_lng = str(rp.get("programming_language") or "")
                selected_topic = str(rp.get("topic") or "")
                selected_prompt = str(rp.get("prompt") or "")
                task_text = rp.get("task_text") or ""
                code_text = rp.get("code_text") or ""
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
# Batch-solve ARM: load tasks from DL tree, send each model the statement,
# test the code via DL (send-solution / get-solution-result).
# ---------------------------------------------------------------------------

def _arm_solve_prompt_options(user, file_extension=None):
    """Опции препромтов для /arm/solve/: только topic-bound ``Prompt`` (общие
    ``SharedPrompt`` на solve не предлагаются).

    Список зависит от выбранного расширения: промпт привязан к теме (``Prompt.topic``),
    тема — к языку программирования, а язык → расширение через ``_guess_extension``.
    При ``file_extension`` отдаются только ``Prompt`` тем, чей язык маппится в это
    расширение (``extension_to_language_ids``). Без ``file_extension`` — все
    доступные по ACL промпты (для начального рендера; реальный выбор на клиенте
    грузится по расширению через ``/solve/prompts/``).

    ACL — ``prompt_queryset_for_user`` (staff/superuser — все, иначе owner/editor).
    Возвращает список ``{id, name}``, отсортированный по имени.
    """
    prompts_qs = prompt_queryset_for_user(Prompt.objects.select_related("topic"), user)
    if file_extension:
        lang_ids = extension_to_language_ids(file_extension)
        prompts_qs = prompts_qs.filter(topic__programming_language_id__in=lang_ids)

    options = []
    for p in prompts_qs.order_by("prompt_name", "id"):
        topic_name = get_localized_name(p.topic, "Русский", "topic_name") if p.topic else ""
        label = f"[Тема] {topic_name} — {p.prompt_name}" if topic_name else f"[Тема] {p.prompt_name}"
        options.append({"id": str(p.id), "name": label})

    options.sort(key=lambda opt: opt["name"])
    return options


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

    # Определяем активный курс пользователя автоматически (без ручного ввода).
    active_course_id, course_error = _resolve_active_course_id(request)
    if course_error and not error_message:
        error_message = course_error

    from ..http_utils import safe_relative_url
    arm_back_url = safe_relative_url(request.session.get("ai_testpanel_back_url"), "/")

    prompt_options = []  # грузится клиентом по выбранному расширению (/solve/prompts/)

    context = {
        **ai_admin_site.each_context(request),
        "title": "ARM: Пакетное решение",
        "health_window_date": get_health_window_date().strftime("%d.%m.%Y"),
        "arm_back_url": arm_back_url,
        "model_options": get_arm_solve_model_options(),
        "prompt_options": prompt_options,
        "extension_options": [{"value": ext, "label": f"{ext} — {name}"}
                             for ext, name in SOLVE_EXTENSION_CHOICES],
        "arm_solve_tree_url": "/ai/admin/arm/solve/load-tree/",
        "arm_solve_prompts_url": "/ai/admin/arm/solve/prompts/",
        "results": results,
        "report": report,
        "error_message": error_message,
        "arm_solve_start_url": "/ai/admin/arm/solve/start/",
        "arm_solve_status_url": "/ai/admin/arm/solve/status/",
        "active_run_id": active_run_id,
        "active_run_snapshot": active_run_snapshot or {},
        "active_course_id": active_course_id or 0,
    }
    return TemplateResponse(request, "admin/ai/arm_solve.html", context)


def admin_arm_solve_load_tree_view(request):
    """Load tasks from a DL course tree (nested, not flattened).

    Accepts course_id (required). Calls get-course-node to find tasksRootId,
    then get-node-tree to fetch the full nested tree. Returns the tree as-is
    (folders + tasks), enriching only leaf task nodes with get-task-info
    (statement, taskId) — best-effort, failures are skipped silently.
    """
    if not can_access_arm(request):
        return HttpResponseForbidden("Access denied")

    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    import json as _json
    # Accept both JSON body and form-urlencoded data.
    body = {}
    try:
        body = _json.loads(request.body or b"{}")
    except (ValueError, _json.JSONDecodeError):
        body = {}

    course_id = body.get("course_id") or request.POST.get("course_id")
    try:
        course_id = int(course_id) if course_id else None
    except (ValueError, TypeError):
        course_id = None

    if course_id is None:
        return JsonResponse({"ok": False, "message": "Укажите course_id"}, status=400)

    # 0 = пусто: дерево не грузим, кэш не трогаем (соглашение /arm/solve/).
    if course_id == 0:
        return JsonResponse({"ok": True, "tree": [], "task_count": 0})

    # Дерево курса стабильно — кэшируем в Redis по course_id, чтобы не бить в DL
    # API при каждом открытии. Переключение course_id → другой ключ (старый
    # остаётся до TTL); инвалидация — по TTL. DLSID нужен только для промаха.
    tree_cache_key = f"{AI_CACHE_KEY_PREFIX}:dl_tree:{course_id}"
    cached = cache.get(tree_cache_key)
    if cached:
        return JsonResponse({
            "ok": True,
            "tree": cached["tree"],
            "task_count": cached["task_count"],
        })

    session_id = _resolve_session_id(request)
    if not session_id:
        return JsonResponse(
            {"ok": False, "message": "Нет DLSID — требуется авторизация на dl.gsu.by."},
            status=400,
        )

    from ..dl_api_client import (
        DLApiError,
        fetch_course_nodes,
        fetch_node_tree,
    )

    def _enrich_node(node):
        """Recursively mark task leaves. Statements are NOT fetched here —
        loading 277 get-task-info requests serially takes minutes and times
        out. The statement/taskId are fetched on-demand in ensure_task when
        the batch run starts. Here we only set placeholder fields so the UI
        can render badges without a statement.
        """
        if not isinstance(node, dict):
            return node
        is_folder = node.get("isFolder", False)
        if not is_folder:
            node["statement"] = ""
            node["task_id"] = 0
            node["has_statement"] = False
        else:
            node["statement"] = ""
            node["task_id"] = 0
            node["has_statement"] = False
        children = node.get("children")
        if children:
            node["children"] = [_enrich_node(c) for c in children]
        return node

    def _count_tasks(nodes):
        total = 0
        for n in nodes:
            if not isinstance(n, dict):
                continue
            if not n.get("isFolder", False):
                total += 1
            children = n.get("children")
            if children:
                total += _count_tasks(children)
        return total

    try:
        # Get tasksRootId from course.
        course_resp = fetch_course_nodes(session_id, course_id)
        root_node_id = course_resp.get("tasksRootId")
        if not root_node_id:
            return JsonResponse(
                {"ok": False, "message": "Не удалось получить tasksRootId для курса."},
                status=400,
            )

        # Fetch the full nested tree.
        tree_resp = fetch_node_tree(session_id, root_node_id, course_id=course_id)
        tree = tree_resp.get("tree", [])

        if not tree:
            return JsonResponse(
                {"ok": False, "message": "Дерево пусто."},
                status=400,
            )

        # Enrich task leaves with get-task-info (best-effort).
        enriched_tree = [_enrich_node(n) for n in tree]
        task_count = _count_tasks(enriched_tree)

        # Кэшируем только непустое дерево (пустое оставляем без кэша, чтобы
        # повторный запрос мог пере-проверить — вдруг курс обновился).
        if enriched_tree:
            cache.set(tree_cache_key, {"tree": enriched_tree, "task_count": task_count},
                      timeout=_DL_TREE_CACHE_TTL)

        return JsonResponse({
            "ok": True,
            "tree": enriched_tree,
            "task_count": task_count,
        })

    except DLApiError as exc:
        return JsonResponse({"ok": False, "message": f"Ошибка DL API: {exc}"}, status=400)
    except Exception as exc:
        return JsonResponse({"ok": False, "message": f"Ошибка: {exc}"}, status=500)


def admin_arm_solve_prompts_view(request):
    """Return prompt options for solve filtered by the selected file extension.

    Общие ``SharedPrompt`` на solve не предлагаются — только topic-bound ``Prompt``,
    отфильтрованные по языку программирования, соответствующему расширению
    (``extension_to_language_ids``). ``file_extension`` передаётся в GET.
    """
    if not can_access_arm(request):
        return HttpResponseForbidden("Access denied")

    file_extension = (request.GET.get("file_extension") or "").strip()
    prompt_options = _arm_solve_prompt_options(request.user, file_extension or None)
    return JsonResponse({"ok": True, "prompt_options": prompt_options})


def admin_arm_solve_start_view(request):
    if not can_access_arm(request):
        return HttpResponseForbidden("Access denied")

    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    import json as _json
    body = {}
    try:
        body = _json.loads(request.body or b"{}")
    except (ValueError, _json.JSONDecodeError):
        # Fall back to form data
        body = {
            "node_ids": request.POST.getlist("node_ids"),
            "models": request.POST.getlist("models"),
            "interface_language": request.POST.get("interface_language", "Русский"),
            "dl_test": request.POST.get("dl_test") == "1",
            "prompt_id": request.POST.get("prompt_id", "").strip() or None,
            "file_extension": request.POST.get("file_extension", "").strip(),
        }

    node_ids = body.get("node_ids") or request.POST.getlist("node_ids")
    model_keys = body.get("models") or request.POST.getlist("models")
    ui_language = body.get("interface_language", "Русский")
    dl_test = body.get("dl_test", False)
    if isinstance(dl_test, str):
        dl_test = dl_test == "1"
    prompt_id = body.get("prompt_id") or None
    # Расширение для DL-тестирования, выбранное пользователем (перекрывает
    # авто-определение задачи). Обязательно: на solve расширение выбирается
    # перед препромтом, «По умолчанию (из задачи)» убрано.
    file_extension = (body.get("file_extension") or request.POST.get("file_extension") or "").strip()
    if not file_extension:
        return JsonResponse(
            {"ok": False, "message": "Не выбрано расширение файла для тестирования."},
            status=400,
        )
    # Название языка для препромпта под выбранным расширением.
    prog_lang_name = EXTENSION_TO_LANG.get(file_extension, "")
    # ID языка программирования для журнала запросов (по выбранному расширению).
    prog_lang_ids = extension_to_language_ids(file_extension) if file_extension else set()
    prog_lang_id = next(iter(prog_lang_ids), None) if prog_lang_ids else None
    # Название и тема препромта для журнала запросов.
    prompt_name = ""
    topic_id_log = None
    topic_name_log = ""
    prompt_obj = None
    if prompt_id and not str(prompt_id).startswith("shared_"):
        try:
            prompt_obj = Prompt.objects.select_related("topic").get(id=int(prompt_id))
            prompt_name = prompt_obj.prompt_name or ""
            if prompt_obj.topic:
                topic_id_log = prompt_obj.topic_id
                topic_name_log = prompt_obj.topic.topic_name or ""
        except (Prompt.DoesNotExist, ValueError):
            pass
    # Course ID: берём из активного курса пользователя (не из запроса).
    # Фронтенд больше не отправляет course_id — он определяется автоматически.
    course_id_from_body = body.get("course_id") or request.POST.get("course_id")
    try:
        course_id = int(course_id_from_body) if course_id_from_body else None
    except (ValueError, TypeError):
        course_id = None
    if not course_id:
        # Определяем активный курс автоматически.
        course_id, course_err = _resolve_active_course_id(request)
        if course_err:
            return JsonResponse({"ok": False, "message": course_err}, status=400)

    session_id = _resolve_session_id(request)
    if not session_id:
        return JsonResponse(
            {"ok": False, "message": "Нет DLSID — требуется авторизация на dl.gsu.by."},
            status=400,
        )

    # Normalize node_ids to ints.
    node_id_ints = []
    for raw in node_ids:
        try:
            node_id_ints.append(int(raw))
        except (ValueError, TypeError):
            continue

    if not node_id_ints:
        return JsonResponse(
            {"ok": False, "message": "Не выбрано ни одной задачи из дерева DL."},
            status=400,
        )

    # Препромт обязателен; общие (shared_*) на solve не принимаются.
    if not prompt_id or str(prompt_id).startswith("shared_"):
        return JsonResponse(
            {"ok": False, "message": "Не выбран препромт (выберите расширение, затем препромт)."},
            status=400,
        )

    run_id, start_error = start_batch_solve_run(
        node_id_ints,
        model_keys,
        request.user.id,
        session_id,
        ui_language=ui_language,
        dl_test=dl_test,
        prompt_id=prompt_id,
        course_id=course_id,
        solve_file_extension=file_extension,
        solve_prog_lang_name=prog_lang_name,
        programming_language_id=prog_lang_id,
        programming_language_name=prog_lang_name,
        prompt_name=prompt_name,
        topic_id=topic_id_log,
        topic_name=topic_name_log,
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


def admin_arm_solve_cancel_view(request):
    """Cancel a running batch-solve ARM job."""
    if not can_access_arm(request):
        return HttpResponseForbidden("Access denied")

    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    import json as _json
    body = {}
    try:
        body = _json.loads(request.body or b"{}")
    except (ValueError, _json.JSONDecodeError):
        body = {}

    run_id = body.get("run_id") or request.POST.get("run_id", "")
    run_id = run_id.strip()
    if not run_id:
        return JsonResponse({"ok": False, "message": "run_id is required"}, status=400)

    found = cancel_arm_run(run_id)
    if not found:
        return JsonResponse(
            {"ok": False, "message": "Процесс не найден или уже завершен"},
            status=404,
        )

    return JsonResponse({"ok": True, "message": "Прерывание запрошено"})


def admin_arm_solve_result_download_view(request, result_id):
    """Скачать извлечённый код модели как файл программы.

    Отдаёт ``AIModelTestResult.code`` как ``text/plain`` во вложении. Имя файла —
    ``task_<node_id><file_extension>`` (расширение из снимка, с ведущей точкой).
    Файлы на диске не хранятся — содержимое берётся прямо из БД.
    """
    if not can_access_arm(request):
        return HttpResponseForbidden("Access denied")

    result = get_object_or_404(AIModelTestResult, pk=result_id)
    code = result.code or ""
    ext = (result.file_extension_snapshot or "").strip()
    if ext and not ext.startswith("."):
        ext = f".{ext}"
    # Sanitize ext: допускаем только ведущую точку + буквы/цифры — иначе символы
    # вроде " \r\n могли бы инъецировать в Content-Disposition. node_id — int,
    # безопасен; filename собирается из проверенных частей.
    ext = re.sub(r"[^\w.]", "", ext)
    if ext and not ext.startswith("."):
        ext = f".{ext}"
    node_id = ""
    if result.task_id and result.task and result.task.node_id:
        node_id = str(result.task.node_id)
    filename = f"task_{node_id}{ext}" if node_id else f"task_result_{result_id}{ext}"

    response = HttpResponse(code, content_type="text/plain; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response
