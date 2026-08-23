"""Клиенты Web DeepSeek через бот-пул (Puppeteer-based).

Web DeepSeek и Web DeepSeek Thinking обращаются к внешнему бот-пулу
(WebDeepseek/api/server.js) через HTTP API. Бот-пул управляет Puppeteer-сессиями
на сайте DeepSeek. Поддерживает автоперезапуск (restart_bot_pool).
"""

import asyncio
import json
import logging
from typing import Tuple

import requests

from ._base import proxy_bypass_session
from .config import BOT_POOL_URL
from .exceptions import map_http_error, safe_parse_response

logger = logging.getLogger(__name__)


def _retry_after_seconds(response) -> int:
    """Retry-After header от bot-пула (429/503 — боты заняты/стартуют), 0 если нет."""
    try:
        val = int((response.headers.get("Retry-After") or "").strip())
        if val > 0:
            return val
    except (TypeError, ValueError):
        pass
    return 0


def _post_to_bot_pool(payload: dict, timeout_seconds: int = 120) -> requests.Response:
    """Internal service call must bypass env proxies (HTTP_PROXY/HTTPS_PROXY)."""
    session = proxy_bypass_session()
    try:
        return session.post(
            f"{BOT_POOL_URL}/api/send",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=timeout_seconds,
        )
    finally:
        session.close()


def restart_bot_pool(timeout_seconds: int = 30) -> bool:
    """Ask the bot pool to restart its workers (автоподъём).

    Returns True if the pool acknowledged the restart, False on network/HTTP
    failure. Never raises — callers (the health check) treat a failed restart
    as "still down" and log it.
    """
    session = proxy_bypass_session()
    try:
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
    finally:
        session.close()


async def _ask_web_deepseek_common(msg: str, user_id: int, thinking: bool) -> Tuple[str, int, bool]:
    payload = {
        "model": "deepseek",
        "user_id": user_id,
        "thinking": thinking,
        "message": msg,
    }
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            response = await asyncio.to_thread(_post_to_bot_pool, payload, 300)
        except requests.Timeout:
            if attempt < max_attempts:
                wait = min(attempt * 5, 20)
                logger.warning("Bot pool timeout (300s), retrying in %ss (attempt %s/%s)", wait, attempt, max_attempts)
                await asyncio.sleep(wait)
                continue
            return "Таймаут при подключении к Web DeepSeek (300с). Попробуйте позже.", 0, True
        except requests.ConnectionError as exc:
            if attempt < max_attempts:
                wait = min(attempt * 3, 15)
                logger.warning("Bot pool connection error: %s, retrying in %ss (attempt %s/%s)", exc, wait, attempt, max_attempts)
                await asyncio.sleep(wait)
                continue
            return f"Ошибка подключения к Web DeepSeek: {exc}", 0, True

        logger.debug("Bot pool response status: %s (attempt %s/%s)", response.status_code, attempt, max_attempts)

        if response.status_code == 200:
            obj, error_message = safe_parse_response(response.text)
            if obj is None:
                return error_message, 0, True
            content = obj.get("data", {}).get("content", "") or ""
            if not content.strip():
                logger.warning("Web DeepSeek returned 200 but empty content (attempt %s/%s)", attempt, max_attempts)
                if attempt < max_attempts:
                    await asyncio.sleep(min(attempt * 3, 15))
                    continue
                return "Модель Web DeepSeek вернула пустой ответ. Попробуйте позже.", 0, True
            return content, 0, False

        if response.status_code in (429, 500, 502, 503, 504):
            # 429 — все боты pool заняты (pool шлёт Retry-After, ~3с): транзитная
            # загрузка, короткое ожидание часто даёт боту освободиться — ретраимся.
            # 5xx — серверная ошибка pool/бота. Особый случай: DeepSeek изменил
            # вёрстку → селекторы устарели, retry бесполезен. Сразу вернём понятную
            # ошибку (экономит ~15 мин 3×300с retry-ей).
            reason = ""
            if response.status_code != 429:
                try:
                    obj, _ = safe_parse_response(response.text)
                    if obj:
                        reason = obj.get("reason") or ""
                except Exception:
                    pass
                if "UI may have changed" in reason or "All answer XPath selectors failed" in reason:
                    return ("DeepSeek изменил интерфейс сайта — селекторы bot-пула устарели, "
                            "нужно обновить WebDeepseek/worker/data.json."), 0, True
            if attempt < max_attempts:
                # Уважаем Retry-After (429/503 — бот освобождается/стартует),
                # иначе мягкий backoff; ограничиваем сверху, чтобы не висеть.
                wait = _retry_after_seconds(response) or min(attempt * 3, 15)
                wait = min(wait, 20)
                logger.warning("Bot pool returned %s, retrying in %ss (attempt %s/%s)", response.status_code, wait, attempt, max_attempts)
                await asyncio.sleep(wait)
                continue

        return map_http_error(response.status_code, "web_deepseek"), 0, True


async def ask_Web_DeepSeek_Thinking_async(msg: str, user_id: int) -> Tuple[str, int, bool]:
    return await _ask_web_deepseek_common(msg, user_id, thinking=True)


async def ask_Web_DeepSeek_async(msg: str, user_id: int) -> Tuple[str, int, bool]:
    return await _ask_web_deepseek_common(msg, user_id, thinking=False)
