"""High-level services used by views, consumers, and admin code."""

from .auth import WebSocketAuthService, get_user_identity_for_log, resolve_external_account
from .conversation_history import ConversationHistory, conversation_history
from .log_writer import LogWriter
from .message_composer import MessageComposer
from .model_caller import ModelCaller
from .prompt_resolver import PromptResolver, get_default_shared_prompt, parse_shared_prompt_id
from .task_registry import apply_dl_task_info, ensure_task

__all__ = [
    "WebSocketAuthService",
    "get_user_identity_for_log",
    "resolve_external_account",
    "ConversationHistory",
    "conversation_history",
    "LogWriter",
    "MessageComposer",
    "ModelCaller",
    "PromptResolver",
    "get_default_shared_prompt",
    "parse_shared_prompt_id",
    "apply_dl_task_info",
    "ensure_task",
]
