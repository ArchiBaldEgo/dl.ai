"""Redis-backed conversation history for stateful model clients."""

from typing import Any

from django.core.cache import cache

from ..constants import AI_CACHE_KEY_PREFIX

DEFAULT_MAX_MESSAGES = 20
DEFAULT_TTL_SECONDS = 24 * 60 * 60  # 24 hours


class ConversationHistory:
    """Conversation history backed by Django's configured cache (Redis in production).

    This is a drop-in replacement for the legacy in-memory ``hist`` dictionary.
    It caps each conversation at ``max_messages`` and stores history in a shared
    cache so it survives process restarts and works across multiple workers.
    """

    def __init__(
        self,
        max_messages: int = DEFAULT_MAX_MESSAGES,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        key_prefix: str = f"{AI_CACHE_KEY_PREFIX}:history",
    ):
        self.max_messages = max_messages
        self.ttl_seconds = ttl_seconds
        self.key_prefix = key_prefix

    def _key(self, user_id: Any) -> str:
        return f"{self.key_prefix}:{user_id}"

    def get(self, user_id: Any) -> list[dict]:
        history = cache.get(self._key(user_id))
        if not isinstance(history, list):
            history = []
            cache.set(self._key(user_id), history, timeout=self.ttl_seconds)
        return history

    def append(self, user_id: Any, message: dict) -> None:
        history = self.get(user_id)
        history.append(message)
        if len(history) > self.max_messages:
            history[:] = history[-self.max_messages :]
        cache.set(self._key(user_id), history, timeout=self.ttl_seconds)

    def add_exchange(self, user_id: Any, user_message: str, assistant_message: str) -> None:
        self.append(user_id, {"role": "user", "content": user_message})
        self.append(user_id, {"role": "assistant", "content": assistant_message})

    def reset(self, user_id: Any) -> None:
        cache.set(self._key(user_id), [], timeout=self.ttl_seconds)

    def clear_all(self) -> None:
        # Cache backends do not expose key enumeration in a portable way.
        # This method is kept for interface compatibility only.
        pass


# Global instance used by the WebSocket consumer and model clients.
conversation_history = ConversationHistory()
