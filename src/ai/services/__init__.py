"""Высокоуровневые сервисы для views, consumers и admin-кода.

Объединяет: аутентификацию WebSocket, композицию сообщений, вызов моделей,
резолвинг промптов, логирование, историю диалогов, авто-перевод, регистрацию DL-задач,
документацию.
"""

from ai.services.auth import (
    WebSocketAuthService,
    can_use_ai,
    get_user_identity_for_log,
    resolve_external_account,
    ws_ai_disabled_reason,
)
from ai.services.auto_translate import translate_object, translate_text, get_translatable_models
from ai.model_clients.history import ConversationHistory, conversation_history
from ai.services.docs import (
    DocChapter,
    DocUnavailableError,
    all_chapters,
    get_chapter,
    read_chapter_markdown,
    render_chapter_html,
    visible_chapter_slugs,
)
from ai.services.log_writer import LogWriter
from ai.services.message_composer import MessageComposer
from ai.services.model_caller import ModelCaller
from ai.services.prompt_resolver import PromptResolver, get_default_shared_prompt, parse_shared_prompt_id
from ai.services.solution_cache import CACHE_SERVE_LOG_MESSAGE, find_passed_solution, last_solve_model, mark_cache_used, record_result, record_submission
from ai.services.task_registry import apply_dl_task_info, ensure_task

__all__ = [
    "WebSocketAuthService",
    "can_use_ai",
    "ws_ai_disabled_reason",
    "get_user_identity_for_log",
    "resolve_external_account",
    "translate_object",
    "translate_text",
    "get_translatable_models",
    "ConversationHistory",
    "conversation_history",
    "DocChapter",
    "DocUnavailableError",
    "all_chapters",
    "get_chapter",
    "read_chapter_markdown",
    "render_chapter_html",
    "visible_chapter_slugs",
    "LogWriter",
    "MessageComposer",
    "ModelCaller",
    "PromptResolver",
    "get_default_shared_prompt",
    "parse_shared_prompt_id",
    "apply_dl_task_info",
    "ensure_task",
    "CACHE_SERVE_LOG_MESSAGE",
    "find_passed_solution",
    "last_solve_model",
    "mark_cache_used",
    "record_result",
    "record_submission",
]
