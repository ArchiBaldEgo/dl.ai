"""SambaNova model clients (DeepSeek, Llama, MiniMax, Gemma, GPT-OSS)."""

import json
import logging
from asyncio import TimeoutError as AsyncTimeoutError
from typing import Tuple, Optional

import requests

from .config import (
    SAMBANOVA_MODEL_DEEPSEEK,
    SAMBANOVA_MODEL_DEEPSEEK_R1_DISTILL_LLAMA_70B,
    SAMBANOVA_MODEL_DEEPSEEK_V3_1,
    SAMBANOVA_MODEL_DEEPSEEK_V3_1_CB,
    SAMBANOVA_MODEL_DEEPSEEK_V3_2,
    SAMBANOVA_MODEL_GEMMA_3_12B_IT,
    SAMBANOVA_MODEL_GPT_OSS,
    SAMBANOVA_MODEL_LLAMA_4_MAVERICK_17B_128E_INSTRUCT,
    SAMBANOVA_MODEL_META,
    SAMBANOVA_MODEL_META_LLAMA_3_3_70B_INSTRUCT,
    SAMBANOVA_MODEL_MINIMAX_M2_5,
    SAMBANOVA_MODEL_MINIMAX_M2_7,
    SAMBANOVA_MODEL_MIXTRAL_ALIAS,
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


def _append_history(user_id, message: str, response_text: str) -> None:
    conversation_history.add_exchange(user_id, message, response_text)


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
) -> Tuple[str, Optional[int]]:
    """Generic SambaNova chat completion wrapper with history management."""
    history = conversation_history.get(user_id)
    history.append({"role": "user", "content": messages})

    payload: dict = {
        "model": model_name,
        "messages": history,
        "max_tokens": max_tokens,
    }
    if temperature is not None:
        payload["temperature"] = temperature

    try:
        response = await __import__("asyncio").to_thread(
            requests.post,
            "https://api.sambanova.ai/v1/chat/completions",
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
            return extract_api_error_text(str(response.status_code)), "0"

        obj, error_message = safe_parse_response(response.text)
        if obj is None:
            return error_message, "0"

        if "choices" not in obj or not obj["choices"]:
            logger.warning("Unexpected response structure: %s", obj)
            return "Неожиданный формат ответа от сервера.", "0"

        completion_tokens = obj.get("usage", {}).get("completion_tokens", 0)
        assistant_content = extract_choice_content(obj)
        conversation_history.append(user_id, {"role": "assistant", "content": assistant_content})
        return assistant_content, completion_tokens

    except requests.exceptions.ConnectionError as e:
        logger.warning("Connection error: %s", e)
        return classify_network_error(e), "0"
    except requests.exceptions.Timeout:
        logger.warning("Timeout connecting to API")
        return "Таймаут при подключении к серверу. Попробуйте позже.", "0"
    except requests.exceptions.RequestException as e:
        logger.warning("Request error: %s", e)
        return "Ошибка при подключении к серверу API.", "0"
    except Exception as e:
        logger.exception("Unexpected error in SambaNova call")
        if is_network_error(e):
            return "Ошибка подключения. Ваш контекст сохранен, попробуйте позже.", "0"
        if is_missing_choices_error(e):
            return "Ошибка в ответе от сервера AI.", "0"
        conversation_history.reset(user_id)
        return "Что-то пошло не так. Контекст очищен, введите новый запрос.", "0"


async def ask_DeepSeek_R1_Distill_Llama_70B_async(messages: str, user_id: int) -> Tuple[str, Optional[int]]:
    return await _ask_sambanova_model_async(
        messages,
        user_id,
        SAMBANOVA_MODEL_DEEPSEEK_R1_DISTILL_LLAMA_70B,
        max_tokens=9000,
        temperature=0.7,
    )


async def ask_DeepSeek_V3_1_async(messages: str, user_id: int) -> Tuple[str, Optional[int]]:
    return await _ask_sambanova_model_async(
        messages,
        user_id,
        SAMBANOVA_MODEL_DEEPSEEK_V3_1,
        max_tokens=9000,
        temperature=0.7,
    )


async def ask_DeepSeek_V3_1_cb_async(messages: str, user_id: int) -> Tuple[str, Optional[int]]:
    return await _ask_sambanova_model_async(messages, user_id, SAMBANOVA_MODEL_DEEPSEEK_V3_1_CB, max_tokens=9000)


async def ask_DeepSeek_V3_2_async(messages: str, user_id: int) -> Tuple[str, Optional[int]]:
    return await _ask_sambanova_model_async(messages, user_id, SAMBANOVA_MODEL_DEEPSEEK_V3_2, max_tokens=9000)


async def ask_Llama_4_Maverick_17B_128E_Instruct_async(messages: str, user_id: int) -> Tuple[str, Optional[int]]:
    return await _ask_sambanova_model_async(
        messages,
        user_id,
        SAMBANOVA_MODEL_LLAMA_4_MAVERICK_17B_128E_INSTRUCT,
        max_tokens=9000,
    )


async def ask_Meta_Llama_3_3_70B_Instruct_async(messages: str, user_id: int) -> Tuple[str, Optional[int]]:
    return await _ask_sambanova_model_async(
        messages,
        user_id,
        SAMBANOVA_MODEL_META_LLAMA_3_3_70B_INSTRUCT,
        max_tokens=9000,
    )


async def ask_MiniMax_M2_5_async(messages: str, user_id: int) -> Tuple[str, Optional[int]]:
    return await _ask_sambanova_model_async(messages, user_id, SAMBANOVA_MODEL_MINIMAX_M2_5, max_tokens=9000)


async def ask_MiniMax_M2_7_async(messages: str, user_id: int) -> Tuple[str, Optional[int]]:
    return await _ask_sambanova_model_async(messages, user_id, SAMBANOVA_MODEL_MINIMAX_M2_7, max_tokens=9000)


async def ask_Gemma_3_12b_it_async(messages: str, user_id: int) -> Tuple[str, Optional[int]]:
    return await _ask_sambanova_model_async(messages, user_id, SAMBANOVA_MODEL_GEMMA_3_12B_IT, max_tokens=9000)


async def ask_DeepSeek_R1_async(messages: str, user_id: int, timeout: float = 25.0) -> Tuple[str, Optional[int]]:
    """Legacy DeepSeek-R1 entry point kept for backward compatibility.

    Note: This uses the env-configurable ``SAMBANOVA_MODEL_DEEPSEEK`` alias
    (default DeepSeek-V3.1).  Newer code should use the explicit model helpers.
    """
    history = conversation_history.get(user_id)
    history.append({"role": "user", "content": messages})

    payload = {
        "model": SAMBANOVA_MODEL_DEEPSEEK,
        "messages": history,
        "max_tokens": 9000,
        "temperature": 0.7,
        "stream": False,
    }

    try:
        response = await __import__("asyncio").wait_for(
            __import__("asyncio").to_thread(
                requests.post,
                "https://api.sambanova.ai/v1/chat/completions",
                json=payload,
                headers={
                    "Authorization": f"Bearer {SC_TOKEN}",
                    "Content-Type": "application/json",
                },
                proxies=proxies,
                timeout=30,
            ),
            timeout=timeout,
        )

        _log_response(response)

        if response.status_code != 200:
            return extract_api_error_text(str(response.status_code)), "0"

        obj, error_message = safe_parse_response(response.text)
        if obj is None:
            return error_message, "0"

        if "choices" not in obj or not obj["choices"]:
            return "Неожиданный формат ответа от сервера.", "0"

        completion_tokens = obj.get("usage", {}).get("completion_tokens", 0)
        assistant_content = obj["choices"][0]["message"].get("content", "")
        conversation_history.append(user_id, {"role": "assistant", "content": assistant_content})
        return assistant_content, completion_tokens

    except AsyncTimeoutError:
        logger.warning("DeepSeek-R1 request timeout after %s seconds", timeout)
        return f"Таймаут запроса ({timeout} сек). Сервер долго не отвечает. Попробуйте позже или уменьшите запрос.", "0"
    except requests.exceptions.Timeout:
        return "Таймаут при подключении к серверу. Попробуйте позже.", "0"
    except Exception as e:
        logger.exception("Unexpected error in DeepSeek-R1 call")
        if is_network_error(e):
            return "Отсутствует подключение к интернету.", "0"
        if is_missing_choices_error(e):
            return "Ошибка в ответе от сервера AI.", "0"
        conversation_history.reset(user_id)
        return "Что-то пошло не так. Контекст очищен, введите новый запрос.", "0"


async def ask_Gpt_oss_120b_async(messages: str, user_id: int) -> Tuple[str, Optional[int]]:
    history = conversation_history.get(user_id)
    history.append({"role": "user", "content": messages})

    payload = {
        "model": SAMBANOVA_MODEL_GPT_OSS,
        "messages": history,
        "max_tokens": 8192,
    }

    try:
        response = await __import__("asyncio").to_thread(
            requests.post,
            "https://api.sambanova.ai/v1/chat/completions",
            json=payload,
            headers={
                "Authorization": f"Bearer {SC_TOKEN}",
                "Content-Type": "application/json",
            },
            proxies=proxies,
            timeout=30,
        )

        _log_response(response)

        if response.status_code != 200:
            return extract_api_error_text(str(response.status_code)), "0"

        obj, error_message = safe_parse_response(response.text)
        if obj is None:
            return error_message, "0"

        completion_tokens = obj.get("usage", {}).get("completion_tokens", 0)
        message = obj["choices"][0].get("message", {})
        assistant_content = message.get("content") or message.get("reasoning") or "Пустой ответ от модели."
        conversation_history.append(user_id, {"role": "assistant", "content": assistant_content})
        return assistant_content, completion_tokens

    except requests.exceptions.ConnectionError as e:
        logger.warning("Connection error: %s", e)
        return classify_network_error(e), "0"
    except requests.exceptions.Timeout:
        return "Таймаут при подключении к серверу. Попробуйте позже.", "0"
    except requests.exceptions.RequestException as e:
        logger.warning("Request error: %s", e)
        return "Ошибка при подключении к серверу API.", "0"
    except KeyError as e:
        if is_missing_choices_error(e):
            return "Ошибка в ответе от сервера AI.", "0"
        raise
    except Exception as e:
        logger.exception("Unexpected error in GPT-OSS call")
        if is_network_error(e):
            return "Ошибка подключения. Ваш контекст сохранен, попробуйте позже.", "0"
        conversation_history.reset(user_id)
        return "Что-то пошло не так. Контекст очищен, введите новый запрос.", "0"


async def ask_Meta_Llama_3_1_70B_Instruct_async(messages: str, user_id: int) -> str:
    """Legacy Meta-Llama alias kept for backward compatibility.

    It routes to the currently configured Meta-Llama model.
    """
    response, _tokens = await _ask_sambanova_model_async(
        messages,
        user_id,
        SAMBANOVA_MODEL_META,
        max_tokens=9000,
    )
    return response


async def ask_Mixtral_8x22b_async(messages: str, user_id: int) -> Tuple[str, Optional[int]]:
    """Legacy Mixtral alias kept for backward compatibility.

    It routes to the currently configured fallback model.
    """
    return await _ask_sambanova_model_async(
        messages,
        user_id,
        SAMBANOVA_MODEL_MIXTRAL_ALIAS,
        max_tokens=9000,
    )
