"""История диалогов на Redis (через Django cache) для stateful-клиентов моделей.

Заменяет старый in-memory словарь. Хранит до max_messages сообщений
для каждого пользователя с TTL 24 часа. Переживает рестарт процесса
и работает across multiple workers.
"""

from typing import Any

from django.core.cache import cache

from ..constants import AI_CACHE_KEY_PREFIX

DEFAULT_MAX_MESSAGES = 20
DEFAULT_TTL_SECONDS = 24 * 60 * 60  # 24 hours


class ConversationHistory:
    """История диалогов на Django cache (Redis в production).

    Drop-in замена старого in-memory словаря. Ограничивает длину истории
    до max_messages и хранит в общем кэше, чтобы переживать рестарты
    и работать across multiple workers.
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

    def reset(self, user_id: Any) -> None:
        cache.set(self._key(user_id), [], timeout=self.ttl_seconds)


# Global instance used by the WebSocket consumer and model clients.
conversation_history = ConversationHistory()
