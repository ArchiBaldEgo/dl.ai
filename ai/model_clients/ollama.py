"""Ollama API клиент — cloud-модели (вид ``<name>:cloud``) и локальный Ollama.

Использует официальную Python-библиотеку ``ollama`` (``from ollama import Client``).
Cloud-модели (glm-5.2:cloud, gemma4:cloud, qwen3.5:cloud, nemotron-3-super:cloud,
kimi-k2.7-code:cloud, kimi-k2.6:cloud) требуют bearer-токен ``OLLAMA_API_KEY`` и
хост ``https://api.ollama.com``. Локальный Ollama работает без ключа на
``http://localhost:11434``.

Архитектура: generic ``_ask_ollama()`` + декларативная таблица ``OLLAMA_MODELS``;
внешние функции-обёртки генерируются автоматически через ``_make_handler`` (по
образцу openrouter.py). Обычный чат БЕЗ tool-calling и БЕЗ истории диалога —
препромпт подставляется на каждое сообщение (как у Groq).

Контракт handler'а (см. ai/services/model_caller.py:75):
``async def handler(msg: str, client_id: str) -> Tuple[str, int]``.
"""

import asyncio
import logging
from typing import Tuple

from ollama import Client

from .config import OLLAMA_HOST, OLLAMA_API_KEY

logger = logging.getLogger(__name__)

# Серверный таймаут одного chat-запроса (сек). Cloud-модели обычно быстрые,
# но длинные промпты могут требовать больше.
_OLLAMA_TIMEOUT = 120.0

# --- Декларативная таблица моделей ---
# registry-ключ → {model: имя модели Ollama, description: для __doc__}
OLLAMA_MODELS: dict[str, dict] = {
    "Ollama_Glm_5_2_Cloud": {
        "model": "glm-5.2:cloud",
        "description": "Ollama GLM 5.2 Cloud — обычный чат",
    },
    "Ollama_Gemma_4_Cloud": {
        "model": "gemma4:cloud",
        "description": "Ollama Gemma 4 Cloud — обычный чат",
    },
    "Ollama_Qwen_3_5_Cloud": {
        "model": "qwen3.5:cloud",
        "description": "Ollama Qwen 3.5 Cloud — обычный чат",
    },
    "Ollama_Nemotron_3_Super_Cloud": {
        "model": "nemotron-3-super:cloud",
        "description": "Ollama Nemotron 3 Super Cloud — обычный чат",
    },
    "Ollama_Kimi_K2_7_Code_Cloud": {
        "model": "kimi-k2.7-code:cloud",
        "description": "Ollama Kimi K2.7 Code Cloud — обычный чат",
    },
    "Ollama_Kimi_K2_6_Cloud": {
        "model": "kimi-k2.6:cloud",
        "description": "Ollama Kimi K2.6 Cloud — обычный чат",
    },
}


def _get_client() -> Client:
    """Создаёт Ollama Client с явным host/headers из config (не полагаемся на env SDK)."""
    headers = {"Authorization": f"Bearer {OLLAMA_API_KEY}"} if OLLAMA_API_KEY else None
    return Client(host=OLLAMA_HOST, timeout=_OLLAMA_TIMEOUT, headers=headers)


def _chat_sync(model: str, msg: str, temperature: float, num_predict: int):
    """Синхронный вызов ollama.chat (запускается через asyncio.to_thread)."""
    client = _get_client()
    return client.chat(
        model=model,
        messages=[{"role": "user", "content": msg}],
        options={"temperature": temperature, "num_predict": num_predict},
    )


async def _ask_ollama(
    msg: str,
    user_id: int,
    model_id: str,
    *,
    temperature: float = 0.7,
    num_predict: int = 4096,
) -> Tuple[str, int]:
    """Общий обработчик для всех моделей Ollama. Обычный чат без инструментов."""
    # Cloud-эндпоинт требует bearer-токен.
    if "api.ollama.com" in OLLAMA_HOST and not OLLAMA_API_KEY:
        return "Ollama API ключ не настроен. Добавьте OLLAMA_API_KEY в .env", 0

    try:
        resp = await asyncio.to_thread(_chat_sync, model_id, msg, temperature, num_predict)
    except Exception as exc:  # ollama.ResponseError / httpx.TimeoutException / прочее
        # Импорт здесь, чтобы не падать при отсутствии SDK на старых окружениях.
        try:
            from ollama import ResponseError
        except Exception:  # pragma: no cover
            ResponseError = ()
        if ResponseError and isinstance(exc, ResponseError):
            logger.warning("Ollama API error for %s: %s", model_id, exc)
            return f"Ошибка Ollama API: {exc}", 0
        text = str(exc).lower()
        if "timeout" in text or "timed out" in text:
            return "Таймаут при подключении к Ollama. Попробуйте позже.", 0
        logger.exception("Ollama request failed for %s: %s", model_id, exc)
        return f"Ошибка Ollama: {exc}", 0

    content = getattr(getattr(resp, "message", None), "content", "") or ""
    tokens = getattr(resp, "eval_count", 0) or 0
    return content, int(tokens)


# --- Автогенерация функций для каждой модели ---

def _make_handler(model_key: str):
    cfg = OLLAMA_MODELS[model_key]

    async def handler(msg: str, user_id: int) -> Tuple[str, int]:
        return await _ask_ollama(msg, user_id, cfg["model"])

    handler.__name__ = f"ask_{model_key}_async"
    handler.__qualname__ = handler.__name__
    handler.__doc__ = cfg["description"]
    return handler


# Экспорт
ask_Ollama_Glm_5_2_Cloud_async = _make_handler("Ollama_Glm_5_2_Cloud")
ask_Ollama_Gemma_4_Cloud_async = _make_handler("Ollama_Gemma_4_Cloud")
ask_Ollama_Qwen_3_5_Cloud_async = _make_handler("Ollama_Qwen_3_5_Cloud")
ask_Ollama_Nemotron_3_Super_Cloud_async = _make_handler("Ollama_Nemotron_3_Super_Cloud")
ask_Ollama_Kimi_K2_7_Code_Cloud_async = _make_handler("Ollama_Kimi_K2_7_Code_Cloud")
ask_Ollama_Kimi_K2_6_Cloud_async = _make_handler("Ollama_Kimi_K2_6_Cloud")