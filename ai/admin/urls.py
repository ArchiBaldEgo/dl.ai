"""Custom URL wiring for the AI admin site."""

from django.urls import include, path

from ..views import set_password_view
from .arm import (
    admin_arm_find_error_view,
    admin_arm_find_error_start_view,
    admin_arm_find_error_status_view,
)
from .auth import _admin_logout_view
from .logs import admin_request_logs_view, admin_request_log_detail_view
from .model_status import (
    admin_model_status_view,
    admin_model_status_refresh_view,
    admin_model_status_state_view,
)
from .my_prompt import admin_my_prompt_view
from .site import ai_admin_site


def get_ai_admin_urls():
    """Return the AI admin URL patterns.

    This function is called by DjangoTest.urls when wiring admin URLs.
    """
    custom_urls = [
        path("logout/", _admin_logout_view, name="logout"),
        path("set-password/", set_password_view, name="set_password_view"),
        path("arm/find-error/start/", ai_admin_site.admin_view(admin_arm_find_error_start_view), name="ai_arm_find_error_start"),
        path("arm/find-error/status/", ai_admin_site.admin_view(admin_arm_find_error_status_view), name="ai_arm_find_error_status"),
        path("arm/models/refresh/", ai_admin_site.admin_view(admin_model_status_refresh_view), name="ai_arm_model_status_refresh"),
        path("arm/models/state/", ai_admin_site.admin_view(admin_model_status_state_view), name="ai_arm_model_status_state"),
        path("arm/models/", ai_admin_site.admin_view(admin_model_status_view), name="ai_arm_model_status"),
        path("arm/find-error/", ai_admin_site.admin_view(admin_arm_find_error_view), name="ai_arm_find_error"),
        path("prompts/my/", ai_admin_site.admin_view(admin_my_prompt_view), name="ai_my_prompt"),
        path("logs/<int:log_id>/", ai_admin_site.admin_view(admin_request_log_detail_view), name="ai_request_log_detail"),
        path("logs/", ai_admin_site.admin_view(admin_request_logs_view), name="ai_request_logs"),
    ]
    return [
        path("", include((ai_admin_site.get_urls(), ai_admin_site.name), namespace=ai_admin_site.name)),
        *custom_urls,
    ]
