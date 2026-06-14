"""Per-user conversation history store for stateful model clients."""

from typing import Any


class ConversationHistory:
    """Simple in-memory conversation history keyed by user/client id.

    This is a drop-in replacement for the legacy global ``hist`` dictionary.
    It caps each conversation at ``max_messages`` to avoid unbounded growth.
    """

    def __init__(self, max_messages: int = 20):
        self._store: dict[Any, list[dict]] = {}
        self.max_messages = max_messages

    def get(self, user_id: Any) -> list[dict]:
        return self._store.setdefault(user_id, [])

    def append(self, user_id: Any, message: dict) -> None:
        history = self.get(user_id)
        history.append(message)
        if len(history) > self.max_messages:
            history[:] = history[-self.max_messages :]

    def add_exchange(self, user_id: Any, user_message: str, assistant_message: str) -> None:
        self.append(user_id, {"role": "user", "content": user_message})
        self.append(user_id, {"role": "assistant", "content": assistant_message})

    def reset(self, user_id: Any) -> None:
        self._store[user_id] = []

    def clear_all(self) -> None:
        self._store.clear()


# Global instance used by the WebSocket consumer and model clients.
conversation_history = ConversationHistory()
