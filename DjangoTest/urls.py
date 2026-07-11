"""
URL configuration for DjangoTest project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
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
    send_solution_view,
    get_solution_result_view,
    health_view,
    set_password_view,
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
    path('ai/api/send-solution/', send_solution_view, name='send_solution'),
    path('ai/api/solution-result/', get_solution_result_view, name='get_solution_result'),

] + static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
