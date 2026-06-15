import os
from urllib.parse import unquote

from django.contrib.auth import get_user_model
from django.contrib.auth.backends import BaseBackend
from django.contrib.auth.models import Group

from .constants import PROMPT_DEVELOPER_GROUP


ADMIN_EXTERNAL_AUTH_BACKEND = "ai.auth_backends.AdminExternalAuthBackend"


def normalize_external_user_id(value):
    value = "" if value is None else str(value)
    value = value.strip()
    if not value or value == "None":
        return ""
    return value


def get_external_user_id_from_request(request):
    """Return the external userId for the current request.

    Lookup order:
    1. ``request.user_info`` (filled in by ``ExternalAuthMiddleware`` from
       the JSON returned by ``EXTERNAL_AUTH_API_URL``).
    2. The query parameters ``uid`` / ``userId`` — the dl.gsu.by toolbar
       embeds the user id directly in the link (e.g. ``/ai/chat/?uid=...``).
    3. A cookie. Defaults to ``userId`` (overridable via
       ``EXTERNAL_USER_ID_COOKIE_NAME``), with ``user_id`` and ``userid`` as
       fallbacks. ``DLID`` is also accepted because that is the actual cookie
       the legacy main site sets for the dl.gsu.by user id.
    """
    user_info = getattr(request, "user_info", None) or {}
    external_user_id = normalize_external_user_id(user_info.get("userId"))
    if external_user_id:
        return external_user_id

    for query_key in ("uid", "userId"):
        external_user_id = normalize_external_user_id(
            request.GET.get(query_key, "")
        )
        if external_user_id:
            return external_user_id

    cookie_names = [
        os.getenv("EXTERNAL_USER_ID_COOKIE_NAME", "userId"),
        "user_id",
        "userid",
        "DLID",
        "dlid",
    ]
    cookies = getattr(request, "COOKIES", None) or {}
    for cookie_name in dict.fromkeys(name for name in cookie_names if name):
        external_user_id = normalize_external_user_id(
            unquote(cookies.get(cookie_name, ""))
        )
        if external_user_id:
            return external_user_id

    return ""


def ensure_prompt_developer_group(user):
    group, _ = Group.objects.get_or_create(name=PROMPT_DEVELOPER_GROUP)
    user.groups.add(group)
    return group


def get_admin_user_by_external_id(external_user_id):
    external_user_id = normalize_external_user_id(external_user_id)
    if not external_user_id:
        return None

    User = get_user_model()
    try:
        return User.objects.get(username=external_user_id)
    except User.DoesNotExist:
        pass

    from .models import ExternalDLAccount

    account = (
        ExternalDLAccount.objects.select_related("user")
        .filter(external_user_id=external_user_id)
        .first()
    )
    return account.user if account else None


def create_admin_user_with_password(external_user_id, password):
    external_user_id = normalize_external_user_id(external_user_id)
    if not external_user_id:
        raise ValueError("userId is required")

    User = get_user_model()
    user, created = User.objects.get_or_create(
        username=external_user_id,
        defaults={"email": ""},
    )
    if created or not user.has_usable_password():
        user.set_password(password)
        user.save(update_fields=["password"])

    ensure_prompt_developer_group(user)
    return user


class AdminExternalAuthBackend(BaseBackend):
    """
    Authenticates already-authorized site users in Django Admin by external userId.
    The first password registration is handled by the admin set-password view.
    """

    def authenticate(self, request, external_user_id=None, **kwargs):
        user = get_admin_user_by_external_id(external_user_id)
        if not user or not user.is_active:
            return None

        ensure_prompt_developer_group(user)
        return user

    def get_user(self, user_id):
        User = get_user_model()
        try:
            return User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return None
