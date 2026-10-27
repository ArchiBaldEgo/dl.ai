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
    can_access_access_groups,
    can_access_admin,
    can_access_arm,
    can_access_dl_api_requests,
    can_access_logs,
    can_access_model_status,
    can_access_pinned_runs,
    can_access_prompt_admin,
    can_access_prompt_developer_docs,
    can_access_prompt_defaults,
    can_access_tasks,
    can_access_test_console,
    can_access_test_data,
    can_access_token_usage,
    can_access_updates,
    can_access_user_models,
    is_prompt_developer_user,
    is_developer_user,
    is_staff_or_superuser,
    is_superuser_user,
    user_has_key,
)
from ..auth_backends import get_external_user_id_from_request

logger = logging.getLogger(__name__)

# Временно скрытые разделы админки: убираются из левого меню (и из списка
# моделей на дашборде), но сами страницы по прямым URL продолжают работать —
# «просто скрыть, чтобы с ними никто не взаимодействовал». Сюда попадают и
# инструменты (_build_ai_nav_apps фильтрует по object_name). Вернуть раздел —
# удалить его имя из множества.
_HIDDEN_NAV_OBJECT_NAMES = {
    "User",              # «Аутентификация и авторизация» → «Пользователи»
    "Task",              # «Задачи (DL)»
    "UpdateLog",          # публикация скрытых записей «Обновлений»
}

# Иконки реальных ModelAdmin-строк (объекты, видимые в левом меню) — чтобы
# «Раздел ИИ» рендерился в том же иконизированном стиле, что и группы
# AI-инструментов (см. _build_ai_nav_apps и шаблон ai/app_list.html).
# Скрытые модели сюда не добавляем — они не рендерятся.
_REAL_MODEL_ICONS = {
    "ProgrammingLanguage": "⌨",
    "Topic": "▣",
    "Prompt": "✎",
    "SharedPrompt": "⁂",
    "ExternalDLAccount": "☰",
}


def _hide_nav_sections(app_list):
    """Выкинуть из app_list скрытые разделы; опустевшие приложения убрать."""
    filtered = []
    for app in app_list:
        models = [
            m for m in app.get("models", [])
            if m.get("object_name") not in _HIDDEN_NAV_OBJECT_NAMES
        ]
        if not models:
            # Например, auth остаётся без «Пользователей» — группа исчезает.
            continue
        app = dict(app, models=models)
        filtered.append(app)
    return filtered


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
    - Кастомные ссылки на ARM, статусы моделей, логи.
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
            # Честная эмуляция «Посмотреть как группа» (ai/admin/guest_mode.py):
            # до ЛЮБЫХ проверок проставляем на request.user контекст выбранной
            # группы — все can_access_*/has_*/ключи честно считаются как у
            # участника. Эндпоинты самого переключателя получают реальный
            # суперюзер (иначе выйти из режима нельзя).
            from .guest_mode import apply_view_as_user
            url_name = getattr(getattr(request, "resolver_match", None), "url_name", None)
            # /view-as/ — и надёжный путь-фолбэк (RequestFactory-запросы без
            # resolver_match), чтобы переключатель всегда видел реального супер.
            if url_name not in {"ai_view_as_select", "ai_view_as_groups"} \
                    and "/view-as/" not in request.path:
                apply_view_as_user(request)

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
        app_list = filter_app_list_for_user(app_list, request)
        # Временно скрытые разделы: убираем из навигации, но НЕ отключаем
        # сами страницы — прямые URL продолжают работать. Возврат — удалить
        # имя из множества.
        app_list = _hide_nav_sections(app_list)
        return app_list

    def each_context(self, request):
        from .guest_mode import is_viewing_as_guest
        from .my_prompt import get_my_prompt_admin_url
        from .test_data import ( get_test_data_url, can_access_test_data )

        context = super().each_context(request)
        is_pd = is_prompt_developer_user(request.user)
        is_staff = is_staff_or_superuser(request.user)
        # Честная эмуляция «Посмотреть как…»: супер-флаги считаются по
        # эффективному пользователю (см. permissions).
        is_super = is_superuser_user(request.user)
        context["is_prompt_developer"] = is_pd
        context["is_staff_or_superuser"] = is_staff
        # Суперпользователь: отсутствие предупреждения «не покидайте страницу»
        # в тестовой консоли. Личное меню «мои процессы» в шапке (ai_processes.js
        # + active_runs) видно всем админам и показывает только свои прогоны.
        context["is_super_user"] = is_super
        # Кнопка «Посмотреть как…» видна по РЕАЛЬному суперюзерству (иначе из
        # режима нельзя выйти); честные проверки (is_super_user и пр.)
        # эмулируют участника группы.
        context["real_super_user"] = bool(getattr(request.user, "is_superuser", False))
        from .guest_mode import view_as_group as _view_as_group
        _view_as = _view_as_group(request)
        context["view_as_group_name"] = getattr(_view_as, "name", "")
        # Режим «Посмотреть как…»: честная эмуляция участника группы
        # (см. guest_mode.py) — доступ уже вычислен по ключам группы
        # (guest-флаг оставлен только туру/wizard'у онбординга).
        guest = is_viewing_as_guest(request)
        context["is_viewing_as_guest"] = guest
        context["user_display_name"] = self._user_display_name(request.user)
        context["user_dl_id"] = self._user_dl_id(request.user)
        context["user_role_label"] = self._user_role_label(
            request.user, is_super, is_staff, is_pd,
        )
        if guest:
            context["user_role_label"] += " (смотрите как группу)"
        show_arm = can_access_arm(request)
        context["show_arm_link"] = show_arm
        # Суперюзерские инструменты честно считаются по ключам группы
        # (в эмуляции is_staff_or_superuser=False и ключи — группы).
        show_model_status = can_access_model_status(request)
        context["show_model_status_link"] = show_model_status
        context["show_prompt_link"] = can_access_prompt_admin(request)
        show_logs = can_access_logs(request)
        context["show_logs_link"] = show_logs
        show_test_console = can_access_test_console(request)
        context["show_test_console_link"] = show_test_console
        arm_solve_url = "/ai/admin/arm/solve/"
        arm_model_status_url = "/ai/admin/arm/models/"
        context["arm_solve_url"] = arm_solve_url
        context["arm_model_status_url"] = arm_model_status_url
        context["arm_model_status_refresh_url"] = "/ai/admin/arm/models/refresh/"
        context["arm_model_status_state_url"] = "/ai/admin/arm/models/state/"
        test_console_url = "/ai/admin/test-console/"
        context["test_console_url"] = test_console_url
        prompt_admin_url = "/ai/admin/ai/prompt/"
        context["prompt_admin_url"] = prompt_admin_url
        my_prompt_url = "/ai/admin/prompts/my/"
        context["my_prompt_url"] = my_prompt_url
        context["my_prompt_change_url"] = get_my_prompt_admin_url(request)
        ai_logs_url = "/ai/admin/ai/request_logs/"
        context["ai_logs_url"] = ai_logs_url
        show_updates = can_access_updates(request)
        context["show_updates_link"] = show_updates
        updates_url = "/ai/admin/updates/"
        context["updates_url"] = updates_url
        aiappsettings_url = self._aiappsettings_url()
        docs_url = "/ai/admin/docs/"
        pinned_runs_url = "/ai/admin/pinned-runs/"
        # Доска «Таски»: суперюзеры/«Разработчик» (страница + брать), мутации —
        # по action-ключам.
        tasks_url = "/ai/admin/tasks/"
        context["tasks_url"] = tasks_url
        context["show_tasks_link"] = can_access_tasks(request)

        test_data_url = get_test_data_url()
        # «Тестовые данные» остаются только пунктом меню «Диагностика»
        # (генератор записей журнала); флаг show_test_data_link удалён вместе
        # с кнопкой «Добавить запись» на странице журнала. Права: супер ИЛИ
        # ключ page.test_data.
        show_test_data = can_access_test_data(request)

        # --- AI tools in the left navigation sidebar (#nav-sidebar) ---
        # The left nav renders `available_apps` (separate from the dashboard's
        # main `app_list`), so we inject the custom (non-ModelAdmin) tool pages
        # as fake "app" groups. They then render through our project override of
        # admin/app_list.html (grouped, sorted, iconised) on every admin page,
        # without appearing in the dashboard's main model list.
        tools_apps = self._build_ai_nav_apps(
            request,
            tools=self._ai_tool_defs(request),
        )
        # Change/tool pages AND the dashboard: the left nav renders
        # available_apps on every page — tools first, then real model apps
        # (unified ai-nav-group markup, see admin/app_list.html). Decorate
        # real model rows with icons so «Раздел ИИ» matches the tool style.
        real_apps = list(context["available_apps"])
        for app in real_apps:
            for model in app.get("models", []):
                icon = _REAL_MODEL_ICONS.get(model.get("object_name"))
                if icon:
                    model["icon"] = icon
            # «Настройка ИИ-приложения» переехала в закреплённый первый пункт
            # навигации (см. tools выше) — в «Разделе ИИ» её больше не рисуем.
            app["models"] = [
                m for m in app.get("models", [])
                if m.get("object_name") != "AIAppSettings"
            ]
        context["available_apps"] = tools_apps + real_apps
        # Онбординг-wizard: конфиг для json_script в base_site.html
        # (читает static/admin/js/ai-wizard.js). В режиме «Посмотреть как…»
        # тур не показывается и не отмечается (см. onboarding.py).
        from .onboarding import wizard_context_for_request
        context["ai_wizard"] = wizard_context_for_request(request)
        return context

    def _ai_tool_defs(self, request):
        """Кастомные AI-инструменты левого нава — ЕДИНСТВЕННЫЙ источник.

        Здесь определяются (в этом порядке, group_order в _build_ai_nav_apps
        задаёт только порядок ГРУПП): навигация каждого рендера админки И
        секции «Страницы»/«Документация» страницы «Группы доступа»
        (ai/admin/access_groups.py::admin_access_groups_view разрешает
        выдачу только по access_key — новая страница, добавленная сюда со
        своим page.*/docs.*-ключом, появляется в «Группах доступа» сама).
        Формат кортежа: (group, label, object_name, url, visible, icon,
        hint, access_key, actions); access_key — ключ-страница/дока
        (page.*|docs.*) или None (доступ гейтится иными правами: staff/
        группой/суперюзером); actions — КОРТЕЖ вложенных (key, label)
        постраничных действий для их аккордеона в «Группах доступа»
        ([] у страниц без отдельных действий). Ключи действий могут быть
        легаси-section ACTION (action.task_create) — строки БД переезжают
        только зрительно, проверяющий код не меняется.
        """
        from .my_prompt import get_my_prompt_admin_url
        from .test_data import get_test_data_url, can_access_test_data

        user = request.user
        is_pd = is_prompt_developer_user(user)
        is_staff = is_staff_or_superuser(user)
        is_super = bool(getattr(user, "is_superuser", False))
        show_arm = can_access_arm(request)
        show_model_status = can_access_model_status(request)
        show_logs = can_access_logs(request)
        show_pinned = can_access_pinned_runs(request)
        show_test_console = can_access_test_console(request)
        show_updates = can_access_updates(request)
        show_tasks = can_access_tasks(request)
        show_test_data = can_access_test_data(request)
        show_groups = can_access_access_groups(request)

        docs_url = "/ai/admin/docs/"
        aiappsettings_url = self._aiappsettings_url()
        tasks_url = "/ai/admin/tasks/"
        my_prompt_url = "/ai/admin/prompts/my/"
        test_data_url = get_test_data_url()
        return [
            # «Настройка ИИ-приложения» закреплена самым первым пунктом
            # навигации (группа «ai-pinned» рендерится без заголовка, см.
            # admin/app_list.html). Раньше это была строка группы «Раздел
            # ИИ» — из real_apps ниже она убирается, чтобы не дублировалась.
            ("Закреплено", "Настройка ИИ-приложения", "AiAppSettings", aiappsettings_url, (is_staff or user_has_key(request.user, "page.app_settings")), "⚙", "Вкл/выкл доступ к ИИ, последние запросы и пакетные прогоны", "page.app_settings", ()),
            # «Закреплённые пакетные решения» — сразу под «Настройкой ИИ-приложения»
            # (та же беззаголовочная группа «ai-pinned»): закреплённые завершённые
            # пакетные прогоны (★ в журнале и на «Настройке ИИ-приложения»). Права —
            # Права — те же, что у журнала (page.logs_own/logs_all), плюс
            # собственный ключ page.pinned_runs (группы выдают и без журнала).
            ("Закреплено", "Закреплённые пакетные решения", "AiPinnedBatchRuns", "/ai/admin/pinned-runs/", show_pinned, "★", "Закреплённые завершённые пакетные прогоны", "page.pinned_runs", ()),
            # Доска «Таски» — прямо под закреплёнными пакетными решениями
            # (ТЗ 2026-10-19): суперюзер создаёт/закрывает, остальные берут;
            # вкладки «Фронт работы» (числовой приоритет, перетаскивание)
            # и «Хотелки». Действия доски — отдельные ключи (аккордеон
            # страницы в «Группах доступа»): создать/закрыть/переставить.
            ("Закреплено", "Таски", "AiDevTask", tasks_url, show_tasks, "▦", "Доска задач: фронт работы + хотелки", "page.tasks", (
                ("action.task_create", "Создавать таски"),
                ("action.task_close", "Закрывать таски / возвращать на доску"),
                ("action.task_reorder", "Менять порядок фронта"),
            )),
            # NB: остальные дубликаты реальных ModelAdmin-строк («Промпты»)
            # сюда НЕ добавляем — они и так есть в группе «Раздел ИИ» ниже.
            # Здесь — только кастомные инструменты. «Мой промпт» гейтится
            # ключом page.prompt_admin (тот же, что у ModelAdmin-промптов:
            # пишущий доступ — свои; «чужое» — вложенный ключ
            # page.prompt_admin.edit_others в «Группах доступа»).
            ("Промпты", "Мой промпт", "AiMyPrompt", my_prompt_url, (is_pd or user_has_key(request.user, "page.prompt_admin")), "✎", "Свои и закреплённые промпты", "page.prompt_admin", (
                ("page.prompt_admin_all", "Видеть все промпты (читать чужие)"),
                ("page.prompt_admin.edit_others", "Перезаписывать чужие промпты"),
            )),
            ("Промпты", "Промпты по умолчанию", "AiArmPromptDefaults", "/ai/admin/prompt-defaults/", can_access_prompt_defaults(request), "⚙", "Авто-подстановка по языку/теме ARM", "page.prompt_defaults", ()),
            ("Документация", "Разработчик промптов", "AiDocsPromptDeveloper", docs_url + "prompt-developer/", can_access_prompt_developer_docs(user), "✎", "Инструкция для разработчика промптов", "docs.prompt_developer", ()),
            ("Документация", "Разработчик", "AiDocsDeveloper", docs_url + "developer/", (is_staff or user_has_key(request.user, "docs.developer")), "⚙", "Документация для разработчика", "docs.developer", ()),
            ("Документация", "Суперюзер", "AiDocsSuperuser", docs_url + "superuser/", user_has_key(request.user, "docs.superuser"), "⚙", "Инструкция для суперадмина", "docs.superuser", ()),
            ("ARM", "Пакетное решение", "AiArmSolve", "/ai/admin/arm/solve/", show_arm, "▤", "Пакетный прогон моделей по задачам DL", "page.arm_solve", ()),
            ("ARM", "Состояние моделей", "AiModelStatus", "/ai/admin/arm/models/", show_model_status, "◉", "Доступность AI-моделей сегодня", "page.model_status", ()),
            # Журнал — родительский ключ page.logs («читать свои»), «читать
            # все» — вложенный page.logs_all (легаси-ключи page.logs_own —
            # синоним родителя, проверки их признают).
            ("Диагностика", "Журнал запросов", "AiRequestLogs", "/ai/admin/ai/request_logs/", show_logs, "≣", "Все запросы к моделям, с поиском", "page.logs", (
                ("page.logs_all", "Читать все записи"),
            )),
            ("Диагностика", "Тестовая консоль", "AiTestConsole", "/ai/admin/test-console/", show_test_console, "⌘", "Запуск набора тестов приложения", "page.test_console", (
                ("action.test_console_run", "Запускать прогоны"),
            )),
            ("Диагностика",
             "Тестовые данные",
             "AiTestData",
             test_data_url,
             show_test_data, "⚙", "Создание и редактирование тестовых данных", "page.test_data", ()),
            # «Контроль токенов» — расход токенов (промпт + ответ) по моделям,
            # пользователям и режимам. Ключ page.tokens НЕ в baseline: страница
            # показывает расход ВСЕХ пользователей — выдаётся группами.
            ("Диагностика", "Контроль токенов", "AiTokenUsage", "/ai/admin/ai/tokens/", can_access_token_usage(request), "◔", "Сколько токенов потрачено: модели, кто, режим", "page.tokens", ()),
            # «Мои AI-модели» — личные модели пользователя (провайдер + модель +
            # свой ключ). Видны и правятся ТОЛЬКО владельцем.
            ("Система", "Мои AI-модели", "AiUserModels", "/ai/admin/user-models/", can_access_user_models(request), "☁", "Свои AI-модели: провайдер, модель, ключ", "page.user_models", ()),
            # «Запросы по DL API» — заявки пользователей на создание API к
            # dl.gsu.by: «актуальные» (API ещё не создан) и «неактуальные».
            ("Система", "Запросы по DL API", "AiDLApiRequests", "/ai/admin/dl-api-requests/", can_access_dl_api_requests(request), "☏", "Заявки на создание API к dl.gsu.by", "page.dl_api_requests", ()),
            ("Система", "Обновления", "AiUpdates", "/ai/admin/updates/", show_updates, "↻", "История изменений проекта", "page.updates", (
                ("action.updates_publish", "Публиковать/скрывать записи"),
            )),
            # «Группы доступа» — суперюзеру, либо держателю ключа page.groups
            # (он управляет правами групп, включая собственную).
            ("Система", "Группы доступа", "AiAccessGroups", "/ai/admin/ai-groups/", show_groups, "⛭", "Группы доступа: права страниц, действия, состав", "page.groups", ()),
        ]

    @staticmethod
    def _user_display_name(user):
        """Человекочитаемое имя для приветствия в шапке админки.

        Логика — в общем сервисе (ai/services/user_directory.py), чтобы
        админка, списки доступа и поиск получателя показывали одно и то же.
        """
        from ..services.user_directory import display_name_for_user

        return display_name_for_user(user)

    @staticmethod
    def _user_role_label(user, is_super, is_staff, is_pd):
        """Короткая русская метка роли для шапки админки.

        «Разработчик» (встроенная AIAccessGroup) — senior-метка над
        «Разработчиком промптов»: member обеих групп видит высшую.
        """
        if is_super:
            return "Суперпользователь"
        if is_staff:
            return "Администратор"
        if is_developer_user(user):
            return "Разработчик"
        if is_pd:
            return "Разработчик промптов"
        return "Пользователь"

    @staticmethod
    def _user_dl_id(user):
        """DL-идентификатор пользователя для приветствия «[id] ФИО».

        Логика — в общем сервисе (ai/services/user_directory.py): сначала
        ``external_user_id``, затем username без префикса ``user_``, затем pk.
        """
        from ..services.user_directory import dl_id_for_user

        return dl_id_for_user(user)

    @staticmethod
    def _aiappsettings_url():
        """Ссылка на страницу «Настройка ИИ-приложения» для навигации.

        Синглтон: если запись уже создана — сразу на её change-форму (именно
        там таблицы последних запросов/прогонов), иначе на changelist, где
        доступно создание.
        """
        from ..models import AIAppSettings

        try:
            pk = AIAppSettings.objects.values_list("pk", flat=True).first()
        except Exception:  # например, таблица ещё не создана при migrate
            return "/ai/admin/ai/aiappsettings/"
        if pk:
            return f"/ai/admin/ai/aiappsettings/{pk}/change/"
        return "/ai/admin/ai/aiappsettings/"

    def _build_ai_nav_apps(self, request, *, tools):
        """Build fake "app" groups (for the left nav) from a list of tools.

        ``tools`` is a list of ``(group, label, object_name, url, visible,
        icon, hint)`` tuples (icon/hint optional). Groups with no visible tool
        are omitted. The returned dicts mimic ``AdminSite._build_app_dict`` so
        our project override of ``admin/app_list.html`` renders them (grouped,
        sorted, iconised) with per-tool ``current-model`` highlight via
        ``admin_url``.

        ``app_url`` is set to ``"#"`` deliberately: the group heading is a
        label, not a navigation target. The stock ``current-app`` state would
        otherwise match EVERY admin page (all paths start with ``/ai/admin/``),
        keeping both tool groups permanently bold. ``"#"`` never matches the
        path, so only the active *tool* row highlights via ``current-model``.

        The injected groups are prefixed with ``ai`` in their ``app_label`` so
        our custom ``app_list.html`` can render them as first-class AI sections
        ahead of the stock model apps.
        """
        group_url = "#"

        # Explicit ordering of tool groups in the left nav (top → bottom).
        # NB: здесь ИМЕНА групп (первый элемент кортежей tools), а не их
        # app_label из label_to_key — dict groups ключуется именем.
        # «Закреплено» (app_label ai-pinned) — беззаголовочная группа,
        # закреплённая самым первым пунктом навигации (шаблон
        # admin/app_list.html не рисует для неё title).
        group_order = ["Закреплено", "Промпты", "Документация", "ARM", "Диагностика", "Система"]
        label_to_key = {
            "Закреплено": "ai-pinned",
            "Промпты": "ai-tools-prompts",
            "Документация": "ai-docs",
            "ARM": "ai-tools-arm",
            "Диагностика": "ai-tools-diag",
            "Система": "ai-tools-system",
        }
        groups: dict[str, dict] = {}
        for tool in tools:
            group, label, object_name, url, visible = tool[:5]
            icon = tool[5] if len(tool) > 5 else ""
            hint = tool[6] if len(tool) > 6 else ""
            if not visible or object_name in _HIDDEN_NAV_OBJECT_NAMES:
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
                "icon": icon,
                "hint": hint,
            })
        return [groups[g] for g in group_order if g in groups]


ai_admin_site = AIAdminSite(name="admin")
