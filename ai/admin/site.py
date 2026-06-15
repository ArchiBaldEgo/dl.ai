"""Custom admin site for the AI app (replaces monkey-patching admin.site)."""

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
    external_id_matches_session,
    has_external_session,
    is_prompt_developer_user,
    is_staff_or_superuser,
)


def _is_admin_login_path(path: str) -> bool:
    normalized = (path or "/").rstrip("/") or "/"
    return normalized == "/ai/admin/login"


def _is_admin_logout_path(path: str) -> bool:
    normalized = (path or "/").rstrip("/") or "/"
    return normalized == "/ai/admin/logout"


def _is_admin_set_password_path(path: str) -> bool:
    normalized = (path or "/").rstrip("/") or "/"
    return normalized == "/ai/admin/set-password"


def _redirect_to_dl(request):
    url = os.getenv("EXTERNAL_AUTH_REDIRECT_URL", "https://dl.gsu.by")
    return redirect(url)


class AIAdminSite(admin.AdminSite):
    site_header = "AI Admin"
    site_title = "AI Admin"
    index_template = "admin/ai/index.html"
    app_index_template = "admin/ai/app_index.html"
    site_url = "/ai/chat/"

    def has_permission(self, request):
        user = getattr(request, "user", None)
        if not user or not user.is_authenticated or not user.is_active:
            return False
        # Login / logout / set-password pages are reachable without
        # user_info: middleware treats the whole admin tree as an
        # optional auth path, so DLSID may be absent on first load.
        if _is_admin_login_path(request.path) or _is_admin_logout_path(request.path):
            return False
        if _is_admin_set_password_path(request.path):
            # set-password needs user_info to know which account to set
            # the password for, but no session match is required.
            return has_external_session(request)
        if not has_external_session(request):
            return False
        if not external_id_matches_session(request):
            return False
        return can_access_admin(user)

    def admin_view(self, view, cacheable=False):
        wrapped_view = super().admin_view(view, cacheable)

        def inner(request, *args, **kwargs):
            # Drop the user back to dl.gsu.by when the DLSID is gone or
            # points to a different person than the local session.
            if request.user.is_authenticated and not _is_admin_login_path(request.path):
                if not has_external_session(request) or not external_id_matches_session(request):
                    auth_logout(request)
                    return _redirect_to_dl(request)

            if request.user.is_authenticated and not request.session.get("admin_fresh_auth"):
                if has_external_session(request) and external_id_matches_session(request):
                    if is_prompt_developer_user(request.user) or is_staff_or_superuser(request.user):
                        request.session["admin_fresh_auth"] = True

            if request.user.is_authenticated and (not request.user.has_usable_password()):
                if not _is_admin_set_password_path(request.path) and request.method == "GET":
                    next_path = urlencode({"next": request.get_full_path()}, safe="/?=&")[5:]
                    return redirect(f"/ai/admin/set-password/?{next_path}")

            if request.user.is_authenticated and not request.session.get("admin_fresh_auth"):
                next_path = urlencode({"next": request.get_full_path()}, safe="/?=&")[5:]
                return redirect(f"/ai/admin/login/?{next_path}")

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
        """Block the built-in admin login form — the only entry is DLSID.

        Forward the original ``?next=`` to dl.gsu.by so the user comes
        back to the page they were trying to reach once they re-auth on
        the main site.
        """
        from django.utils.http import urlencode
        from ..http_utils import safe_relative_url
        next_url = safe_relative_url(request.GET.get("next"), "/ai/admin/")
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
        context["is_prompt_developer"] = is_pd
        context["is_staff_or_superuser"] = is_staff
        context["show_arm_link"] = can_access_arm(request)
        context["show_model_status_link"] = can_access_model_status(request)
        context["show_prompt_link"] = can_access_prompt_admin(request)
        context["show_logs_link"] = can_access_logs(request)
        context["arm_find_error_url"] = "/ai/admin/arm/find-error/"
        context["arm_model_status_url"] = "/ai/admin/arm/models/"
        context["arm_model_status_refresh_url"] = "/ai/admin/arm/models/refresh/"
        context["arm_model_status_state_url"] = "/ai/admin/arm/models/state/"
        context["prompt_admin_url"] = "/ai/admin/ai/prompt/"
        context["my_prompt_url"] = "/ai/admin/prompts/my/"
        context["my_prompt_change_url"] = get_my_prompt_admin_url(request)
        context["ai_logs_url"] = "/ai/admin/ai/airequestlog/"
        return context


ai_admin_site = AIAdminSite(name="admin")
