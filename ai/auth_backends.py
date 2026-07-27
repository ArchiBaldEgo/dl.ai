"""Бэкенды аутентификации и вспомогательные функции для внешней авторизации.

Содержит:
- AdminExternalAuthBackend: бэкенд Django для входа в админку по external userId.
- normalize_external_user_id: нормализация внешнего ID пользователя.
- get_external_user_id_from_request: извлечение external userId из запроса.
- ensure_prompt_developer_group: добавление пользователя в группу prompt_developer.
- get_admin_user_by_external_id: поиск Django-пользователя по external ID.
- create_admin_user_with_password: создание пользователя с паролем (первичная регистрация).
"""

import os
from urllib.parse import unquote

from django.contrib.auth import get_user_model
from django.contrib.auth.backends import BaseBackend
from django.contrib.auth.models import Group

from .constants import PROMPT_DEVELOPER_GROUP


ADMIN_EXTERNAL_AUTH_BACKEND = "ai.auth_backends.AdminExternalAuthBackend"


def normalize_external_user_id(value):
    """Нормализует внешний ID пользователя: strip, пустые значения → ''."""
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

    # NOTE: Query-params (?uid=, ?userId=) are intentionally NOT trusted
    # as identity sources — they allowed impersonation via /ai/chat/?uid=admin.
    # Only DLSID-validated user_info and signed cookies are accepted.

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
    """Добавляет пользователя в группу prompt_developer (создаёт группу при необходимости)."""
    group, _ = Group.objects.get_or_create(name=PROMPT_DEVELOPER_GROUP)
    user.groups.add(group)
    return group


def get_admin_user_by_external_id(external_user_id):
    """Return the Django user mapped to ``external_user_id``.

    ``ExternalDLAccount`` is the single source of truth for the dl.gsu.by
    user mapping. A Django username matching the external id is NOT used
    as a shortcut: such a username can belong to a completely different
    account and caused an infinite redirect loop between ``/ai/admin/``
    and ``/ai/admin/set-password/``.
    """
    external_user_id = normalize_external_user_id(external_user_id)
    if not external_user_id:
        return None

    from .models import ExternalDLAccount

    account = (
        ExternalDLAccount.objects.select_related("user")
        .filter(external_user_id=external_user_id)
        .first()
    )
    return account.user if account else None


def create_admin_user_with_password(external_user_id, password):
    """Создаёт Django-пользователя с заданным паролем для внешнего ID.

    Если пользователь уже существует, но не имеет пароля — устанавливает пароль.
    Добавляет пользователя в группу prompt_developer.
    """
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
    """Бэкенд аутентификации Django Admin по внешнему userId (dl.gsu.by).

    Первичная регистрация пароля выполняется через set-password view.
    Этот бэкенд используется для входа уже зарегистрированных пользователей.
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
