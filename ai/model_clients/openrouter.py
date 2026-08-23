"""OpenRouter API клиент — бесплатные и платные модели через единый endpoint.

OpenRouter предоставляет доступ к сотням моделей через OpenAI-совместимый API.
Бесплатные модели помечены суффиксом :free. Rate limits: ~20-50 запросов/день.

Base URL: https://openrouter.ai/api/v1
"""

import logging
from typing import Tuple, Optional

import requests

from ._base import bearer_headers, make_table_handlers
from .config import OPENROUTER_API_KEY, proxies
from .exceptions import map_http_error
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
    return bearer_headers(
        OPENROUTER_API_KEY,
        {"HTTP-Referer": "https://dlai.gsu.by", "X-Title": "DLAI"},
    )


async def _ask_openrouter(
    messages: str,
    user_id: int,
    model_id: str,
    *,
    max_tokens: int = 8000,
    temperature: float = 0.7,
) -> Tuple[str, Optional[int], bool]:
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

        if response.status_code != 200:
            # Логируем только неспецифические статусы (429/413/401 в оригинале
            # возвращались без warning — сохраняем это поведение).
            if response.status_code not in (401, 413, 429):
                logger.warning("OpenRouter API error: status=%s body=%s", response.status_code, response.text[:500])
            return map_http_error(response.status_code, "openrouter"), 0, True

        obj = response.json()
        if "choices" not in obj or not obj["choices"]:
            return "Неожиданный формат ответа от OpenRouter.", 0, True

        completion_tokens = obj.get("usage", {}).get("completion_tokens", 0)
        assistant_content = obj["choices"][0]["message"].get("content", "") or ""
        if not assistant_content.strip():
            logger.warning("OpenRouter model %s returned empty content", model_id)
            return f"Модель OpenRouter ({model_id}) вернула пустой ответ. Попробуйте позже.", 0, True
        conversation_history.append(user_id, {"role": "assistant", "content": assistant_content})
        return assistant_content, completion_tokens, False

    except requests.exceptions.ConnectionError:
        return "Ошибка подключения к OpenRouter.", 0, True
    except requests.exceptions.Timeout:
        return "Таймаут при подключении к OpenRouter. Попробуйте позже.", 0, True
    except Exception as e:
        logger.exception("Unexpected error in OpenRouter call")
        conversation_history.reset(user_id)
        return "Что-то пошло не так. Контекст очищен, введите новый запрос.", 0, True


# --- Автогенерация функций для каждой модели (через _base.make_table_handlers) ---


def _openrouter_handler_factory(model_key: str, cfg: dict):
    model_id = cfg["model"]
    max_tokens = cfg["max_tokens"]

    async def handler(messages: str, user_id: int) -> Tuple[str, Optional[int], bool]:
        return await _ask_openrouter(messages, user_id, model_id, max_tokens=max_tokens)

    return handler


globals().update(
    make_table_handlers(
        OPENROUTER_FREE_MODELS,
        _openrouter_handler_factory,
        doc_fn=lambda key, cfg: cfg["description"],
    )
)