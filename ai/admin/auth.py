"""Custom admin authentication form and helpers."""

from django import forms
from django.contrib.auth import authenticate, login as auth_login, logout as auth_logout
from django.contrib.admin.forms import AdminAuthenticationForm
from django.middleware import csrf
from django.shortcuts import redirect
from django.utils.http import urlencode

from ..constants import ADMIN_LOGOUT_COOKIE_NAME, PROMPT_DEVELOPER_GROUP
from ..auth_backends import (
    ADMIN_EXTERNAL_AUTH_BACKEND,
    ensure_prompt_developer_group,
    get_external_user_id_from_request,
)
from ..http_utils import safe_relative_url
from ..models import ExternalDLAccount
from .permissions import is_prompt_developer_user, is_staff_or_superuser


def _is_admin_logout_forced(request):
    return bool(request.COOKIES.get(ADMIN_LOGOUT_COOKIE_NAME))


def _is_admin_password_setup_path(request):
    return request.path.startswith("/ai/admin/set-password/")


def _is_admin_auth_service_path(request):
    return (
        request.path.startswith("/ai/admin/login/")
        or request.path.startswith("/ai/admin/logout/")
        or _is_admin_password_setup_path(request)
    )


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
    """Автоматически логинит пользователя, если middleware предоставил user_info."""
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


def _external_admin_entry_response(request):
    """Перехватывает вход в админку с внешней авторизации."""
    if request.method != "GET" or _is_admin_auth_service_path(request):
        return None

    if _is_admin_logout_forced(request):
        if request.session.get("admin_manual_login"):
            return None
        next_path = urlencode({"next": request.get_full_path()}, safe="/?=&")[5:]
        return redirect(f"/ai/admin/login/?{next_path}")

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

    next_path = urlencode({"next": request.get_full_path()}, safe="/?=&")[5:]
    return redirect(f"/ai/admin/set-password/?{next_path}")


def _admin_logout_view(request):
    if request.method not in {"GET", "POST"}:
        from django.http import HttpResponseNotAllowed
        return HttpResponseNotAllowed(["GET", "POST"])

    next_candidate = request.POST.get("next") if request.method == "POST" else request.GET.get("next")
    next_url = safe_relative_url(next_candidate, "/ai/admin/")
    next_path = urlencode({"next": next_url}, safe="/?=&")[5:]

    auth_logout(request)
    response = redirect(f"/ai/admin/login/?{next_path}")
    response.set_cookie(ADMIN_LOGOUT_COOKIE_NAME, "1", path="/ai/admin/", samesite="Lax")
    return response


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

        if is_staff_or_superuser(user) or is_prompt_developer_user(user):
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
