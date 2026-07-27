"""
URL-маршруты проекта DjangoTest.

Содержит:
- health-check эндпоинт (/health)
- AI admin (/ai/admin/) с кастомным admin site
- AI app URLs (страницы чата, решения задач, поиска ошибок)
- AI API эндпоинты (языки, темы, промпты, данные задач dl.gsu.by)
- static files
"""
from django.conf import settings
from django.conf.urls.static import static
from django.urls import path, include

from ai.admin.site import ai_admin_site
from ai.admin.urls import get_ai_admin_urls
from ai.views import (
    get_languages,
    get_topics,
    get_prompts,
    get_shared_prompts,
    get_problem_data,
    get_task_info_view,
    get_task_solution_view,
    health_view,
    set_password_view,
    get_groq_limits_view,
)

urlpatterns = [
    path('health', health_view, name='health'),
    path('ai/admin/', include(get_ai_admin_urls())),
    path('', include('ai.urls')),
    path('ai/api/languages/', get_languages, name='get_languages'),
    path('ai/api/topics/', get_topics, name='get_topics'),
    path('ai/api/shared-prompts/', get_shared_prompts, name='get_shared_prompts'),
    path('ai/api/prompts/', get_prompts, name='get_prompts'),
    path('ai/api/problem-data/', get_problem_data, name='get_problem_data'),
    path('ai/api/task-info/', get_task_info_view, name='get_task_info'),
    path('ai/api/task-solution/', get_task_solution_view, name='get_task_solution'),
    path('ai/api/groq-limits/', get_groq_limits_view, name='get_groq_limits'),

] + static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
