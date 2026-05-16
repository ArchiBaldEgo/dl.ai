from django.urls import path
from . import views

urlpatterns = [
    path('ai/chat/', views.chat_view, name='chat_view'),
    path('ai/solve-problem/', views.decide_task_view, name='decide_task_view'),
    path('ai/find-error/',views.find_error_view, name='find_error_view'),
    path('ai/test-panel/login/', views.prompt_developer_login_view, name='prompt_developer_login_view'),
    path('ai/admin/set-password/', views.set_password_view, name='set_password_view'),
    path('ai/assets/<path:asset_path>', views.asset_view, name='asset_view'),
]
