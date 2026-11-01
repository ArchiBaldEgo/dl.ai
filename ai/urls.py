"""URL-маршруты AI-приложения для основных страниц и API."""
from django.urls import path
from . import views

urlpatterns = [
    path('ai/', views.ai_root_view, name='ai_root'),
    path('ai/chat/', views.chat_view, name='chat_view'),
    path('ai/solve-problem/', views.decide_task_view, name='decide_task_view'),
    path('ai/find-error/',views.find_error_view, name='find_error_view'),
    path('ai/assets/<path:asset_path>', views.asset_view, name='asset_view'),
    path('ai/transcribe/', views.transcribe_audio, name='transcribe_audio'),
    path('ai/my-models/', views.my_models_view, name='ai_my_models'),
    path('ai/my-models/create/', views.my_model_save_view, name='ai_my_model_create'),
    path('ai/my-models/delete/', views.my_model_delete_view, name='ai_my_model_delete'),
    path('ai/my-models/access/', views.my_model_access_view, name='ai_my_model_access'),
    path('ai/my-models/search-users/', views.my_model_search_users_view, name='ai_my_model_search_users'),
    path('ai/docs/', views.chat_user_docs_view, name='ai_user_docs'),
    path('ai/docs/download/', views.chat_user_docs_download_view, name='ai_user_docs_download'),
]
