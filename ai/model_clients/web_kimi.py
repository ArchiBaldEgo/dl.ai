"""Клиент Web Kimi через бот-пул (Puppeteer-based).

Web Kimi обращается к внешнему бот-пулу (WebKimi/api/server.js) через HTTP API.
Бот-пул управляет Puppeteer-сессиями на сайте Kimi (kimi.moonshot.cn). Вход в Kimi
не автоматизируется (телефон/WeChat/email) — сессия сидируется вручную в постоянный
Chrome-профиль. Поддерживает автоперезапуск (restart_kimi_bot_pool).

Вся логика протокола пула (ретраи, Retry-After, автоперезапуск) — в
``_base.BotPoolClient``; этот модуль задаёт только конфигурацию пула.
"""

from ._base import BotPoolClient
from .config import KIMI_POOL_URL

_client = BotPoolClient(
    base_url=KIMI_POOL_URL,
    model="kimi",
    label="Web Kimi",
    provider="web_kimi",
    site_label="Kimi",
    selector_doc="WebKimi/worker/data.json",
)


def restart_kimi_bot_pool(timeout_seconds: int = 30) -> bool:
    """Автоперезапуск воркеров Kimi бот-пула (для health-check)."""
    return _client.restart(timeout_seconds)


async def ask_Web_Kimi_async(msg: str, user_id: int) -> tuple[str, int, bool]:
    # У Kimi одна модель (без отдельного thinking-режима) — thinking всегда False.
    return await _client.ask(msg, user_id, thinking=False)