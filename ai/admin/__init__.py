"""AI admin package — thin aggregator.

The real admin site lives in ai.admin.site.ai_admin_site and is wired in
DjangoTest.urls through ai.admin.urls.get_ai_admin_urls().
"""
from .site import ai_admin_site
from .models import ProgrammingLanguageAdmin, TopicAdmin, PromptAdmin, SharedPromptAdmin
from .forms import PromptForm, SharedPromptForm
from .logs import AIRequestLogAdmin, admin_request_log_detail_view, admin_request_logs_view
from .arm import (
    admin_arm_find_error_view,
    admin_arm_find_error_start_view,
    admin_arm_find_error_status_view,
)
from .model_status import (
    admin_model_status_view,
    admin_model_status_state_view,
    admin_model_status_refresh_view,
)
from .my_prompt import admin_my_prompt_view
from .auth import _external_admin_entry_response

__all__ = [
    "ai_admin_site",
    "ProgrammingLanguageAdmin",
    "TopicAdmin",
    "PromptAdmin",
    "SharedPromptAdmin",
    "PromptForm",
    "SharedPromptForm",
    "AIRequestLogAdmin",
    "admin_arm_find_error_view",
    "admin_arm_find_error_start_view",
    "admin_arm_find_error_status_view",
    "admin_model_status_view",
    "admin_model_status_state_view",
    "admin_model_status_refresh_view",
    "admin_my_prompt_view",
    "admin_request_logs_view",
    "_external_admin_entry_response",
]
