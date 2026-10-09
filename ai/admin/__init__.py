"""AI admin package — thin aggregator.

The real admin site lives in ai.admin.site.ai_admin_site and is wired in
DjangoTest.urls through ai.admin.urls.get_ai_admin_urls().
"""
from django.contrib.auth import get_user_model

from .site import ai_admin_site
from ..models import (
    AIAppSettings,
    AuthorAlias,
    ExternalDLAccount,
    ProgrammingLanguage,
    Prompt,
    PromptTestCase,
    PromptTestRun,
    AIWizardSeen,
    SharedPrompt,
    Task,
    TaskSolution,
    Topic,
    UpdateLog,
)
from .models import (
    AIAppSettingsAdmin,
    AuthorAliasAdmin,
    ExternalDLAccountAdmin,
    ProgrammingLanguageAdmin,
    TopicAdmin,
    PromptAdmin,
    PromptTestCaseAdmin,
    PromptTestRunAdmin,
    AIWizardSeenAdmin,
    RestrictedUserAdmin,
    SharedPromptAdmin,
    TaskAdmin,
    TaskSolutionAdmin,
    UpdateLogAdmin,
)
from .forms import PromptForm, SharedPromptForm
from .logs import AIRequestLogAdmin, admin_request_log_detail_view, admin_request_logs_view, resend_request_view
from ..models import AIRequestLog
from .arm import (
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
    "AuthorAliasAdmin",
    "ProgrammingLanguageAdmin",
    "TopicAdmin",
    "PromptAdmin",
    "PromptTestCaseAdmin",
    "PromptTestRunAdmin",
    "AIWizardSeenAdmin",
    "SharedPromptAdmin",
    "TaskAdmin",
    "TaskSolutionAdmin",
    "UpdateLogAdmin",
    "PromptForm",
    "SharedPromptForm",
    "AIRequestLogAdmin",
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
    "resend_request_view",
]

# Register AI models on the custom admin site so they appear in /ai/admin/.
ai_admin_site.register(AIAppSettings, AIAppSettingsAdmin)
ai_admin_site.register(AuthorAlias, AuthorAliasAdmin)
ai_admin_site.register(ProgrammingLanguage, ProgrammingLanguageAdmin)
ai_admin_site.register(Task, TaskAdmin)
ai_admin_site.register(Topic, TopicAdmin)
ai_admin_site.register(Prompt, PromptAdmin)
ai_admin_site.register(SharedPrompt, SharedPromptAdmin)
ai_admin_site.register(PromptTestCase, PromptTestCaseAdmin)
ai_admin_site.register(PromptTestRun, PromptTestRunAdmin)
ai_admin_site.register(ExternalDLAccount, ExternalDLAccountAdmin)
ai_admin_site.register(TaskSolution, TaskSolutionAdmin)
# NOTE: AIWizardSeen — листинг «кто видел онбординг-wizard» (только суперюзер);
# удаление строки перезапускает тур этого scope конкретному пользователю.
ai_admin_site.register(AIWizardSeen, AIWizardSeenAdmin)
# NOTE: UpdateLog is registered superuser-only (publish/hide hidden rows); it is
# kept OUT of the left nav via _HIDDEN_NAV_OBJECT_NAMES — the custom «Обновления»
# page links to /ai/admin/ai/updatelog/ (Скрытые записи).
ai_admin_site.register(UpdateLog, UpdateLogAdmin)
# NOTE: AIRequestLog is intentionally NOT registered as a ModelAdmin. Its
# changelist URL (/ai/admin/ai/airequestlog/) is served by the custom
# admin_request_logs_view (ai/admin/urls.py), which renders the richer
# request_logs.html UI. Registering it too shadowed that view and left the
# ModelAdmin's add/change/delete orphaned. The "Логи запросов" nav row points
# at the custom view.

User = get_user_model()
ai_admin_site.register(User, RestrictedUserAdmin)
