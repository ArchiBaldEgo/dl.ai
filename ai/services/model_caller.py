"""Model invocation service for the WebSocket consumer."""

from typing import Any

from asgiref.sync import sync_to_async

from ..model_clients import registry
from ..model_clients.exceptions import humanize_model_error


_LEGACY_ALIASES = {
    "DeepSeek_R1": "DeepSeek_R1_Distill_Llama_70B",
    "DeepSeek-R1": "DeepSeek_R1_Distill_Llama_70B",
    "DeepSeek-R1-Distill-Llama-70B": "DeepSeek_R1_Distill_Llama_70B",
    "DeepSeek-V3.1": "DeepSeek_V3_1",
    "DeepSeek-V3.1-cb": "DeepSeek_V3_1_cb",
    "DeepSeek-V3.2": "DeepSeek_V3_2",
    "Llama_3_1_Tulu_3_405B": "Meta_Llama_3_3_70B_Instruct",
    "Meta_Llama_3_1_70B_Instruct": "Meta_Llama_3_3_70B_Instruct",
    "Meta-Llama-3.3-70B-Instruct": "Meta_Llama_3_3_70B_Instruct",
    "Llama-4-Maverick-17B-128E-Instruct": "Llama_4_Maverick_17B_128E_Instruct",
    "MiniMax-M2.5": "MiniMax_M2_5",
    "MiniMax-M2.7": "MiniMax_M2_7",
    "gemma-3-12b-it": "Gemma_3_12b_it",
    "gpt-oss-120b": "Gpt_oss_120b",
    "QwQ_32B": "DeepSeek_R1_Distill_Llama_70B",
    "Mixtral_8x7B": "Llama_4_Maverick_17B_128E_Instruct",
    "Mixtral_8x22b": "Llama_4_Maverick_17B_128E_Instruct",
}


def _resolve_legacy_alias(value: str) -> str:
    return _LEGACY_ALIASES.get(value, value)


class ModelCallResult:
    """Result of a model invocation."""

    def __init__(
        self,
        response_text: str = "",
        tokens: Any = 0,
        model_title: str = "",
        error_message: str = "",
        is_error: bool = False,
    ):
        self.response_text = response_text
        self.tokens = tokens
        self.model_title = model_title
        self.error_message = error_message
        self.is_error = is_error


class ModelCaller:
    """Resolve a model key (including legacy aliases) and invoke its handler."""

    def __init__(self, registry_instance=registry):
        self.registry = registry_instance

    async def call(self, message: str, client_id: str, model_key: str) -> ModelCallResult:
        normalized_key = model_key if self.registry.get(model_key) else _resolve_legacy_alias(model_key)
        handler = self.registry.handler(normalized_key)
        title = self.registry.title(normalized_key)

        if handler is None:
            error_text = f"Модель {model_key} не найдена. Используйте доступные модели."
            return ModelCallResult(
                response_text=error_text,
                model_title=model_key,
                error_message=error_text,
                is_error=True,
            )

        try:
            response = await handler(message, client_id)
        except Exception as exc:
            friendly, detailed = humanize_model_error(str(exc), include_detail=True)
            return ModelCallResult(
                response_text=f"Ошибка при обработке запроса: {friendly}",
                model_title=title,
                error_message=detailed,
                is_error=True,
            )

        if isinstance(response, tuple):
            response_text = response[0] if len(response) > 0 else ""
            tokens = response[1] if len(response) > 1 else 0
        else:
            response_text = response
            tokens = 0

        return ModelCallResult(
            response_text=str(response_text or ""),
            tokens=tokens,
            model_title=title,
        )
