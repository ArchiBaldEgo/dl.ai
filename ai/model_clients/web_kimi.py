"""Клиент Web Kimi через бот-пул (Puppeteer-based).

Web Kimi обращается к внешнему бот-пулу (WebKimi/api/server.js) через HTTP API.
Бот-пул управляет Puppeteer-сессиями на сайте Kimi (kimi.moonshot.cn). Вход в Kimi
не автоматизируется (телефон/WeChat/email) — сессия сидируется вручную в постоянный
Chrome-профиль. Поддерживает автоперезапуск (restart_kimi_bot_pool).
"""

import asyncio
import json
import logging
from typing import Tuple

import requests

from ._base import proxy_bypass_session
from .config import KIMI_POOL_URL
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
            f"{KIMI_POOL_URL}/api/send",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=timeout_seconds,
        )
    finally:
        session.close()


def restart_kimi_bot_pool(timeout_seconds: int = 30) -> bool:
    """Ask the Kimi bot pool to restart its workers (автоподъём).

    Returns True if the pool acknowledged the restart, False on network/HTTP
    failure. Never raises — callers (the health check) treat a failed restart
    as "still down" and log it.
    """
    session = proxy_bypass_session()
    try:
        response = session.post(
            f"{KIMI_POOL_URL}/api/restart",
            json={},
            headers={"Content-Type": "application/json"},
            timeout=timeout_seconds,
        )
        if response.status_code < 300:
            logger.info("Kimi bot pool restart acknowledged: %s", response.text[:200])
            return True
        logger.warning("Kimi bot pool restart returned HTTP %s", response.status_code)
        return False
    except Exception as exc:
        logger.warning("Kimi bot pool restart failed: %s", exc)
        return False
    finally:
        session.close()


async def _ask_web_kimi_common(msg: str, user_id: int, thinking: bool) -> Tuple[str, int, bool]:
    payload = {
        "model": "kimi",
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
                logger.warning("Kimi bot pool timeout (300s), retrying in %ss (attempt %s/%s)", wait, attempt, max_attempts)
                await asyncio.sleep(wait)
                continue
            return "Таймаут при подключении к Web Kimi (300с). Попробуйте позже.", 0, True
        except requests.ConnectionError as exc:
            if attempt < max_attempts:
                wait = min(attempt * 3, 15)
                logger.warning("Kimi bot pool connection error: %s, retrying in %ss (attempt %s/%s)", exc, wait, attempt, max_attempts)
                await asyncio.sleep(wait)
                continue
            return f"Ошибка подключения к Web Kimi: {exc}", 0, True

        logger.debug("Kimi bot pool response status: %s (attempt %s/%s)", response.status_code, attempt, max_attempts)

        if response.status_code == 200:
            obj, error_message = safe_parse_response(response.text)
            if obj is None:
                return error_message, 0, True
            content = obj.get("data", {}).get("content", "") or ""
            if not content.strip():
                logger.warning("Web Kimi returned 200 but empty content (attempt %s/%s)", attempt, max_attempts)
                if attempt < max_attempts:
                    await asyncio.sleep(min(attempt * 3, 15))
                    continue
                return "Модель Web Kimi вернула пустой ответ. Попробуйте позже.", 0, True
            return content, 0, False

        if response.status_code in (429, 500, 502, 503, 504):
            # 429 — все боты pool заняты (pool шлёт Retry-After, ~3с): транзитная
            # загрузка, короткое ожидание часто даёт боту освободиться — ретраимся.
            # 5xx — серверная ошибка pool/бота. Особый случай: Kimi изменил вёрстку
            # → селекторы устарели, retry бесполезен. Сразу вернём понятную ошибку
            # (экономит ~15 мин 3×300с retry-ей).
            reason = ""
            if response.status_code != 429:
                try:
                    obj, _ = safe_parse_response(response.text)
                    if obj:
                        reason = obj.get("reason") or ""
                except Exception:
                    pass
                if "UI may have changed" in reason or "All answer XPath selectors failed" in reason:
                    return ("Kimi изменил интерфейс сайта — селекторы bot-пула устарели, "
                            "нужно обновить WebKimi/worker/data.json."), 0, True
            if attempt < max_attempts:
                # Уважаем Retry-After (429/503 — бот освобождается/стартует),
                # иначе мягкий backoff; ограничиваем сверху, чтобы не висеть.
                wait = _retry_after_seconds(response) or min(attempt * 3, 15)
                wait = min(wait, 20)
                logger.warning("Kimi bot pool returned %s, retrying in %ss (attempt %s/%s)", response.status_code, wait, attempt, max_attempts)
                await asyncio.sleep(wait)
                continue

        return map_http_error(response.status_code, "web_kimi"), 0, True


async def ask_Web_Kimi_async(msg: str, user_id: int) -> Tuple[str, int, bool]:
    # У Kimi одна модель (без отдельного thinking-режима) — thinking всегда False.
    return await _ask_web_kimi_common(msg, user_id, thinking=False)