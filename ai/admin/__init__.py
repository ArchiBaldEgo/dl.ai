"""AI admin package — thin aggregator.

The real admin site lives in ai.admin.site.ai_admin_site and is wired in
DjangoTest.urls through ai.admin.urls.get_ai_admin_urls().
"""
from django.contrib.auth import get_user_model

from .site import ai_admin_site
from ..models import AIAppSettings, ProgrammingLanguage, Prompt, SharedPrompt, Topic
from .models import (
    AIAppSettingsAdmin,
    ProgrammingLanguageAdmin,
    TopicAdmin,
    PromptAdmin,
    RestrictedUserAdmin,
    SharedPromptAdmin,
)
from .forms import PromptForm, SharedPromptForm
from .logs import AIRequestLogAdmin, admin_request_log_detail_view, admin_request_logs_view
from ..models import AIRequestLog
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
    "AIAppSettingsAdmin",
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

# Register AI models on the custom admin site so they appear in /ai/admin/.
ai_admin_site.register(AIAppSettings, AIAppSettingsAdmin)
ai_admin_site.register(ProgrammingLanguage, ProgrammingLanguageAdmin)
ai_admin_site.register(Topic, TopicAdmin)
ai_admin_site.register(Prompt, PromptAdmin)
ai_admin_site.register(SharedPrompt, SharedPromptAdmin)
ai_admin_site.register(AIRequestLog, AIRequestLogAdmin)

User = get_user_model()
ai_admin_site.register(User, RestrictedUserAdmin)
