"""Custom admin site for the AI app (replaces monkey-patching admin.site)."""

from django.contrib import admin
from django.contrib.auth import logout as auth_logout
from django.shortcuts import redirect
from django.urls import path
from django.utils.http import urlencode

from ..constants import ADMIN_LOGOUT_COOKIE_NAME
from ..http_utils import safe_relative_url
from .auth import TesterOrStaffAdminAuthenticationForm, _external_admin_entry_response
from .permissions import can_access_arm, can_access_logs, can_access_model_status, can_access_prompt_admin


class AIAdminSite(admin.AdminSite):
    site_header = "AI Admin"
    site_title = "AI Admin"
    index_template = "admin/ai/index.html"
    app_index_template = "admin/ai/app_index.html"
    login_form = TesterOrStaffAdminAuthenticationForm
    site_url = "/ai/chat/"

    def has_permission(self, request):
        from .auth import _auto_login_from_external, _is_admin_logout_forced
        from .permissions import is_prompt_developer_user, is_staff_or_superuser

        if request.path.startswith("/ai/admin/login/"):
            if request.method != "POST":
                if _is_admin_logout_forced(request):
                    if request.user.is_authenticated:
                        auth_logout(request)
                    request.session.pop("admin_fresh_auth", None)
                    return False
                if request.user.is_authenticated and request.session.get("admin_fresh_auth"):
                    return True
                if hasattr(request, 'user_info') and _auto_login_from_external(request):
                    return True
                if request.user.is_authenticated:
                    auth_logout(request)
                request.session.pop("admin_fresh_auth", None)
            return False

        user = getattr(request, "user", None)
        if not user or not user.is_authenticated or not user.is_active:
            return False
        return is_staff_or_superuser(user)

    def admin_view(self, view, cacheable=False):
        wrapped_view = super().admin_view(view, cacheable)

        def inner(request, *args, **kwargs):
            from .auth import _is_admin_password_setup_path
            from .permissions import is_prompt_developer_user, is_staff_or_superuser

            external_entry_response = _external_admin_entry_response(request)
            if external_entry_response is not None:
                return external_entry_response

            if request.user.is_authenticated and not request.session.get("admin_fresh_auth"):
                if hasattr(request, 'user_info') or is_prompt_developer_user(request.user):
                    request.session["admin_fresh_auth"] = True

            if request.user.is_authenticated and (not request.user.has_usable_password()):
                if not request.path.startswith("/ai/admin/set-password/") and request.method == "GET":
                    next_path = urlencode({"next": request.get_full_path()}, safe="/?=&")[5:]
                    return redirect(f"/ai/admin/set-password/?{next_path}")

            if request.user.is_authenticated and not request.session.get("admin_fresh_auth"):
                next_path = urlencode({"next": request.get_full_path()}, safe="/?=&")[5:]
                return redirect(f"/ai/admin/login/?{next_path}")

            response = wrapped_view(request, *args, **kwargs)
            if request.session.pop("admin_manual_login", None):
                response.delete_cookie(ADMIN_LOGOUT_COOKIE_NAME, path="/ai/admin/")
            return response

        return inner

    def each_context(self, request):
        from .my_prompt import get_my_prompt_admin_url
        from .permissions import is_prompt_developer_user, is_staff_or_superuser
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
