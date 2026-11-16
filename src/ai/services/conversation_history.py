"""Совместимый реэкспорт общего хранилища истории диалогов."""

ai.model_clients.history import ConversationHistory, conversation_history

__all__ = ["ConversationHistory", "conversation_history"]
