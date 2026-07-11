"""AI admin package — thin aggregator.

The real admin site lives in ai.admin.site.ai_admin_site and is wired in
DjangoTest.urls through ai.admin.urls.get_ai_admin_urls().
"""
from django.contrib.auth import get_user_model

from .site import ai_admin_site
from ..models import (
    AIAppSettings,
    ProgrammingLanguage,
    Prompt,
    PromptTestCase,
    PromptTestRun,
    SharedPrompt,
    Task,
    Topic,
)
from .models import (
    AIAppSettingsAdmin,
    ProgrammingLanguageAdmin,
    TopicAdmin,
    PromptAdmin,
    PromptTestCaseAdmin,
    PromptTestRunAdmin,
    RestrictedUserAdmin,
    SharedPromptAdmin,
    TaskAdmin,
)
from .forms import PromptForm, SharedPromptForm
from .logs import AIRequestLogAdmin, admin_request_log_detail_view, admin_request_logs_view
from ..models import AIRequestLog
from .arm import (
    admin_arm_find_error_view,
    admin_arm_find_error_start_view,
    admin_arm_find_error_status_view,
    admin_arm_solve_view,
    admin_arm_solve_start_view,
    admin_arm_solve_status_view,
)
from .model_status import (
    admin_model_status_view,
    admin_model_status_state_view,
    admin_model_status_refresh_view,
)
from .my_prompt import admin_my_prompt_view
from .prompt_regression import (
    admin_prompt_regression_view,
    admin_prompt_regression_start_view,
    admin_prompt_regression_status_view,
)

__all__ = [
    "ai_admin_site",
    "AIAppSettingsAdmin",
    "ProgrammingLanguageAdmin",
    "TopicAdmin",
    "PromptAdmin",
    "PromptTestCaseAdmin",
    "PromptTestRunAdmin",
    "SharedPromptAdmin",
    "TaskAdmin",
    "PromptForm",
    "SharedPromptForm",
    "AIRequestLogAdmin",
    "admin_arm_find_error_view",
    "admin_arm_find_error_start_view",
    "admin_arm_find_error_status_view",
    "admin_arm_solve_view",
    "admin_arm_solve_start_view",
    "admin_arm_solve_status_view",
    "admin_model_status_view",
    "admin_model_status_state_view",
    "admin_model_status_refresh_view",
    "admin_my_prompt_view",
    "admin_prompt_regression_view",
    "admin_prompt_regression_start_view",
    "admin_prompt_regression_status_view",
    "admin_request_logs_view",
]

# Register AI models on the custom admin site so they appear in /ai/admin/.
ai_admin_site.register(AIAppSettings, AIAppSettingsAdmin)
ai_admin_site.register(ProgrammingLanguage, ProgrammingLanguageAdmin)
ai_admin_site.register(Task, TaskAdmin)
ai_admin_site.register(Topic, TopicAdmin)
ai_admin_site.register(Prompt, PromptAdmin)
ai_admin_site.register(SharedPrompt, SharedPromptAdmin)
ai_admin_site.register(PromptTestCase, PromptTestCaseAdmin)
ai_admin_site.register(PromptTestRun, PromptTestRunAdmin)
# NOTE: AIRequestLog is intentionally NOT registered as a ModelAdmin. Its
# changelist URL (/ai/admin/ai/airequestlog/) is served by the custom
# admin_request_logs_view (ai/admin/urls.py), which renders the richer
# request_logs.html UI. Registering it too shadowed that view and left the
# ModelAdmin's add/change/delete orphaned. The "Логи запросов" nav row points
# at the custom view.

User = get_user_model()
ai_admin_site.register(User, RestrictedUserAdmin)
