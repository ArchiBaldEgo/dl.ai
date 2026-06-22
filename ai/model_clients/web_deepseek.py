"""Bot-pool based Web DeepSeek clients."""

import json
import logging
from typing import Tuple

import requests

from .config import BOT_POOL_URL
from .exceptions import safe_parse_response

logger = logging.getLogger(__name__)


def _post_to_bot_pool(payload: dict, timeout_seconds: int = 120) -> requests.Response:
    """Internal service call must bypass env proxies (HTTP_PROXY/HTTPS_PROXY)."""
    with requests.Session() as session:
        session.trust_env = False
        return session.post(
            f"{BOT_POOL_URL}/api/send",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=timeout_seconds,
        )


def restart_bot_pool(timeout_seconds: int = 30) -> bool:
    """Ask the bot pool to restart its workers (автоподъём).

    Returns True if the pool acknowledged the restart, False on network/HTTP
    failure. Never raises — callers (the health check) treat a failed restart
    as "still down" and log it.
    """
    try:
        with requests.Session() as session:
            session.trust_env = False
            response = session.post(
                f"{BOT_POOL_URL}/api/restart",
                json={},
                headers={"Content-Type": "application/json"},
                timeout=timeout_seconds,
            )
        if response.status_code < 300:
            logger.info("Bot pool restart acknowledged: %s", response.text[:200])
            return True
        logger.warning("Bot pool restart returned HTTP %s", response.status_code)
        return False
    except Exception as exc:
        logger.warning("Bot pool restart failed: %s", exc)
        return False


async def _ask_web_deepseek_common(msg: str, user_id: int, thinking: bool) -> Tuple[str, int]:
    payload = {
        "model": "deepseek",
        "user_id": user_id,
        "thinking": thinking,
        "message": msg,
    }
    max_attempts = 4
    for attempt in range(1, max_attempts + 1):
        response = await __import__("asyncio").to_thread(_post_to_bot_pool, payload, 120)
        logger.debug("Bot pool response status: %s (attempt %s/%s)", response.status_code, attempt, max_attempts)

        if response.status_code == 200:
            obj, error_message = safe_parse_response(response.text)
            if obj is None:
                return error_message, 0
            return obj["data"]["content"], 0

        if response.status_code in (503, 504) and attempt < max_attempts:
            await __import__("asyncio").sleep(attempt * 2)
            continue

        if response.status_code == 400:
            return "Неправильный запрос", 0
        if response.status_code == 401:
            return "Бот не авторизован. Проверьте логин/пароль", 0
        if response.status_code == 429:
            return "Все боты заняты", 0
        if response.status_code >= 503:
            return "Бот инициализируется слишком долго. Попробуйте позже.", 0

        return f"Ошибка сервиса Web DeepSeek (код {response.status_code}).", 0


async def ask_Web_DeepSeek_Thinking_async(msg: str, user_id: int) -> str:
    response, _ = await _ask_web_deepseek_common(msg, user_id, thinking=True)
    return response


async def ask_Web_DeepSeek_async(msg: str, user_id: int) -> str:
    response, _ = await _ask_web_deepseek_common(msg, user_id, thinking=False)
    return response
