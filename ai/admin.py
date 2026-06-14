"""Backward-compatible shim for ai/admin/__init__.

Imports are kept here so that code referencing ai.admin.PromptAdmin,
ai.admin.TesterOrStaffAdminAuthenticationForm, etc. continues to work.
"""
from .admin.auth import TesterOrStaffAdminAuthenticationForm, _admin_logout_view
from .admin.models import ProgrammingLanguageAdmin, TopicAdmin, PromptAdmin, SharedPromptAdmin
from .admin.logs import AIRequestLogAdmin
from .admin.models import AIAppSettingsAdmin
from .admin.arm import (
    admin_arm_find_error_view,
    admin_arm_find_error_start_view,
    admin_arm_find_error_status_view,
)
from .admin.model_status import (
    admin_model_status_view,
    admin_model_status_state_view,
    admin_model_status_refresh_view,
)
from .admin.my_prompt import admin_my_prompt_view
from .admin.logs import admin_request_logs_view

__all__ = [
    "TesterOrStaffAdminAuthenticationForm",
    "_admin_logout_view",
    "ProgrammingLanguageAdmin",
    "TopicAdmin",
    "PromptAdmin",
    "SharedPromptAdmin",
    "AIRequestLogAdmin",
    "AIAppSettingsAdmin",
    "admin_arm_find_error_view",
    "admin_arm_find_error_start_view",
    "admin_arm_find_error_status_view",
    "admin_model_status_view",
    "admin_model_status_state_view",
    "admin_model_status_refresh_view",
    "admin_my_prompt_view",
    "admin_request_logs_view",
]
