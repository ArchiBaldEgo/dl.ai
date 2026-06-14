"""AI model client registry and public exports.

Example:
    >>> from ai.model_clients import registry
    >>> handler = registry.get("DeepSeek_V3_1")
    >>> response = await handler("hello", user_id=42)
"""

from .registry import registry

__all__ = ["registry"]
