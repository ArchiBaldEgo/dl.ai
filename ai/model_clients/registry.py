"""Реестр AI-моделей, используемый WebSocket consumer и health-checker.

Сопоставляет внутренние ключи моделей (DeepSeek_V3_1, Web_DeepSeek, ...)
с их обработчиками (async-функциями), отображаемыми названиями и capabilities
(text/vision/reasoning). ModelRegistry — основной API для получения
обработчика модели по ключу.

Провайдеры SambaNova и Groq по умолчанию отключены и включаются через
настройки ``AI_ENABLE_SAMBANOVA`` / ``AI_ENABLE_GROQ`` (см. DjangoTest/settings).
"""

from typing import Callable, Coroutine, Dict

from django.conf import settings

from . import openrouter, web_deepseek, ollama

Handler = Callable[..., Coroutine]

# Default capability set for a plain text-only, non-reasoning model.
_TEXT_ONLY = {"text": True, "vision": False, "reasoning": False}
# Reasoning / "thinking" models: text-only but advertised as reasoning-capable.
_REASONING = {"text": True, "vision": False, "reasoning": True}


_MODELS: Dict[str, Dict[str, object]] = {
    # --- Web DeepSeek (бот-пул через Puppeteer) — бесплатные, идут первыми ---
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
    # --- OpenRouter (бесплатные модели) ---
    "OR_Nemotron_Ultra_550B": {
        "title": "OR Nemotron Ultra 550B",
        "handler": openrouter.ask_OR_Nemotron_Ultra_550B_async,
        "capabilities": _TEXT_ONLY,
    },
    "OR_Ling_3_Flash": {
        "title": "OR Ling 3.0 Flash",
        "handler": openrouter.ask_OR_Ling_3_Flash_async,
        "capabilities": _TEXT_ONLY,
    },
    "OR_Gemma_4_31B": {
        "title": "OR Gemma 4 31B",
        "handler": openrouter.ask_OR_Gemma_4_31B_async,
        "capabilities": _TEXT_ONLY,
    },
    "OR_Gemma_4_26B": {
        "title": "OR Gemma 4 26B",
        "handler": openrouter.ask_OR_Gemma_4_26B_async,
        "capabilities": _TEXT_ONLY,
    },
    "OR_Nemotron_Super_120B": {
        "title": "OR Nemotron Super 120B",
        "handler": openrouter.ask_OR_Nemotron_Super_120B_async,
        "capabilities": _TEXT_ONLY,
    },
    "OR_North_Mini_Code": {
        "title": "OR North Mini Code",
        "handler": openrouter.ask_OR_North_Mini_Code_async,
        "capabilities": _TEXT_ONLY,
    },
    "OR_Nemotron_Nano_30B_Reasoning": {
        "title": "OR Nemotron Nano 30B Reasoning",
        "handler": openrouter.ask_OR_Nemotron_Nano_30B_Reasoning_async,
        "capabilities": _REASONING,
    },
    "OR_Nemotron_Nano_30B": {
        "title": "OR Nemotron Nano 30B",
        "handler": openrouter.ask_OR_Nemotron_Nano_30B_async,
        "capabilities": _TEXT_ONLY,
    },
    "OR_GPT_OSS_20B": {
        "title": "OR GPT-OSS 20B",
        "handler": openrouter.ask_OR_GPT_OSS_20B_async,
        "capabilities": _TEXT_ONLY,
    },
    "OR_Nemotron_Nano_12B_VL": {
        "title": "OR Nemotron Nano 12B VL",
        "handler": openrouter.ask_OR_Nemotron_Nano_12B_VL_async,
        "capabilities": {"text": True, "vision": True, "reasoning": False},
    },
    "OR_Nemotron_Nano_9B": {
        "title": "OR Nemotron Nano 9B",
        "handler": openrouter.ask_OR_Nemotron_Nano_9B_async,
        "capabilities": _TEXT_ONLY,
    },
    "OR_Free_Router": {
        "title": "OR Free Router",
        "handler": openrouter.ask_OR_Free_Router_async,
        "capabilities": _TEXT_ONLY,
    },
    # --- Ollama (cloud-модели вида <name>:cloud, обычный чат без инструментов) ---
    "Ollama_Glm_5_2_Cloud": {
        "title": "Ollama GLM 5.2 Cloud",
        "handler": ollama.ask_Ollama_Glm_5_2_Cloud_async,
        "capabilities": _TEXT_ONLY,
    },
    "Ollama_Gemma_4_Cloud": {
        "title": "Ollama Gemma 4 Cloud",
        "handler": ollama.ask_Ollama_Gemma_4_Cloud_async,
        "capabilities": _TEXT_ONLY,
    },
    "Ollama_Qwen_3_5_Cloud": {
        "title": "Ollama Qwen 3.5 Cloud",
        "handler": ollama.ask_Ollama_Qwen_3_5_Cloud_async,
        "capabilities": _TEXT_ONLY,
    },
    "Ollama_Nemotron_3_Super_Cloud": {
        "title": "Ollama Nemotron 3 Super Cloud",
        "handler": ollama.ask_Ollama_Nemotron_3_Super_Cloud_async,
        "capabilities": _TEXT_ONLY,
    },
    "Ollama_Kimi_K2_7_Code_Cloud": {
        "title": "Ollama Kimi K2.7 Code Cloud",
        "handler": ollama.ask_Ollama_Kimi_K2_7_Code_Cloud_async,
        "capabilities": _TEXT_ONLY,
    },
    "Ollama_Kimi_K2_6_Cloud": {
        "title": "Ollama Kimi K2.6 Cloud",
        "handler": ollama.ask_Ollama_Kimi_K2_6_Cloud_async,
        "capabilities": _TEXT_ONLY,
    },
}


def _groq_models() -> Dict[str, Dict[str, object]]:
    """Записи реестра для Groq-провайдера (включается через AI_ENABLE_GROQ)."""
    from . import groq

    return {
        "Groq_Llama_3_3_70B": {
            "title": "Groq Llama 3.3 70B",
            "handler": groq.ask_Groq_Llama_3_3_70B_async,
            "capabilities": _TEXT_ONLY,
        },
        "Groq_Llama_3_1_8B": {
            "title": "Groq Llama 3.1 8B",
            "handler": groq.ask_Groq_Llama_3_1_8B_async,
            "capabilities": _TEXT_ONLY,
        },
        "Groq_Gpt_Oss_120B": {
            "title": "Groq GPT-OSS 120B",
            "handler": groq.ask_Groq_Gpt_Oss_120B_async,
            "capabilities": _TEXT_ONLY,
        },
        "Groq_Gpt_Oss_20B": {
            "title": "Groq GPT-OSS 20B",
            "handler": groq.ask_Groq_Gpt_Oss_20B_async,
            "capabilities": _TEXT_ONLY,
        },
        "Groq_Qwen_3_6_27B": {
            "title": "Groq Qwen 3.6 27B",
            "handler": groq.ask_Groq_Qwen_3_6_27B_async,
            "capabilities": _TEXT_ONLY,
        },
    }


def _sambanova_models() -> Dict[str, Dict[str, object]]:
    """Записи реестра для SambaNova-провайдера (включается через AI_ENABLE_SAMBANOVA)."""
    from . import sambanova

    return {
        "DeepSeek_V3_1": {
            "title": "Samba DeepSeek-V3.1",
            "handler": sambanova.ask_DeepSeek_V3_1_async,
            "capabilities": _TEXT_ONLY,
        },
        "DeepSeek_V3_2": {
            "title": "Samba DeepSeek-V3.2",
            "handler": sambanova.ask_DeepSeek_V3_2_async,
            "capabilities": _TEXT_ONLY,
        },
        "Meta_Llama_3_3_70B_Instruct": {
            "title": "Samba Meta-Llama-3.3-70B-Instruct",
            "handler": sambanova.ask_Meta_Llama_3_3_70B_Instruct_async,
            "capabilities": _TEXT_ONLY,
        },
        "MiniMax_M2_7": {
            "title": "Samba MiniMax-M2.7",
            "handler": sambanova.ask_MiniMax_M2_7_async,
            "capabilities": _TEXT_ONLY,
        },
        "Gemma_3_12b_it": {
            "title": "Samba gemma-4-31B-it",
            "handler": sambanova.ask_Gemma_3_12b_it_async,
            "capabilities": _TEXT_ONLY,
        },
        "Gpt_oss_120b": {
            "title": "Samba gpt-oss-120b",
            "handler": sambanova.ask_Gpt_oss_120b_async,
            "capabilities": _TEXT_ONLY,
        },
        "DeepSeek_R1_Distill_Llama_70B": {
            "title": "Samba DeepSeek-R1-Distill-Llama-70B",
            "handler": sambanova.ask_DeepSeek_R1_Distill_Llama_70B_async,
            "capabilities": _REASONING,
        },
        "DeepSeek_V3_1_cb": {
            "title": "Samba DeepSeek-V3.1 (cloud buffer)",
            "handler": sambanova.ask_DeepSeek_V3_1_cb_async,
            "capabilities": _TEXT_ONLY,
        },
        "Llama_4_Maverick_17B_128E_Instruct": {
            "title": "Samba Llama-4-Maverick-17B-128E-Instruct",
            "handler": sambanova.ask_Llama_4_Maverick_17B_128E_Instruct_async,
            "capabilities": _TEXT_ONLY,
        },
        "MiniMax_M2_5": {
            "title": "Samba MiniMax-M2.5",
            "handler": sambanova.ask_MiniMax_M2_5_async,
            "capabilities": _TEXT_ONLY,
        },
    }


# Условная регистрация отключённых по умолчанию провайдеров.
if getattr(settings, "AI_ENABLE_GROQ", False):
    _MODELS.update(_groq_models())

if getattr(settings, "AI_ENABLE_SAMBANOVA", False):
    _MODELS.update(_sambanova_models())

_DEFAULT_CAPABILITIES = _TEXT_ONLY


class ModelRegistry:
    """Реестр AI-моделей: сопоставляет ключ → {title, handler, capabilities}."""

    def __init__(self, models: Dict[str, Dict[str, object]]):
        self._models = dict(models)

    def keys(self):
        return self._models.keys()

    def items(self):
        return self._models.items()

    def title_to_key(self) -> dict:
        """Вернуть обратный map ``{title: key}`` для всех зарегистрированных моделей.

        Используется вместо прямого доступа к ``_models`` там, где нужен
        обратный lookup по отображаемому названию (см. ai/views.py).
        """
        return {self.title(key): key for key in self._models}

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