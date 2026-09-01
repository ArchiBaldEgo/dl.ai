"""Кастомный admin-site для AI-приложения (заменяет стандартный admin.site).

Реализует DLSID-аутентификацию: вход через dl.gsu.by, проверка соответствия
сессии внешнему ID, редирект на set-password при первом входе.
Фильтрация видимых моделей по правам пользователя (через permissions.py).
"""

import logging
import os

from django.contrib import admin
from django.contrib.auth import logout as auth_logout
from django.http import HttpResponseForbidden
from django.shortcuts import redirect
from django.urls import path
from django.utils.http import urlencode

from .permissions import (
    can_access_admin,
    can_access_arm,
    can_access_logs,
    can_access_model_status,
    can_access_prompt_admin,
    can_access_prompt_regression,
    can_access_test_console,
    is_prompt_developer_user,
    is_staff_or_superuser,
)
from ..auth_backends import get_external_user_id_from_request

logger = logging.getLogger(__name__)


def _is_admin_login_path(path: str) -> bool:
    normalized = (path or "/").split("?")[0].rstrip("/") or "/"
    return normalized == "/ai/admin/login"


def _is_admin_logout_path(path: str) -> bool:
    normalized = (path or "/").split("?")[0].rstrip("/") or "/"
    return normalized == "/ai/admin/logout"


def _is_admin_set_password_path(path: str) -> bool:
    normalized = (path or "/").split("?")[0].rstrip("/") or "/"
    return normalized == "/ai/admin/set-password"


def _session_matches_external_id(request, external_id: str) -> bool:
    """Return True iff ``request.user`` is the user provisioned for
    ``external_id`` by ExternalAuthMiddleware.

    Username is NOT a reliable key: ``get_or_create_user_from_external``
    may have set it to ``login`` (a nickname), ``user_<id>`` (numeric
    fallback), or a suffixed variant — none of which is the literal
    ``userId`` from the external API. The trustworthy link is the
    ``ExternalDLAccount.external_user_id`` row, and the most efficient
    check is the one the middleware itself can stash on the request.
    """
    if not external_id:
        return False
    user = getattr(request, "user", None)
    if not user or not getattr(user, "is_authenticated", False):
        return False
    provisioned = getattr(request, "_ai_provisioned_user", None)
    if provisioned is not None and getattr(provisioned, "pk", None) is not None:
        return getattr(user, "pk", None) == provisioned.pk
    # Fallback: middleware didn't tag the request (e.g. set-password
    # view was entered via a form POST). Compare against the
    # ExternalDLAccount row that the API just confirmed.
    from ..models import ExternalDLAccount
    try:
        account = (
            ExternalDLAccount.objects
            .select_related("user")
            .filter(external_user_id=str(external_id))
            .first()
        )
    except Exception:
        return False
    return bool(account and account.user_id == getattr(user, "pk", None))


def _redirect_to_dl(request):
    url = os.getenv("EXTERNAL_AUTH_REDIRECT_URL", "https://dl.gsu.by")
    return redirect(url)


class AIAdminSite(admin.AdminSite):
    """Кастомный AdminSite для AI-приложения.

    Особенности:
    - Аутентификация через DLSID (dl.gsu.by), а не через стандартную форму.
    - Проверка соответствия сессии внешнему ID (_session_matches_external_id).
    - Редирект на set-password при отсутствии пароля.
    - Фильтрация моделей в sidebar по правам пользователя (filter_app_list_for_user).
    - Кастомные ссылки на ARM, статусы моделей, регрессионные тесты, логи.
    """
    site_header = "ИИ-админка DL.AI"
    site_title = "ИИ-админка DL.AI"
    index_template = "admin/ai/index.html"
    app_index_template = "admin/ai/app_index.html"
    site_url = "/ai/chat/"

    def app_index(self, request, app_label, extra_context=None):
        """Redirect /ai/admin/ai/ → /ai/admin/ — we don't use the per-app index,
        all models are shown on the main admin dashboard."""
        from django.shortcuts import redirect
        return redirect("admin:index")

    def has_permission(self, request):
        user = getattr(request, "user", None)
        if not user or not user.is_authenticated:
            logger.info("has_permission: anonymous → False")
            return False
        if getattr(user, "is_active", True) is False:
            logger.info("has_permission: inactive user → False")
            return False
        # Login / logout pages are reachable without an external id —
        # we redirect them away anyway, so no point in checking.
        if _is_admin_login_path(request.path) or _is_admin_logout_path(request.path):
            return True
        external_id = get_external_user_id_from_request(request)
        # The set-password page is allowed only when the request carries a
        # DLSID-validated external id and the local session belongs to the
        # provisioned user. Without this check an attacker could pass an
        # arbitrary uid/userId query parameter and set a password for that
        # external account.
        if _is_admin_set_password_path(request.path):
            if not external_id:
                return False
            return _session_matches_external_id(request, external_id)
        # For every other admin page the user must have a verified
        # external id (DLSID / DLID / uid query) on the current request,
        # AND the local session must belong to the same user that the
        # external API just authenticated. Otherwise a stale Django
        # session from a different account (e.g. a superuser from
        # yesterday) would silently get admin access on someone else's
        # DLSID — which is exactly the bug we are fixing here.
        if not external_id:
            logger.info("has_permission: no external_id → False")
            return False
        if not _session_matches_external_id(request, external_id):
            logger.info(
                "has_permission: session/user mismatch → False "
                "(user=%s pk=%s external_id=%s provisioned=%s)",
                getattr(user, "username", "?"),
                getattr(user, "pk", "?"),
                external_id,
                getattr(getattr(request, "_ai_provisioned_user", None), "pk", "?"),
            )
            return False
        if not can_access_admin(user):
            logger.info(
                "has_permission: can_access_admin=False for user=%s",
                getattr(user, "username", "?"),
            )
            return False
        return True

    def admin_view(self, view, cacheable=False):
        wrapped_view = super().admin_view(view, cacheable)

        def inner(request, *args, **kwargs):
            # Bounce the user back to dl.gsu.by if the DLSID chain is
            # broken on this request. The login / set-password pages
            # are exempt: login is the entry, set-password is what we
            # send the user to when the cookie is present but the
            # local password is missing.
            if (
                request.user.is_authenticated
                and not _is_admin_login_path(request.path)
                and not _is_admin_set_password_path(request.path)
                and not get_external_user_id_from_request(request)
            ):
                auth_logout(request)
                return _redirect_to_dl(request)

            if request.user.is_authenticated and not request.session.get("admin_fresh_auth"):
                if get_external_user_id_from_request(request):
                    if is_prompt_developer_user(request.user) or is_staff_or_superuser(request.user):
                        request.session["admin_fresh_auth"] = True

            if request.user.is_authenticated and (not request.user.has_usable_password()):
                if not _is_admin_set_password_path(request.path) and request.method == "GET":
                    next_query = urlencode({"next": request.get_full_path()})
                    return redirect(f"/ai/admin/set-password/?{next_query}")

            if request.user.is_authenticated and not request.session.get("admin_fresh_auth"):
                next_query = urlencode({"next": request.get_full_path()})
                return redirect(f"/ai/admin/login/?{next_query}")

            response = wrapped_view(request, *args, **kwargs)

            # Belt-and-braces: deny direct hits to ModelAdmin URLs that
            # the current user is not allowed to see.
            if request.method == "GET" and request.resolver_match is not None:
                model_admin = self._registry_by_url_name(request)
                if model_admin is not None and not model_admin.has_module_permission(request):
                    return HttpResponseForbidden("Access denied")

            return response

        return inner

    def _registry_by_url_name(self, request):
        match = request.resolver_match
        if match is None or match.app_name != self.name:
            return None
        url_name = match.url_name
        for model, admin_obj in self._registry.items():
            info = admin_obj.model._meta.app_label, admin_obj.model._meta.model_name
            if url_name in {
                f"{info[0]}_{info[1]}_changelist",
                f"{info[0]}_{info[1]}_add",
                f"{info[0]}_{info[1]}_change",
                f"{info[0]}_{info[1]}_delete",
                f"{info[0]}_{info[1]}_history",
            }:
                return admin_obj
        return None

    def login(self, request, extra_context=None):
        """Route the user to the admin entry point.

        The only supported sign-in path is the DLSID cookie set by
        dl.gsu.by, so:

        * If the request already carries an external id (DLSID / DLID /
          uid query) the user is "signed in" from dl.gsu.by's point of
          view — we redirect to ``?next=`` (or the admin index) without
          rendering a local login form.
        * Otherwise we bounce to dl.gsu.by with the original ``?next=``
          preserved, so the user lands back on the admin page after
          authenticating on the main site.
        """
        from django.utils.http import urlencode
        from ..http_utils import safe_relative_url

        next_url = safe_relative_url(request.GET.get("next"), "/ai/admin/")
        if get_external_user_id_from_request(request):
            return redirect(next_url)

        url = os.getenv("EXTERNAL_AUTH_REDIRECT_URL", "https://dl.gsu.by")
        separator = "&" if "?" in url else "?"
        return redirect(f"{url}{separator}{urlencode({'next': next_url})}")

    def get_app_list(self, request):
        app_list = super().get_app_list(request)
        from .permissions import filter_app_list_for_user
        # Tag every model with the actual class so filter_app_list_for_user
        # can look up the corresponding ModelAdmin in self._registry (which
        # is keyed by model class, not by string name).
        registry = self._registry
        for app in app_list:
            for model in app.get("models", []):
                for cls, admin_obj in registry.items():
                    if cls.__name__ == model.get("object_name"):
                        model["_model_cls"] = cls
                        break
        request._ai_admin_registry = registry
        return filter_app_list_for_user(app_list, request)

    def each_context(self, request):
        from .my_prompt import get_my_prompt_admin_url
        context = super().each_context(request)
        is_pd = is_prompt_developer_user(request.user)
        is_staff = is_staff_or_superuser(request.user)
        is_super = bool(getattr(request.user, "is_superuser", False))
        context["is_prompt_developer"] = is_pd
        context["is_staff_or_superuser"] = is_staff
        # Суперпользователь: отсутствие предупреждения «не покидайте страницу»
        # в тестовой консоли. Личное меню «мои процессы» в шапке (ai_processes.js
        # + active_runs) видно всем админам и показывает только свои прогоны.
        context["is_super_user"] = is_super
        context["user_display_name"] = self._user_display_name(request.user)
        context["user_role_label"] = self._user_role_label(
            request.user, is_super, is_staff, is_pd,
        )
        show_arm = can_access_arm(request)
        context["show_arm_link"] = show_arm
        show_model_status = can_access_model_status(request)
        context["show_model_status_link"] = show_model_status
        context["show_prompt_link"] = can_access_prompt_admin(request)
        show_logs = can_access_logs(request)
        context["show_logs_link"] = show_logs
        show_prompt_regression = can_access_prompt_regression(request)
        context["show_prompt_regression_link"] = show_prompt_regression
        show_test_console = can_access_test_console(request)
        context["show_test_console_link"] = show_test_console
        arm_find_error_url = "/ai/admin/arm/find-error/"
        arm_solve_url = "/ai/admin/arm/solve/"
        arm_model_status_url = "/ai/admin/arm/models/"
        context["arm_find_error_url"] = arm_find_error_url
        context["arm_solve_url"] = arm_solve_url
        context["arm_model_status_url"] = arm_model_status_url
        context["arm_model_status_refresh_url"] = "/ai/admin/arm/models/refresh/"
        context["arm_model_status_state_url"] = "/ai/admin/arm/models/state/"
        prompt_regression_url = "/ai/admin/prompt-regression/"
        test_console_url = "/ai/admin/test-console/"
        context["prompt_regression_url"] = prompt_regression_url
        context["test_console_url"] = test_console_url
        context["prompt_admin_url"] = "/ai/admin/ai/prompt/"
        my_prompt_url = "/ai/admin/prompts/my/"
        context["my_prompt_url"] = my_prompt_url
        context["my_prompt_change_url"] = get_my_prompt_admin_url(request)
        ai_logs_url = "/ai/admin/ai/airequestlog/"
        context["ai_logs_url"] = ai_logs_url
        show_updates = is_staff
        context["show_updates_link"] = show_updates
        updates_url = "/ai/admin/updates/"
        context["updates_url"] = updates_url

        # --- AI tools in the left navigation sidebar (#nav-sidebar) ---
        # The left nav renders `available_apps` (separate from the dashboard's
        # main `app_list`), so we inject the custom (non-ModelAdmin) tool pages
        # as fake "app" groups. They then render natively via admin/app_list.html
        # (with the stock filter + current-page highlight) on every admin page,
        # without appearing in the dashboard's main model list.
        tools_apps = self._build_ai_nav_apps(
            request,
            tools=[
                # (group, label, object_name, url, flag)
                ("Инструменты", "Мой промпт", "AiMyPrompt", my_prompt_url, is_pd),
                ("Инструменты", "Поиск ошибки (ARM)", "AiArmFindError", arm_find_error_url, show_arm),
                ("Инструменты", "Пакетное решение (ARM)", "AiArmSolve", arm_solve_url, show_arm),
                ("Инструменты", "Регрессионные тесты", "AiPromptRegression", prompt_regression_url, show_prompt_regression),
                ("Инструменты", "Тестовая консоль", "AiTestConsole", test_console_url, show_test_console),
                ("Администрирование", "Состояние моделей", "AiModelStatus", arm_model_status_url, show_model_status),
                ("Администрирование", "Журнал запросов", "AiRequestLogs", ai_logs_url, show_logs),
                ("Администрирование", "Обновления", "AiUpdates", updates_url, show_updates),
            ],
        )
        # Change/tool pages AND the dashboard: stock admin/nav_sidebar.html renders
        # available_apps (real apps + the injected tool groups), so «Раздел ИИ»
        # stays in the left nav on every page — including the dashboard.
        context["available_apps"] = list(context["available_apps"]) + tools_apps
        return context

    @staticmethod
    def _user_display_name(user):
        """Человекочитаемое имя для приветствия в шапке админки.

        Предпочитаем полное имя, затем имя+фамилию, иначе username без префикса
        ``user_`` (DL-провижненные аккаунты — это ``user_<id>``).
        """
        if not user or not getattr(user, "is_authenticated", False):
            return ""
        full = (getattr(user, "get_full_name", lambda: "")() or "").strip()
        if full:
            return full
        first = (getattr(user, "first_name", "") or "").strip()
        last = (getattr(user, "last_name", "") or "").strip()
        if first or last:
            return f"{first} {last}".strip()
        username = getattr(user, "get_username", lambda: "")() or ""
        if username.startswith("user_"):
            return username[5:]
        return username

    @staticmethod
    def _user_role_label(user, is_super, is_staff, is_pd):
        """Короткая русская метка роли для шапки админки."""
        if is_super:
            return "Суперпользователь"
        if is_staff:
            return "Администратор"
        if is_pd:
            return "Разработчик промптов"
        return "Пользователь"

    def _build_ai_nav_apps(self, request, *, tools):
        """Build fake "app" groups (for the left nav) from a list of tools.

        ``tools`` is a list of ``(group, label, object_name, url, visible)``
        tuples. Groups with no visible tool are omitted. The returned dicts mimic
        ``AdminSite._build_app_dict`` so ``admin/app_list.html`` renders them
        natively (per-tool ``current-model`` highlight via ``admin_url``).

        ``app_url`` is set to ``"#"`` deliberately: the group heading is a label,
        not a navigation target. The stock ``admin/app_list.html`` adds the
        ``current-app`` class (which renders the caption bold + header-coloured)
        when ``app.app_url in request.path`` — pointing it at the admin index
        would match EVERY admin page (all paths start with ``/ai/admin/``), so
        both tool groups would stay permanently bold. ``"#"`` never matches the
        path, so the heading keeps its normal weight and only the active *tool*
        row highlights via ``current-model``.
        """
        group_url = "#"

        label_to_key = {"Инструменты": "ai-tools", "Администрирование": "ai-admin"}
        groups: dict[str, dict] = {}
        for group, label, object_name, url, visible in tools:
            if not visible:
                continue
            app = groups.get(group)
            if app is None:
                app = {
                    "name": group,
                    "app_label": label_to_key.get(group, "ai-tools"),
                    "app_url": group_url,
                    "has_module_perms": True,
                    "models": [],
                }
                groups[group] = app
            app["models"].append({
                "name": label,
                "object_name": object_name,
                "admin_url": url,
                "add_url": None,
                "perms": {"view": True},
                "view_only": True,
            })
        # Stable order: Инструменты before Администрирование.
        order = ["Инструменты", "Администрирование"]
        return [groups[g] for g in order if g in groups]


ai_admin_site = AIAdminSite(name="admin")
