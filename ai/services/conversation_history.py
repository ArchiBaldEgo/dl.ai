"""Compatibility re-export of the shared conversation history store."""

from ..model_clients.history import ConversationHistory, conversation_history

__all__ = ["ConversationHistory", "conversation_history"]
