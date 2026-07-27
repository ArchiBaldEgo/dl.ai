"""OpenRouter API клиент — бесплатные и платные модели через единый endpoint.

OpenRouter предоставляет доступ к сотням моделей через OpenAI-совместимый API.
Бесплатные модели помечены суффиксом :free. Rate limits: ~20-50 запросов/день.

Base URL: https://openrouter.ai/api/v1
"""

import logging
from typing import Tuple, Optional

import requests

from .config import OPENROUTER_API_KEY, proxies
from .history import conversation_history

logger = logging.getLogger(__name__)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# --- Декларативная таблица бесплатных моделей ---
# Ключ → (model_id, max_tokens, description)
OPENROUTER_FREE_MODELS: dict[str, dict] = {
    "OR_Nemotron_Ultra_550B": {
        "model": "nvidia/nemotron-3-ultra-550b-a55b:free",
        "max_tokens": 8000,
        "description": "NVIDIA Nemotron Ultra 550B (A55B) — 1M контекст",
    },
    "OR_Ling_3_Flash": {
        "model": "inclusionai/ling-3.0-flash:free",
        "max_tokens": 8000,
        "description": "Ling 3.0 Flash — 262K контекст",
    },
    "OR_Gemma_4_31B": {
        "model": "google/gemma-4-31b-it:free",
        "max_tokens": 8000,
        "description": "Google Gemma 4 31B — 262K контекст",
    },
    "OR_Gemma_4_26B": {
        "model": "google/gemma-4-26b-a4b-it:free",
        "max_tokens": 8000,
        "description": "Google Gemma 4 26B A4B — 262K контекст",
    },
    "OR_Nemotron_Super_120B": {
        "model": "nvidia/nemotron-3-super-120b-a12b:free",
        "max_tokens": 8000,
        "description": "NVIDIA Nemotron Super 120B (A12B) — 262K контекст",
    },
    "OR_North_Mini_Code": {
        "model": "cohere/north-mini-code:free",
        "max_tokens": 8000,
        "description": "Cohere North Mini Code — 256K контекст",
    },
    "OR_Nemotron_Nano_30B_Reasoning": {
        "model": "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
        "max_tokens": 8000,
        "description": "NVIDIA Nemotron Nano 30B Reasoning — 256K контекст",
    },
    "OR_Nemotron_Nano_30B": {
        "model": "nvidia/nemotron-3-nano-30b-a3b:free",
        "max_tokens": 8000,
        "description": "NVIDIA Nemotron Nano 30B — 256K контекст",
    },
    "OR_GPT_OSS_20B": {
        "model": "openai/gpt-oss-20b:free",
        "max_tokens": 8000,
        "description": "OpenAI GPT-OSS 20B — 131K контекст",
    },
    "OR_Nemotron_Nano_12B_VL": {
        "model": "nvidia/nemotron-nano-12b-v2-vl:free",
        "max_tokens": 8000,
        "description": "NVIDIA Nemotron Nano 12B Vision-Language — 128K контекст",
    },
    "OR_Nemotron_Nano_9B": {
        "model": "nvidia/nemotron-nano-9b-v2:free",
        "max_tokens": 8000,
        "description": "NVIDIA Nemotron Nano 9B — 128K контекст",
    },
    "OR_Free_Router": {
        "model": "openrouter/free",
        "max_tokens": 8000,
        "description": "OpenRouter Free Router — авто-выбор бесплатной модели",
    },
}


def _get_headers() -> dict:
    return {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://dlai.gsu.by",
        "X-Title": "DLAI",
    }


async def _ask_openrouter(
    messages: str,
    user_id: int,
    model_id: str,
    *,
    max_tokens: int = 8000,
    temperature: float = 0.7,
) -> Tuple[str, Optional[int]]:
    """Generic OpenRouter chat completion wrapper with history management."""
    import asyncio

    history = conversation_history.get(user_id)
    history.append({"role": "user", "content": messages})

    payload = {
        "model": model_id,
        "messages": history,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }

    try:
        response = await asyncio.to_thread(
            requests.post,
            f"{OPENROUTER_BASE_URL}/chat/completions",
            json=payload,
            headers=_get_headers(),
            proxies=proxies,
            timeout=60,
        )

        if response.status_code == 429:
            return "Превышен лимит запросов OpenRouter (free tier). Попробуйте позже.", "0"

        if response.status_code == 413:
            return "Превышен лимит токенов для модели. Попробуйте сократить запрос или выберите другую модель.", "0"

        if response.status_code == 401:
            return "OpenRouter API ключ недействителен. Проверьте OPENROUTER_API_KEY.", "0"

        if response.status_code != 200:
            logger.warning("OpenRouter API error: status=%s body=%s", response.status_code, response.text[:500])
            return f"Ошибка OpenRouter API (код {response.status_code}).", "0"

        obj = response.json()
        if "choices" not in obj or not obj["choices"]:
            return "Неожиданный формат ответа от OpenRouter.", "0"

        completion_tokens = obj.get("usage", {}).get("completion_tokens", 0)
        assistant_content = obj["choices"][0]["message"].get("content", "")
        conversation_history.append(user_id, {"role": "assistant", "content": assistant_content})
        return assistant_content, completion_tokens

    except requests.exceptions.ConnectionError:
        return "Ошибка подключения к OpenRouter.", "0"
    except requests.exceptions.Timeout:
        return "Таймаут при подключении к OpenRouter. Попробуйте позже.", "0"
    except Exception as e:
        logger.exception("Unexpected error in OpenRouter call")
        conversation_history.reset(user_id)
        return "Что-то пошло не так. Контекст очищен, введите новый запрос.", "0"


# --- Автогенерация функций для каждой модели ---

def _make_handler(model_key: str):
    cfg = OPENROUTER_FREE_MODELS[model_key]

    async def handler(messages: str, user_id: int) -> Tuple[str, Optional[int]]:
        return await _ask_openrouter(
            messages, user_id, cfg["model"],
            max_tokens=cfg["max_tokens"],
        )

    handler.__name__ = f"ask_{model_key}_async"
    handler.__qualname__ = handler.__name__
    handler.__doc__ = cfg["description"]
    return handler


# Экспорт
ask_OR_Nemotron_Ultra_550B_async = _make_handler("OR_Nemotron_Ultra_550B")
ask_OR_Ling_3_Flash_async = _make_handler("OR_Ling_3_Flash")
ask_OR_Gemma_4_31B_async = _make_handler("OR_Gemma_4_31B")
ask_OR_Gemma_4_26B_async = _make_handler("OR_Gemma_4_26B")
ask_OR_Nemotron_Super_120B_async = _make_handler("OR_Nemotron_Super_120B")
ask_OR_North_Mini_Code_async = _make_handler("OR_North_Mini_Code")
ask_OR_Nemotron_Nano_30B_Reasoning_async = _make_handler("OR_Nemotron_Nano_30B_Reasoning")
ask_OR_Nemotron_Nano_30B_async = _make_handler("OR_Nemotron_Nano_30B")
ask_OR_GPT_OSS_20B_async = _make_handler("OR_GPT_OSS_20B")
ask_OR_Nemotron_Nano_12B_VL_async = _make_handler("OR_Nemotron_Nano_12B_VL")
ask_OR_Nemotron_Nano_9B_async = _make_handler("OR_Nemotron_Nano_9B")
ask_OR_Free_Router_async = _make_handler("OR_Free_Router")