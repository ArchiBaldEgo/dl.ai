"""Клиенты моделей SambaNova (DeepSeek, Llama, MiniMax, Gemma, GPT-OSS).

Каждая функция — async-обработчик, принимающий (messages, user_id) и
возвращающий (response_text, tokens). Использует SambaNova API (SC_TOKEN).

Архитектура: generic _ask_sambanova_model_async() + декларативная таблица
SAMBANOVA_MODELS. Внешние функции-обёртки генерируются автоматически.
"""

import logging
from asyncio import TimeoutError as AsyncTimeoutError
from typing import Tuple, Optional

import requests

from ._base import make_table_handlers
from .config import (
    SAMBANOVA_MODEL_DEEPSEEK_R1_DISTILL_LLAMA_70B,
    SAMBANOVA_MODEL_DEEPSEEK_V3_1,
    SAMBANOVA_MODEL_DEEPSEEK_V3_1_CB,
    SAMBANOVA_MODEL_DEEPSEEK_V3_2,
    SAMBANOVA_MODEL_GEMMA_3_12B_IT,
    SAMBANOVA_MODEL_GPT_OSS,
    SAMBANOVA_MODEL_LLAMA_4_MAVERICK_17B_128E_INSTRUCT,
    SAMBANOVA_MODEL_META_LLAMA_3_3_70B_INSTRUCT,
    SAMBANOVA_MODEL_MINIMAX_M2_5,
    SAMBANOVA_MODEL_MINIMAX_M2_7,
    SC_TOKEN,
    proxies,
)
from .exceptions import (
    classify_network_error,
    extract_api_error_text,
    extract_choice_content,
    is_missing_choices_error,
    is_network_error,
    safe_parse_response,
)
from .history import conversation_history

logger = logging.getLogger(__name__)

SAMBANOVA_API_URL = "https://api.sambanova.ai/v1/chat/completions"


# --- Декларативная таблица моделей ---
# Ключ → (config_name, max_tokens, temperature, response_field)
# response_field: "content" (default) или "reasoning" (для GPT-OSS)
SAMBANOVA_MODELS: dict[str, dict] = {
    "DeepSeek_R1_Distill_Llama_70B": {
        "model": SAMBANOVA_MODEL_DEEPSEEK_R1_DISTILL_LLAMA_70B,
        "max_tokens": 9000,
        "temperature": 0.7,
    },
    "DeepSeek_V3_1": {
        "model": SAMBANOVA_MODEL_DEEPSEEK_V3_1,
        "max_tokens": 9000,
        "temperature": 0.7,
    },
    "DeepSeek_V3_1_cb": {
        "model": SAMBANOVA_MODEL_DEEPSEEK_V3_1_CB,
        "max_tokens": 9000,
    },
    "DeepSeek_V3_2": {
        "model": SAMBANOVA_MODEL_DEEPSEEK_V3_2,
        "max_tokens": 9000,
    },
    "Llama_4_Maverick_17B_128E_Instruct": {
        "model": SAMBANOVA_MODEL_LLAMA_4_MAVERICK_17B_128E_INSTRUCT,
        "max_tokens": 9000,
    },
    "Meta_Llama_3_3_70B_Instruct": {
        "model": SAMBANOVA_MODEL_META_LLAMA_3_3_70B_INSTRUCT,
        "max_tokens": 9000,
    },
    "MiniMax_M2_5": {
        "model": SAMBANOVA_MODEL_MINIMAX_M2_5,
        "max_tokens": 9000,
    },
    "MiniMax_M2_7": {
        "model": SAMBANOVA_MODEL_MINIMAX_M2_7,
        "max_tokens": 9000,
    },
    "Gemma_3_12b_it": {
        "model": SAMBANOVA_MODEL_GEMMA_3_12B_IT,
        "max_tokens": 9000,
    },
    "Gpt_oss_120b": {
        "model": SAMBANOVA_MODEL_GPT_OSS,
        "max_tokens": 8192,
        "response_field": "reasoning",  # GPT-OSS может вернуть reasoning вместо content
    },
}


def _log_response(response, max_len: int = 500) -> None:
    """Log response details at DEBUG level only."""
    if not logger.isEnabledFor(logging.DEBUG):
        return
    logger.debug("Response Status: %s", response.status_code)
    text = response.text
    if response.status_code != 200 or len(text) > max_len:
        logger.debug("Response Content (truncated): %s...", text[:max_len])
    else:
        logger.debug("Response Content: %s", text)


async def _ask_sambanova_model_async(
    messages: str,
    user_id: int,
    model_name: str,
    *,
    max_tokens: int = 9000,
    temperature: Optional[float] = None,
    timeout: float = 30.0,
    response_field: str = "content",
) -> Tuple[str, Optional[int]]:
    """Generic SambaNova chat completion wrapper with history management.

    Args:
        messages: Текст сообщения пользователя.
        user_id: ID пользователя (для истории).
        model_name: Имя модели на стороне SambaNova.
        max_tokens: Лимит токенов ответа.
        temperature: Температура генерации (None = не указывать).
        timeout: Таймаут запроса в секундах.
        response_field: Поле для извлечения ответа ("content" или "reasoning").
    """
    import asyncio

    conversation_history.append(user_id, {"role": "user", "content": messages})
    history = conversation_history.get(user_id)

    payload: dict = {
        "model": model_name,
        "messages": history,
        "max_tokens": max_tokens,
    }
    if temperature is not None:
        payload["temperature"] = temperature

    try:
        response = await asyncio.to_thread(
            requests.post,
            SAMBANOVA_API_URL,
            json=payload,
            headers={
                "Authorization": f"Bearer {SC_TOKEN}",
                "Content-Type": "application/json",
            },
            proxies=proxies,
            timeout=timeout,
        )

        _log_response(response)

        if response.status_code != 200:
            return extract_api_error_text(str(response.status_code)), 0

        obj, error_message = safe_parse_response(response.text)
        if obj is None:
            return error_message, 0

        if "choices" not in obj or not obj["choices"]:
            logger.warning("Unexpected response structure: %s", obj)
            return "Неожиданный формат ответа от сервера.", 0

        completion_tokens = obj.get("usage", {}).get("completion_tokens", 0)

        # Извлекаем ответ: content (по умолчанию) или reasoning (GPT-OSS)
        message = obj["choices"][0].get("message", {})
        if response_field == "reasoning":
            assistant_content = message.get("content") or message.get("reasoning") or ""
        else:
            assistant_content = extract_choice_content(obj)

        # Пустой ответ модели — детектим как ошибку (3-кортеж с is_error=True),
        # иначе пустая строка дойдёт до пользователя как «успех». До этого
        # extract_choice_content подставляла плейсхолдер «Пустой ответ от модели.»,
        # который обходил нижестоящие empty-гарды.
        if not (assistant_content or "").strip():
            logger.warning("SambaNova model %s returned empty content", model_name)
            return (
                "Модель вернула пустой ответ (возможно превышен лимит запросов "
                "или отказ генерации). Попробуйте позже.",
                0,
                True,
            )

        conversation_history.append(user_id, {"role": "assistant", "content": assistant_content})
        return assistant_content, completion_tokens

    except AsyncTimeoutError:
        logger.warning("SambaNova request timeout after %s seconds", timeout)
        return f"Таймаут запроса ({timeout} сек). Сервер долго не отвечает. Попробуйте позже или уменьшите запрос.", 0
    except requests.exceptions.ConnectionError as e:
        logger.warning("Connection error: %s", e)
        return classify_network_error(e), 0
    except requests.exceptions.Timeout:
        logger.warning("Timeout connecting to API")
        return "Таймаут при подключении к серверу. Попробуйте позже.", 0
    except requests.exceptions.RequestException as e:
        logger.warning("Request error: %s", e)
        return "Ошибка при подключении к серверу API.", 0
    except KeyError as e:
        if is_missing_choices_error(e):
            return "Ошибка в ответе от сервера AI.", 0
        raise
    except Exception as e:
        logger.exception("Unexpected error in SambaNova call")
        if is_network_error(e):
            return "Ошибка подключения. Ваш контекст сохранен, попробуйте позже.", 0
        if is_missing_choices_error(e):
            return "Ошибка в ответе от сервера AI.", 0
        conversation_history.reset(user_id)
        return "Что-то пошло не так. Контекст очищен, введите новый запрос.", 0


# --- Автогенерация функций для каждой модели ---
# Вместо ручных ask_*_async функций, генерируем их из таблицы через
# _base.make_table_handlers (DRY): добавление новой модели = 1 строка в таблице.


def _sambanova_handler_factory(model_key: str, cfg: dict):
    """Создаёт async-обработчик для модели по ключу из SAMBANOVA_MODELS."""
    model_name = cfg["model"]
    max_tokens = cfg["max_tokens"]
    temperature = cfg.get("temperature")
    response_field = cfg.get("response_field", "content")

    async def handler(messages: str, user_id: int) -> Tuple[str, Optional[int], bool]:
        return await _ask_sambanova_model_async(
            messages,
            user_id,
            model_name,
            max_tokens=max_tokens,
            temperature=temperature,
            response_field=response_field,
        )

    return handler


globals().update(
    make_table_handlers(
        SAMBANOVA_MODELS,
        _sambanova_handler_factory,
        doc_fn=lambda key, cfg: f"SambaNova {key} → {cfg['model']}",
    )
)
