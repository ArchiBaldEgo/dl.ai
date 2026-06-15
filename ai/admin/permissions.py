"""Permission helpers shared across AI admin views and model admins."""

from ..constants import PROMPT_DEVELOPER_GROUP


def is_prompt_developer_user(user):
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_active", True) is False:
        return False
    return user.groups.filter(name=PROMPT_DEVELOPER_GROUP).exists()


def is_staff_or_superuser(user):
    if not user:
        return False
    if getattr(user, "is_active", True) is False:
        return False
    return bool(user.is_superuser or user.is_staff)


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


def filter_app_list_for_user(app_list, request):
    """Return app_list with models the current user cannot access stripped out.

    Apps that end up with no visible models and no custom links are dropped
    entirely so they do not render as empty sections in the sidebar.
    """
    if not app_list:
        return app_list

    is_pd = is_prompt_developer_user(getattr(request, "user", None))
    is_staff = is_staff_or_superuser(getattr(request, "user", None))
    has_any_custom_link = bool(
        getattr(request, "_show_arm_link_cached", None)
        or is_pd
    )

    registry = getattr(request, "_ai_admin_registry", None)
    filtered = []
    for app in app_list:
        app_label = app.get("app_label") if isinstance(app, dict) else getattr(app, "app_label", None)
        visible_models = []
        for model in app.get("models", []):
            # Django stores ModelAdmin by model *class* in _registry.
            # app_list entries use the string "object_name" though, so
            # look up by class if available, otherwise by name.
            model_cls = model.get("_model_cls") if isinstance(model, dict) else None
            model_admin = None
            if registry is not None:
                if model_cls is not None:
                    model_admin = registry.get(model_cls)
                if model_admin is None and model.get("object_name"):
                    for cls, admin_obj in registry.items():
                        if cls.__name__ == model["object_name"]:
                            model_admin = admin_obj
                            break
            if model_admin is None or model_admin.has_module_permission(request):
                visible_models.append(model)
        new_app = dict(app)
        new_app["models"] = visible_models
        if not visible_models and app_label != "ai":
            # Drop empty non-AI apps (e.g., "Authentication and Authorization" for non-staff).
            continue
        if not visible_models and app_label == "ai" and not (is_pd or is_staff):
            # The AI app shows custom links; keep it only if the user can see any of them.
            if has_any_custom_link:
                filtered.append(new_app)
            continue
        filtered.append(new_app)
    return filtered
