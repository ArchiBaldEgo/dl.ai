"""Совместимый реэкспорт общего хранилища истории диалогов."""

from ..model_clients.history import ConversationHistory, conversation_history

__all__ = ["ConversationHistory", "conversation_history"]
