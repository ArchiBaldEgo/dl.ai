"""Permission helpers shared across AI admin views and model admins."""

from ..constants import PROMPT_DEVELOPER_GROUP


def is_prompt_developer_user(user):
    if not user or not getattr(user, "is_authenticated", False):
        return False
    return user.groups.filter(name=PROMPT_DEVELOPER_GROUP).exists()


def is_staff_or_superuser(user):
    return bool(user and (user.is_superuser or user.is_staff))


def can_access_admin(user):
    return is_staff_or_superuser(user) or is_prompt_developer_user(user)


def can_access_arm(request):
    if not request.user.is_authenticated:
        return False
    return is_staff_or_superuser(request.user) or is_prompt_developer_user(request.user)


def can_access_model_status(request):
    if not request.user.is_authenticated:
        return False
    return request.user.is_superuser or request.user.is_staff


def can_access_prompt_admin(request):
    if not request.user.is_authenticated:
        return False
    if is_staff_or_superuser(request.user):
        return True
    return is_prompt_developer_user(request.user)


def can_access_logs(request):
    return request.user.is_authenticated and is_staff_or_superuser(request.user)
