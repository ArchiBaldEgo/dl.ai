import csv
from django.contrib import admin
from django.contrib.auth import authenticate, get_user_model, logout as auth_logout, login as auth_login
from django.contrib.admin.forms import AdminAuthenticationForm
from django import forms
from django.db.models import Q
from django.core.paginator import Paginator
from django.http import HttpResponse, HttpResponseForbidden, HttpResponseNotAllowed, JsonResponse
from django.middleware import csrf
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import path
from django.utils import timezone
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

from .arm_runner import get_arm_run_snapshot, start_arm_sequential_run
from .i18n import get_localized_name, get_localized_text
from .model_health import (
    get_available_model_options,
    get_model_status_rows,
    get_health_window_date,
    is_model_health_refresh_running,
    trigger_model_health_refresh_async,
)
from .models import AIRequestLog, ProgrammingLanguage, Topic, Prompt, SharedPrompt, AIAppSettings, ExternalDLAccount
from .auth_backends import (
    ADMIN_EXTERNAL_AUTH_BACKEND,
    ensure_prompt_developer_group,
    get_external_user_id_from_request,
)

MOSCOW_TZ = ZoneInfo("Europe/Moscow")
PROMPT_DEVELOPER_GROUP = "prompt_developer"
ADMIN_LOGOUT_COOKIE_NAME = "ai_admin_logged_out"
User = get_user_model()


def _safe_relative_url(candidate, fallback):
    value = (candidate or "").strip()
    if value.startswith("/") and not value.startswith("//"):
        return value
    return fallback


def _is_admin_logout_forced(request):
    return bool(request.COOKIES.get(ADMIN_LOGOUT_COOKIE_NAME))


def _is_prompt_developer_user(request):
    if not request.user.is_authenticated:
        return False
    return request.user.groups.filter(name=PROMPT_DEVELOPER_GROUP).exists()


def _is_staff_or_superuser(user):
    return bool(user and (user.is_superuser or user.is_staff))


def _can_access_arm(request):
    if not request.user.is_authenticated:
        return False
    if _is_staff_or_superuser(request.user):
        return True
    return _is_prompt_developer_user(request)


def _can_access_model_status(request):
    if not request.user.is_authenticated:
        return False
    return request.user.is_superuser or request.user.is_staff


def _can_access_prompt_admin(request):
    if not request.user.is_authenticated:
        return False
    if _is_staff_or_superuser(request.user):
        return True
    return _is_prompt_developer_user(request)


def _can_access_logs(request):
    return request.user.is_authenticated and _is_staff_or_superuser(request.user)


def _get_my_prompt_admin_url(request):
    return "/ai/admin/prompts/my/"


def _is_mine_only_request(request):
    if getattr(request, "_mine_only", False):
        return True
    value = (request.GET.get("mine") or "").strip().lower()
    return value in {"1", "true", "yes"}


def _prompt_queryset_for_user(queryset, user):
    if not user or not user.is_authenticated:
        return queryset.none()
    return queryset.filter(Q(owner=user) | Q(editors=user)).distinct()


def _user_matches_external_id(user, external_user_id):
    if not user or not user.is_authenticated or not user.is_active or not external_user_id:
        return False
    if user.username == external_user_id:
        return True
    return ExternalDLAccount.objects.filter(
        user=user,
        external_user_id=external_user_id,
    ).exists()


def _auto_login_from_external(request):
    """
    Проверяет, есть ли user_info от middleware (ExternalAuthMiddleware).
    Если есть — автоматически логинит пользователя без пароля.
    Возвращает True если удалось залогинить.
    """
    if _is_admin_logout_forced(request):
        return False

    external_user_id = get_external_user_id_from_request(request)
    if not external_user_id:
        return False

    user = authenticate(request, external_user_id=external_user_id)
    if not user:
        return False

    auth_login(request, user, backend=ADMIN_EXTERNAL_AUTH_BACKEND)
    csrf.rotate_token(request)
    request.session["admin_fresh_auth"] = True
    return True


def _is_admin_password_setup_path(request):
    return request.path.startswith("/ai/admin/set-password/")


def _is_admin_auth_service_path(request):
    return (
        request.path.startswith("/ai/admin/login/")
        or request.path.startswith("/ai/admin/logout/")
        or _is_admin_password_setup_path(request)
    )


def _external_admin_entry_response(request):
    if request.method != "GET" or _is_admin_auth_service_path(request):
        return None

    if _is_admin_logout_forced(request):
        if request.session.get("admin_manual_login"):
            return None
        next_path = quote(request.get_full_path(), safe="/?=&")
        return redirect(f"/ai/admin/login/?next={next_path}")

    external_user_id = get_external_user_id_from_request(request)
    if not external_user_id:
        return None

    current_user = getattr(request, "user", None)
    if _user_matches_external_id(current_user, external_user_id):
        ensure_prompt_developer_group(current_user)
        request.session["admin_fresh_auth"] = True
        return None

    if _auto_login_from_external(request):
        return None

    next_path = quote(request.get_full_path(), safe="/?=&")
    return redirect(f"/ai/admin/set-password/?next={next_path}")


class TesterOrStaffAdminAuthenticationForm(AdminAuthenticationForm):
    def clean(self):
        username = (self.data.get("username") or "").strip()
        if username and username.isdigit():
            account = ExternalDLAccount.objects.select_related("user").filter(
                external_user_id=username
            ).first()
            mapped_username = account.user.username if account and account.user_id else f"user_{username}"
            mutable_data = self.data.copy()
            mutable_data["username"] = mapped_username
            self.data = mutable_data
        return super().clean()

    def confirm_login_allowed(self, user):
        if not user.is_active:
            raise forms.ValidationError(
                self.error_messages["inactive"],
                code="inactive",
            )

        if (
            _is_staff_or_superuser(user)
            or user.groups.filter(name=PROMPT_DEVELOPER_GROUP).exists()
        ):
            # Check if user needs to set password first
            if (not user.has_usable_password()) and getattr(self, "request", None) is not None:
                request = self.request
                next_url = request.GET.get("next", "/ai/admin/")
                raise forms.ValidationError(
                    f"Please set your password first. <a href='/ai/admin/set-password/?next={next_url}'>Set password</a>",
                    code="set_password_required",
                )
            
            if getattr(self, "request", None) is not None:
                self.request.session["admin_fresh_auth"] = True
                self.request.session["admin_manual_login"] = True
            return

        raise forms.ValidationError(
            "Please enter the correct username and password for a staff or prompt developer account.",
            code="invalid_login",
        )


def _language_instruction(language_name):
    if language_name == "Русский":
        return ". Разговаривай со мной только по-русски"
    if language_name == "Français":
        return ". Communiquez avec moi uniquement en français"
    if language_name == "English":
        return ". Communicate with me only in English"
    return ""


def _build_find_error_message(task_text, code_text, prog_lang_name, prompt_text, ui_language):
    message = (
        "У меня есть задача по программированию, я написал для нее код на языке "
        f"{prog_lang_name}, код не работает, найди пожалуйста ошибку. "
        f"Задача: {task_text}. Код: {code_text}."
    )
    if prompt_text:
        message += f". Препромпт: {prompt_text}"

    message += _language_instruction(ui_language)
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
    prompt_text = (
        prompt_obj.get_effective_text(form_state["selected_language_ui"], prog_lng_name)
        if prompt_obj else ""
    )

    message = _build_find_error_message(
        task_text=task_text,
        code_text=code_text,
        prog_lang_name=prog_lng_name,
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


def admin_arm_find_error_view(request):
    if not _can_access_arm(request):
        return HttpResponseForbidden("Access denied")

    languages = list(ProgrammingLanguage.objects.all().values("id", "language_name"))
    topics = [
        {
            "id": t.id,
            "topic_name": t.topic_name,
            "name": get_localized_name(t, selected_language_ui, "topic_name"),
            "programming_language_id": t.programming_language_id,
        }
        for t in Topic.objects.all()
    ]
    prompts = [
        {
            "id": p.id,
            "prompt_name": p.prompt_name,
            "name": get_localized_name(p, selected_language_ui, "prompt_name"),
            "prompt_text": p.prompt_text,
            "effective_text": p.get_effective_text(selected_language_ui, ""),
            "topic_id": p.topic_id,
            "topic__programming_language": p.topic.programming_language_id if p.topic else None,
        }
        for p in Prompt.objects.select_related("topic").order_by("prompt_name", "id")
    ]

    selected_models = []
    selected_language_ui = "Русский"
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
            run_id, start_error = start_arm_sequential_run(
                run_payload["message"],
                run_payload["selected_models"],
                request.user.id,
                programming_language_id=run_payload.get("programming_language_id"),
                programming_language_name=run_payload.get("programming_language_name"),
                topic_id=run_payload.get("topic_id"),
                topic_name=run_payload.get("topic_name_localized") or run_payload.get("topic_name"),
                prompt_id=run_payload.get("prompt_id"),
                prompt_name=run_payload.get("prompt_name_localized") or run_payload.get("prompt_name"),
            )

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

    arm_back_url = _safe_relative_url(request.session.get("ai_testpanel_back_url"), "/")
    context = {
        **admin.site.each_context(request),
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
    if not _can_access_arm(request):
        return HttpResponseForbidden("Access denied")

    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    form_state = _collect_arm_form_state(request)
    run_payload, error_message = _prepare_arm_run_payload(form_state, request.user)
    if error_message:
        return JsonResponse({"ok": False, "message": error_message}, status=400)

    run_id, start_error = start_arm_sequential_run(
        run_payload["message"],
        run_payload["selected_models"],
        request.user.id,
        programming_language_id=run_payload.get("programming_language_id"),
        programming_language_name=run_payload.get("programming_language_name"),
        topic_id=run_payload.get("topic_id"),
        topic_name=run_payload.get("topic_name_localized") or run_payload.get("topic_name"),
        prompt_id=run_payload.get("prompt_id"),
        prompt_name=run_payload.get("prompt_name_localized") or run_payload.get("prompt_name"),
    )
    if not run_id:
        return JsonResponse(
            {
                "ok": False,
                "message": start_error or "Не удалось запустить ARM процесс",
            },
            status=400,
        )

    return JsonResponse(
        {
            "ok": True,
            "run_id": run_id,
            "run": get_arm_run_snapshot(run_id),
        }
    )


def admin_arm_find_error_status_view(request):
    if not _can_access_arm(request):
        return HttpResponseForbidden("Access denied")

    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])

    run_id = (request.GET.get("run_id") or "").strip()
    if not run_id:
        return JsonResponse({"ok": False, "message": "run_id is required"}, status=400)

    run_snapshot = get_arm_run_snapshot(run_id)
    if not run_snapshot:
        return JsonResponse(
            {
                "ok": False,
                "message": "ARM процесс не найден или уже завершен",
            },
            status=404,
        )

    return JsonResponse({"ok": True, "run": run_snapshot})


def admin_model_status_view(request):
    if not _can_access_model_status(request):
        return HttpResponseForbidden("Access denied")

    refresh_message = ""
    refresh_error = ""

    if request.method == "POST" and request.POST.get("action") == "refresh_models":
        try:
            if trigger_model_health_refresh_async():
                refresh_message = (
                    "Обновление моделей запущено в фоне. "
                    "Окно 04:00 МСК не изменяется."
                )
            else:
                refresh_message = "Обновление уже выполняется. Дождитесь завершения."
        except Exception as exc:
            refresh_error = f"Не удалось запустить обновление моделей: {exc}"

    context = {
        **admin.site.each_context(request),
        "title": "AI: Состояние моделей",
        "health_window_date": get_health_window_date().strftime("%d.%m.%Y"),
        "model_status_rows": get_model_status_rows(),
        "refresh_message": refresh_message,
        "refresh_error": refresh_error,
        "refresh_in_progress": is_model_health_refresh_running(),
        "arm_find_error_url": "/ai/admin/arm/find-error/",
        "arm_model_status_refresh_url": "/ai/admin/arm/models/refresh/",
        "arm_model_status_state_url": "/ai/admin/arm/models/state/",
    }
    return TemplateResponse(request, "admin/ai/model_status.html", context)


def _serialize_model_status_rows_for_api(rows):
    serialized = []

    for row in rows:
        checked_at = row.get("checked_at")
        checked_at_msk = ""
        if checked_at:
            checked_at_msk = timezone.localtime(checked_at, MOSCOW_TZ).strftime("%d.%m.%Y %H:%M:%S")

        window_date = row.get("window_date")

        serialized.append(
            {
                "key": row.get("key") or "",
                "title": row.get("title") or "",
                "is_active": bool(row.get("is_active")),
                "status_label": row.get("status_label") or "",
                "window_date": window_date.isoformat() if window_date else "",
                "checked_at_msk": checked_at_msk,
                "is_current_window": bool(row.get("is_current_window")),
            }
        )

    return serialized


def admin_model_status_state_view(request):
    if not _can_access_model_status(request):
        return HttpResponseForbidden("Access denied")

    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])

    rows = get_model_status_rows()
    return JsonResponse(
        {
            "ok": True,
            "health_window_date": get_health_window_date().strftime("%d.%m.%Y"),
            "refresh_in_progress": is_model_health_refresh_running(),
            "model_status_rows": _serialize_model_status_rows_for_api(rows),
        }
    )


def admin_model_status_refresh_view(request):
    if not _can_access_arm(request):
        return HttpResponseForbidden("Access denied")

    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    try:
        started = trigger_model_health_refresh_async()
    except Exception as exc:
        return JsonResponse(
            {
                "ok": False,
                "message": f"Не удалось запустить обновление моделей: {exc}",
            },
            status=500,
        )

    if started:
        message = "Обновление моделей запущено в фоне. Окно 04:00 МСК не изменяется."
    else:
        message = "Обновление уже выполняется. Дождитесь завершения."

    return JsonResponse(
        {
            "ok": True,
            "message": message,
            "refresh_in_progress": is_model_health_refresh_running(),
        }
    )


def admin_my_prompt_view(request):
    if not _can_access_prompt_admin(request):
        return HttpResponseForbidden("Access denied")
    request._mine_only = True
    prompt_admin = admin.site._registry.get(Prompt)
    if prompt_admin is None:
        return HttpResponse("Prompt admin is not registered", status=404)
    return prompt_admin.changelist_view(request, extra_context={"mine_only": True})


def admin_request_logs_view(request):
    if not _can_access_logs(request):
        return HttpResponseForbidden("Access denied")

    qs = AIRequestLog.objects.all()

    status = request.GET.get("status", "").strip()
    source = request.GET.get("source", "").strip()
    model = request.GET.get("model", "").strip()
    user_q = request.GET.get("user", "").strip()
    date_from = request.GET.get("date_from", "").strip()
    date_to = request.GET.get("date_to", "").strip()

    status_values = dict(AIRequestLog.STATUS_CHOICES)
    source_values = dict(AIRequestLog.SOURCE_CHOICES)

    if status in status_values:
        qs = qs.filter(status=status)
    if source in source_values:
        qs = qs.filter(source=source)
    if model:
        qs = qs.filter(model_names__contains=[model])
    if user_q:
        qs = qs.filter(
            Q(user_full_name__icontains=user_q)
            | Q(username__icontains=user_q)
            | Q(external_user_id__icontains=user_q)
        )
    if date_from:
        qs = qs.filter(sent_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(sent_at__date__lte=date_to)

    qs = qs.order_by("-sent_at")

    paginator = Paginator(qs, 50)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    filters_query = request.GET.copy()
    filters_query.pop("page", None)
    filters_query_str = urlencode(filters_query)
    if filters_query_str:
        filters_query_str += "&"

    context = {
        **admin.site.each_context(request),
        "title": "DL.AI: Логи запросов",
        "page_obj": page_obj,
        "status_choices": AIRequestLog.STATUS_CHOICES,
        "source_choices": AIRequestLog.SOURCE_CHOICES,
        "filters_query": filters_query_str,
        "filters": {
            "status": status,
            "source": source,
            "model": model,
            "user": user_q,
            "date_from": date_from,
            "date_to": date_to,
        },
    }
    return TemplateResponse(request, "admin/ai/request_logs.html", context)


def admin_logout_view(request):
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])

    next_candidate = request.POST.get("next") if request.method == "POST" else request.GET.get("next")
    next_url = _safe_relative_url(next_candidate, "/ai/admin/")
    next_path = quote(next_url, safe="/?=&")

    auth_logout(request)
    response = redirect(f"/ai/admin/login/?next={next_path}")
    response.set_cookie(ADMIN_LOGOUT_COOKIE_NAME, "1", path="/ai/admin/", samesite="Lax")
    return response


_default_get_urls = admin.site.get_urls


def _custom_admin_urls():
    from .views import set_password_view

    custom_urls = [
        path(
            "logout/",
            admin_logout_view,
            name="logout",
        ),
        path(
            "set-password/",
            set_password_view,
            name="set_password_view",
        ),
        path(
            "arm/find-error/start/",
            admin.site.admin_view(admin_arm_find_error_start_view),
            name="ai_arm_find_error_start",
        ),
        path(
            "arm/find-error/status/",
            admin.site.admin_view(admin_arm_find_error_status_view),
            name="ai_arm_find_error_status",
        ),
        path(
            "arm/models/refresh/",
            admin.site.admin_view(admin_model_status_refresh_view),
            name="ai_arm_model_status_refresh",
        ),
        path(
            "arm/models/state/",
            admin.site.admin_view(admin_model_status_state_view),
            name="ai_arm_model_status_state",
        ),
        path(
            "arm/models/",
            admin.site.admin_view(admin_model_status_view),
            name="ai_arm_model_status",
        ),
        path(
            "arm/find-error/",
            admin.site.admin_view(admin_arm_find_error_view),
            name="ai_arm_find_error",
        ),
        path(
            "prompts/my/",
            admin.site.admin_view(admin_my_prompt_view),
            name="ai_my_prompt",
        ),
        path(
            "logs/",
            admin.site.admin_view(admin_request_logs_view),
            name="ai_request_logs",
        ),
    ]
    return custom_urls + _default_get_urls()


admin.site.get_urls = _custom_admin_urls


def _custom_has_permission(request):
    # На странице логина — если пользователь уже аутентифицирован через middleware,
    # автоматически пропускаем. Иначе очищаем сессию и требуем вход.
    if request.path.startswith("/ai/admin/login/"):
        if request.method != "POST":
            if _is_admin_logout_forced(request):
                if request.user.is_authenticated:
                    auth_logout(request)
                request.session.pop("admin_fresh_auth", None)
                return False
            # Если middleware уже залогинил пользователя — пропускаем
            if request.user.is_authenticated and request.session.get("admin_fresh_auth"):
                return True
            # Если есть user_info от middleware, но пользователь не залогинен — пробуем автологин
            if hasattr(request, 'user_info') and _auto_login_from_external(request):
                return True
            # Иначе — разлогиниваем и показываем форму
            if request.user.is_authenticated:
                auth_logout(request)
            request.session.pop("admin_fresh_auth", None)
        return False

    user = getattr(request, "user", None)
    if not user or not user.is_authenticated or not user.is_active:
        return False
    if _is_staff_or_superuser(user):
        return True
    return user.groups.filter(name=PROMPT_DEVELOPER_GROUP).exists()


admin.site.has_permission = _custom_has_permission
admin.site.login_form = TesterOrStaffAdminAuthenticationForm


_default_admin_view = admin.site.admin_view


def _custom_admin_view(view, cacheable=False):
    wrapped_view = _default_admin_view(view, cacheable)

    def inner(request, *args, **kwargs):
        external_entry_response = _external_admin_entry_response(request)
        if external_entry_response is not None:
            return external_entry_response

        # Если пользователь уже аутентифицирован через middleware, но нет fresh_auth — ставим
        if request.user.is_authenticated and not request.session.get("admin_fresh_auth"):
            if hasattr(request, 'user_info') or request.user.groups.filter(name=PROMPT_DEVELOPER_GROUP).exists():
                request.session["admin_fresh_auth"] = True
        
        # Check if user needs to set password (avoid breaking POST saves)
        if request.user.is_authenticated and (not request.user.has_usable_password()):
            # Allow only set-password view and logout; skip redirect for POST
            if not request.path.startswith("/ai/admin/set-password/") and request.method == "GET":
                next_path = quote(request.get_full_path(), safe="/?=&")
                return redirect(f"/ai/admin/set-password/?next={next_path}")
        
        if request.user.is_authenticated and not request.session.get("admin_fresh_auth"):
            next_path = quote(request.get_full_path(), safe="/?=&")
            return redirect(f"/ai/admin/login/?next={next_path}")
        response = wrapped_view(request, *args, **kwargs)
        if request.session.pop("admin_manual_login", None):
            response.delete_cookie(ADMIN_LOGOUT_COOKIE_NAME, path="/ai/admin/")
        return response

    return inner


admin.site.admin_view = _custom_admin_view


_default_each_context = admin.site.each_context


def _custom_each_context(request):
    context = _default_each_context(request)
    context["show_arm_link"] = _can_access_arm(request)
    context["show_model_status_link"] = _can_access_model_status(request)
    context["show_prompt_link"] = _can_access_prompt_admin(request)
    context["show_logs_link"] = _can_access_logs(request)
    context["arm_find_error_url"] = "/ai/admin/arm/find-error/"
    context["arm_model_status_url"] = "/ai/admin/arm/models/"
    context["arm_model_status_refresh_url"] = "/ai/admin/arm/models/refresh/"
    context["arm_model_status_state_url"] = "/ai/admin/arm/models/state/"
    context["prompt_admin_url"] = "/ai/admin/ai/prompt/"
    context["my_prompt_url"] = "/ai/admin/prompts/my/"
    context["my_prompt_change_url"] = _get_my_prompt_admin_url(request)
    context["ai_logs_url"] = "/ai/admin/logs/"
    return context


admin.site.each_context = _custom_each_context
admin.site.index_template = "admin/ai/index.html"
admin.site.app_index_template = "admin/ai/app_index.html"
admin.site.site_url = "/ai/chat/"

# Форма для Prompt с улучшенным Textarea
class PromptForm(forms.ModelForm):
    programming_language = forms.ModelChoiceField(
        queryset=ProgrammingLanguage.objects.none(),
        required=False,
        label="Programming language",
    )

    class Meta:
        model = Prompt
        fields = '__all__'
        widgets = {
            'prompt_text': forms.Textarea(attrs={
                'rows': 25,
                'style': 'width: 95%; font-family: monospace; line-height: 1.4; white-space: pre-wrap;'
            }),
            'prompt_text_ru': forms.Textarea(attrs={
                'rows': 25,
                'style': 'width: 95%; font-family: monospace; line-height: 1.4; white-space: pre-wrap;'
            }),
            'prompt_text_en': forms.Textarea(attrs={
                'rows': 25,
                'style': 'width: 95%; font-family: monospace; line-height: 1.4; white-space: pre-wrap;'
            }),
            'prompt_text_fr': forms.Textarea(attrs={
                'rows': 25,
                'style': 'width: 95%; font-family: monospace; line-height: 1.4; white-space: pre-wrap;'
            }),
            'prompt_text_override': forms.Textarea(attrs={
                'rows': 25,
                'style': 'width: 95%; font-family: monospace; line-height: 1.4; white-space: pre-wrap;'
            }),
        }

    class Media:
        js = ("admin/js/prompt_language_topic.js",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if "programming_language" not in self.fields or "topic" not in self.fields:
            return

        self.fields["programming_language"].queryset = ProgrammingLanguage.objects.order_by("language_name")
        self.fields["programming_language"].widget.attrs["data-topics-url"] = "/ai/api/topics/"

        selected_language_id = self._resolve_selected_language_id()
        self.fields["topic"].queryset = Topic.objects.none()
        if selected_language_id:
            self.fields["topic"].queryset = Topic.objects.filter(
                programming_language_id=selected_language_id
            ).order_by("topic_name")
        elif self.instance.pk and self.instance.topic_id:
            self.fields["topic"].queryset = Topic.objects.filter(pk=self.instance.topic_id)
        else:
            self.fields["topic"].widget.attrs["disabled"] = "disabled"

        if not self.is_bound and selected_language_id:
            self.fields["programming_language"].initial = selected_language_id

    def _resolve_selected_language_id(self):
        if self.is_bound:
            language_id = self.data.get(self.add_prefix("programming_language"))
            if language_id:
                return language_id

            topic_id = self.data.get(self.add_prefix("topic"))
            if topic_id:
                return Topic.objects.filter(pk=topic_id).values_list("programming_language_id", flat=True).first()
            return None

        if self.instance.pk and self.instance.topic_id:
            return self.instance.topic.programming_language_id
        return None

    def clean(self):
        cleaned_data = super().clean()
        if "programming_language" not in self.fields or "topic" not in self.fields:
            return cleaned_data

        topic = cleaned_data.get("topic")
        programming_language = cleaned_data.get("programming_language")

        if topic and not programming_language:
            self.add_error("programming_language", "Выберите язык программирования.")
        if topic and programming_language and topic.programming_language_id != programming_language.id:
            self.add_error("topic", "Тема не относится к выбранному языку программирования.")
        return cleaned_data


class SharedPromptForm(forms.ModelForm):
    """Форма для общих (shared) препромптов."""
    class Meta:
        model = SharedPrompt
        fields = '__all__'
        widgets = {
            'prompt_text': forms.Textarea(attrs={
                'rows': 25,
                'style': 'width: 95%; font-family: monospace; line-height: 1.4; white-space: pre-wrap;'
            }),
            'prompt_text_ru': forms.Textarea(attrs={
                'rows': 25,
                'style': 'width: 95%; font-family: monospace; line-height: 1.4; white-space: pre-wrap;'
            }),
            'prompt_text_en': forms.Textarea(attrs={
                'rows': 25,
                'style': 'width: 95%; font-family: monospace; line-height: 1.4; white-space: pre-wrap;'
            }),
            'prompt_text_fr': forms.Textarea(attrs={
                'rows': 25,
                'style': 'width: 95%; font-family: monospace; line-height: 1.4; white-space: pre-wrap;'
            }),
        }

class SharedPromptAdmin(admin.ModelAdmin):
    form = SharedPromptForm
    list_display = ('prompt_name', 'language_list', 'updated_at', 'owner_username')
    list_display_links = ('prompt_name',)
    list_filter = ('programming_languages',)
    search_fields = ('prompt_name', 'prompt_text')
    autocomplete_fields = ('owner', 'editors')
    filter_horizontal = ('programming_languages', 'editors')

    def language_list(self, obj):
        langs = obj.programming_languages.all()
        return ", ".join([l.language_name for l in langs]) if langs else "Все языки"
    language_list.short_description = "Языки"

    def owner_username(self, obj):
        return obj.owner.username if obj.owner else "-"
    owner_username.short_description = "Owner"

    def has_module_permission(self, request):
        if _is_staff_or_superuser(request.user):
            return True
        return _is_prompt_developer_user(request)

    def has_view_permission(self, request, obj=None):
        return _is_staff_or_superuser(request.user) or _is_prompt_developer_user(request)

    def has_add_permission(self, request):
        return _is_staff_or_superuser(request.user) or _is_prompt_developer_user(request)

    def has_change_permission(self, request, obj=None):
        if request.user.is_superuser:
            return True
        if not (_is_staff_or_superuser(request.user) or _is_prompt_developer_user(request)):
            return False
        if obj is None:
            return True
        if obj.owner_id == request.user.pk:
            return True
        return obj.editors.filter(pk=request.user.pk).exists()

    def has_delete_permission(self, request, obj=None):
        if request.user.is_superuser:
            return True
        if not (_is_staff_or_superuser(request.user) or _is_prompt_developer_user(request)):
            return False
        if obj is None:
            return True
        return obj.owner_id == request.user.pk

    def get_fieldsets(self, request, obj=None):
        return (
            (None, {"fields": (
                "prompt_name", "prompt_name_ru", "prompt_name_en", "prompt_name_fr",
                "prompt_text", "prompt_text_ru", "prompt_text_en", "prompt_text_fr",
                "programming_languages",
            )}),
            ("Доступ", {"fields": ("owner", "editors"), "classes": ("collapse",)}),
        )


# Inline для Prompt
class PromptInline(admin.TabularInline):
    model = Prompt
    form = PromptForm
    extra = 0
    fields = ('prompt_name',)
    classes = ('collapse',)

    def get_queryset(self, request):
        return _prompt_queryset_for_user(
            super().get_queryset(request).select_related("owner"),
            request.user,
        )

# Inline для Topic (исправлено: было "ininlines")
class TopicInline(admin.TabularInline):
    model = Topic
    extra = 1
    fk_name = 'programming_language'
    show_change_link = True  # Добавляем ссылку на изменение

class ProgrammingLanguageAdmin(admin.ModelAdmin):
    inlines = [TopicInline]
    list_display = ('language_name',)
    search_fields = ('language_name',)

class TopicAdmin(admin.ModelAdmin):
    inlines = [PromptInline]
    list_display = ('topic_name', 'programming_language')
    list_filter = ('programming_language',)
    search_fields = ('topic_name', 'topic_name_ru', 'topic_name_en', 'topic_name_fr')
    raw_id_fields = ('programming_language',)  # Для удобства при многих языках
    fieldsets = (
        (None, {"fields": ("topic_name", "topic_name_ru", "topic_name_en", "topic_name_fr", "programming_language")}),
    )


class PromptUserIdFilter(admin.SimpleListFilter):
    title = "userId"
    parameter_name = "user_id"

    def lookups(self, request, model_admin):
        if not request.user.is_superuser:
            return ()

        users = (
            User.objects.filter(Q(owned_prompts__isnull=False) | Q(editable_prompts__isnull=False))
            .distinct()
            .order_by("id")
        )
        return [(str(user.id), f"{user.id}: {user.get_username()}") for user in users]

    def queryset(self, request, queryset):
        value = self.value()
        if not value:
            return queryset
        return queryset.filter(Q(owner_id=value) | Q(editors__id=value)).distinct()


class PromptAdmin(admin.ModelAdmin):
    form = PromptForm
    list_display = (
        'prompt_name',
        'programming_language_name',
        'topic',
        'owner_user_id',
        'owner_username',
        'short_prompt_text',
    )
    list_display_links = ('prompt_name',)
    list_filter = (PromptUserIdFilter, 'topic__programming_language', 'topic')
    list_per_page = 25
    search_fields = ('prompt_name', 'prompt_text', 'owner__username', '=owner__id')
    autocomplete_fields = ("owner", "editors")
    actions = ("export_prompts_csv",)
    date_hierarchy = "created_at" if any(field.name == "created_at" for field in Prompt._meta.fields) else None

    def get_queryset(self, request):
        queryset = (
            super()
            .get_queryset(request)
            .select_related("topic", "topic__programming_language", "owner")
            .prefetch_related("editors")
        )
        if _is_mine_only_request(request):
            user = getattr(request, "user", None)
            if not user or not user.is_authenticated:
                return queryset.none()
            return _prompt_queryset_for_user(queryset, user)
        return queryset

    def lookup_allowed(self, lookup, value, request=None):
        if lookup == "mine":
            return True
        return super().lookup_allowed(lookup, value)

    def _can_edit_prompt(self, request, obj):
        if not (_is_staff_or_superuser(request.user) or _is_prompt_developer_user(request)):
            return False
        if request.user.is_superuser:
            return True
        if obj is None:
            return True
        if obj.owner_id == request.user.pk:
            return True
        return obj.editors.filter(pk=request.user.pk).exists()

    def has_module_permission(self, request):
        if _is_staff_or_superuser(request.user):
            return True
        return _is_prompt_developer_user(request)

    def has_view_permission(self, request, obj=None):
        return _is_staff_or_superuser(request.user) or _is_prompt_developer_user(request)

    def has_change_permission(self, request, obj=None):
        return self._can_edit_prompt(request, obj)

    def has_add_permission(self, request):
        return _is_staff_or_superuser(request.user) or _is_prompt_developer_user(request)

    def has_delete_permission(self, request, obj=None):
        if request.user.is_superuser:
            return True
        if not (_is_staff_or_superuser(request.user) or _is_prompt_developer_user(request)):
            return False
        if obj is None:
            return True
        return obj.owner_id == request.user.pk

    def get_fieldsets(self, request, obj=None):
        main_fields = (
            "programming_language", "topic",
            "prompt_name", "prompt_name_ru", "prompt_name_en", "prompt_name_fr",
            "shared_prompt", "prompt_text_override",
            "prompt_text", "prompt_text_ru", "prompt_text_en", "prompt_text_fr",
        )
        if request.user.is_superuser:
            return (
                (None, {"fields": main_fields}),
                ("Access", {"fields": ("owner", "editors"), "classes": ("collapse",)}),
            )
        return ((None, {"fields": main_fields}),)

    def get_readonly_fields(self, request, obj=None):
        if _is_staff_or_superuser(request.user):
            return ()
        if self._can_edit_prompt(request, obj):
            return ()
        return (
            "programming_language", "topic",
            "prompt_name", "prompt_name_ru", "prompt_name_en", "prompt_name_fr",
            "shared_prompt", "prompt_text_override",
            "prompt_text", "prompt_text_ru", "prompt_text_en", "prompt_text_fr",
        )

    def save_model(self, request, obj, form, change):
        if not change and not obj.owner_id:
            obj.owner = request.user
        super().save_model(request, obj, form, change)
        if not request.user.is_superuser:
            obj.editors.add(request.user)

    def export_prompts_csv(self, request, queryset):
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="prompts.csv"'
        writer = csv.writer(response)
        writer.writerow(["id", "prompt_name", "language", "topic", "owner_id", "owner_username", "prompt_text"])
        for prompt in queryset.select_related("topic", "topic__programming_language", "owner"):
            topic = prompt.topic
            language = topic.programming_language.language_name if topic and topic.programming_language else ""
            writer.writerow([
                prompt.id,
                prompt.prompt_name or "",
                language,
                topic.topic_name if topic else "",
                prompt.owner_id or "",
                prompt.owner.username if prompt.owner else "",
                prompt.prompt_text,
            ])
        return response
    export_prompts_csv.short_description = "Export selected prompts to CSV"

    def programming_language_name(self, obj):
        if obj.topic and obj.topic.programming_language:
            return obj.topic.programming_language.language_name
        return "-"
    programming_language_name.short_description = "Language"
    programming_language_name.admin_order_field = "topic__programming_language__language_name"

    def programming_language(self, obj):
        return self.programming_language_name(obj)
    programming_language.short_description = "Programming language"

    def owner_user_id(self, obj):
        return obj.owner_id or "-"
    owner_user_id.short_description = "userId"
    owner_user_id.admin_order_field = "owner_id"

    def owner_username(self, obj):
        return obj.owner.username if obj.owner else "-"
    owner_username.short_description = "Owner"

    def short_prompt_text(self, obj):
        return f"{obj.prompt_text[:100]}..." if len(obj.prompt_text) > 100 else obj.prompt_text
    short_prompt_text.short_description = "Prompt Text"


@admin.register(AIAppSettings)
class AIAppSettingsAdmin(admin.ModelAdmin):
    list_display = ("is_enabled", "updated_at")

    def has_add_permission(self, request):
        if AIAppSettings.objects.exists():
            return False
        return super().has_add_permission(request)

    def has_delete_permission(self, request, obj=None):
        return False

@admin.register(AIRequestLog)
class AIRequestLogAdmin(admin.ModelAdmin):
    list_display = (
        "sent_at",
        "received_at",
        "user_full_name",
        "username",
        "external_user_id",
        "model_names_display",
        "programming_language_name",
        "topic_name",
        "prompt_name",
        "status",
        "source",
        "duration_seconds",
    )
    list_filter = ("status", "source", "programming_language_name", "sent_at")
    search_fields = (
        "external_user_id",
        "username",
        "user_full_name",
        "message",
        "programming_language_name",
        "topic_name",
        "prompt_name",
    )
    date_hierarchy = "sent_at"
    ordering = ("-sent_at",)
    readonly_fields = [f.name for f in AIRequestLog._meta.fields]

    def has_module_permission(self, request):
        return _can_access_logs(request)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return _can_access_logs(request)

    def model_names_display(self, obj):
        return ", ".join(obj.model_names or [])
    model_names_display.short_description = "Модели"

    def programming_language_name(self, obj):
        return obj.programming_language_name or "—"
    programming_language_name.short_description = "Язык программирования"

    def topic_name(self, obj):
        return obj.topic_name or "—"
    topic_name.short_description = "Тема"

    def prompt_name(self, obj):
        return obj.prompt_name or "—"
    prompt_name.short_description = "Препромпт"


# Регистрация
admin.site.register(ProgrammingLanguage, ProgrammingLanguageAdmin)
admin.site.register(Topic, TopicAdmin)
admin.site.register(Prompt, PromptAdmin)
admin.site.register(SharedPrompt, SharedPromptAdmin)
