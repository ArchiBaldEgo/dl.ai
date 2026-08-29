"""Клиенты Web DeepSeek через бот-пул (Puppeteer-based).

Web DeepSeek и Web DeepSeek Thinking обращаются к внешнему бот-пулу
(WebDeepseek/api/server.js) через HTTP API. Бот-пул управляет Puppeteer-сессиями
на сайте DeepSeek. Поддерживает автоперезапуск (restart_bot_pool).

Вся логика протокола пула (ретраи, Retry-After, автоперезапуск) — в
``_base.BotPoolClient``; этот модуль задаёт только конфигурацию пула.
"""

from ._base import BotPoolClient
from .config import BOT_POOL_URL

_client = BotPoolClient(
    base_url=BOT_POOL_URL,
    model="deepseek",
    label="Web DeepSeek",
    provider="web_deepseek",
    site_label="DeepSeek",
    selector_doc="WebDeepseek/worker/data.json",
)


def restart_bot_pool(timeout_seconds: int = 30) -> bool:
    """Автоперезапуск воркеров Web DeepSeek бот-пула (для health-check)."""
    return _client.restart(timeout_seconds)


async def ask_Web_DeepSeek_Thinking_async(msg: str, user_id: int) -> tuple[str, int, bool]:
    return await _client.ask(msg, user_id, thinking=True)


async def ask_Web_DeepSeek_async(msg: str, user_id: int) -> tuple[str, int, bool]:
    return await _client.ask(msg, user_id, thinking=False)