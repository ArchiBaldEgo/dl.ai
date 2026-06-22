"""Model client registry used by the WebSocket consumer and health checker."""

from typing import Callable, Coroutine, Dict

from . import gigachat, huggingface, sambanova, web_deepseek

Handler = Callable[..., Coroutine]

# Default capability set for a plain text-only, non-reasoning model.
_TEXT_ONLY = {"text": True, "vision": False, "reasoning": False}
# Reasoning / "thinking" models: text-only but advertised as reasoning-capable.
_REASONING = {"text": True, "vision": False, "reasoning": True}


_MODELS: Dict[str, Dict[str, object]] = {
    "DeepSeek_R1_Distill_Llama_70B": {
        "title": "DeepSeek-R1-Distill-Llama-70B",
        "handler": sambanova.ask_DeepSeek_R1_Distill_Llama_70B_async,
        "capabilities": _REASONING,
    },
    "DeepSeek_V3_1": {
        "title": "DeepSeek-V3.1",
        "handler": sambanova.ask_DeepSeek_V3_1_async,
        "capabilities": _TEXT_ONLY,
    },
    "DeepSeek_V3_1_cb": {
        "title": "DeepSeek-V3.1-cb",
        "handler": sambanova.ask_DeepSeek_V3_1_cb_async,
        "capabilities": _TEXT_ONLY,
    },
    "DeepSeek_V3_2": {
        "title": "DeepSeek-V3.2",
        "handler": sambanova.ask_DeepSeek_V3_2_async,
        "capabilities": _TEXT_ONLY,
    },
    "Llama_4_Maverick_17B_128E_Instruct": {
        "title": "Llama-4-Maverick-17B-128E-Instruct",
        "handler": sambanova.ask_Llama_4_Maverick_17B_128E_Instruct_async,
        "capabilities": _TEXT_ONLY,
    },
    "Meta_Llama_3_3_70B_Instruct": {
        "title": "Meta-Llama-3.3-70B-Instruct",
        "handler": sambanova.ask_Meta_Llama_3_3_70B_Instruct_async,
        "capabilities": _TEXT_ONLY,
    },
    "MiniMax_M2_5": {
        "title": "MiniMax-M2.5",
        "handler": sambanova.ask_MiniMax_M2_5_async,
        "capabilities": _TEXT_ONLY,
    },
    "MiniMax_M2_7": {
        "title": "MiniMax-M2.7",
        "handler": sambanova.ask_MiniMax_M2_7_async,
        "capabilities": _TEXT_ONLY,
    },
    "Gemma_3_12b_it": {
        "title": "gemma-3-12b-it",
        "handler": sambanova.ask_Gemma_3_12b_it_async,
        "capabilities": _TEXT_ONLY,
    },
    "Gpt_oss_120b": {
        "title": "gpt-oss-120b",
        "handler": sambanova.ask_Gpt_oss_120b_async,
        "capabilities": _TEXT_ONLY,
    },
    "Web_DeepSeek": {
        "title": "Web DeepSeek",
        "handler": web_deepseek.ask_Web_DeepSeek_async,
        "capabilities": _TEXT_ONLY,
    },
    "Web_DeepSeek_Thinking": {
        "title": "Web DeepSeek Thinking",
        "handler": web_deepseek.ask_Web_DeepSeek_Thinking_async,
        "capabilities": _REASONING,
    },
    # Legacy/alias handlers kept for runtime backward compatibility.
    "DeepSeek_R1": {
        "title": "DeepSeek-R1",
        "handler": sambanova.ask_DeepSeek_R1_async,
        "capabilities": _REASONING,
    },
    "Meta_Llama_3_1_70B_Instruct": {
        "title": "Meta-Llama-3.1-70B-Instruct",
        "handler": sambanova.ask_Meta_Llama_3_1_70B_Instruct_async,
        "capabilities": _TEXT_ONLY,
    },
    "Mixtral_8x22b": {
        "title": "Mixtral-8x22b",
        "handler": sambanova.ask_Mixtral_8x22b_async,
        "capabilities": _TEXT_ONLY,
    },
    "Mistral_Nemo_Instruct": {
        "title": "Mistral-Nemo-Instruct",
        "handler": huggingface.ask_Mistral_Nemo_Instruct_async,
        "capabilities": _TEXT_ONLY,
    },
    "Gemma_7b": {
        "title": "Gemma-7b",
        "handler": huggingface.ask_Gemma_7b_async,
        "capabilities": _TEXT_ONLY,
    },
    "GigaChat": {
        "title": "GigaChat-Pro",
        "handler": gigachat.send_prompt_async,
        "capabilities": _TEXT_ONLY,
    },
}

_DEFAULT_CAPABILITIES = _TEXT_ONLY


class ModelRegistry:
    """Registry mapping internal model keys to callable handlers."""

    def __init__(self, models: Dict[str, Dict[str, object]]):
        self._models = dict(models)

    def keys(self):
        return self._models.keys()

    def items(self):
        return self._models.items()

    def get(self, key: str):
        return self._models.get(key)

    def handler(self, key: str) -> Handler | None:
        info = self._models.get(key)
        if info is None:
            return None
        return info.get("handler")  # type: ignore[return-value]

    def title(self, key: str) -> str:
        info = self._models.get(key)
        if info is None:
            return key
        return str(info.get("title") or key)

    def capabilities(self, key: str) -> dict:
        """Return the capability dict (text/vision/reasoning) for a model.

        Always returns a dict with the three boolean keys; unknown models get
        the conservative text-only default.
        """
        info = self._models.get(key)
        caps = info.get("capabilities") if info else None
        if not isinstance(caps, dict):
            return dict(_DEFAULT_CAPABILITIES)
        return {
            "text": bool(caps.get("text", True)),
            "vision": bool(caps.get("vision", False)),
            "reasoning": bool(caps.get("reasoning", False)),
        }

    def register(self, key: str, title: str, handler: Handler, capabilities: dict | None = None) -> None:
        self._models[key] = {
            "title": title,
            "handler": handler,
            "capabilities": capabilities or dict(_DEFAULT_CAPABILITIES),
        }


registry = ModelRegistry(_MODELS)