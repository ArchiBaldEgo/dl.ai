"""Ollama API клиент — cloud-модели (вид ``<name>:cloud``) и локальный Ollama.

Использует официальную Python-библиотеку ``ollama`` (``from ollama import Client``).
Cloud-модели (glm-5.2:cloud, glm-5.3-flash:cloud, gemma4:cloud, qwen3.5:cloud,
nemotron-3-super:cloud, kimi-k2.7-code:cloud, kimi-k2.6:cloud, gpt-oss:20b-cloud,
gpt-oss:120b-cloud) требуют bearer-токен ``OLLAMA_API_KEY`` и
хост ``https://api.ollama.com``. Локальный Ollama работает без ключа на
``http://localhost:11434``.

Архитектура: generic ``_ask_ollama()`` + декларативная таблица ``OLLAMA_MODELS``;
внешние функции-обёртки генерируются автоматически через ``_make_handler`` (по
образцу openrouter.py). Обычный чат БЕЗ tool-calling и БЕЗ истории диалога —
препромпт подставляется на каждое сообщение (как у Groq).

Контракт handler'а (см. ai/services/model_caller.py:75):
``async def handler(msg: str, client_id: str) -> Tuple[str, int, bool]``.
"""

import asyncio
import logging
from typing import Tuple

from ollama import Client

from ._base import make_table_handlers
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
        "description": "Ollama GLM 5.2 — обычный чат",
    },
    "Ollama_Glm_5_3_Flash_Cloud": {
        "model": "glm-5.3-flash:cloud",
        "description": "Ollama GLM 5.3 Flash — обычный чат",
    },
    "Ollama_Gemma_4_Cloud": {
        "model": "gemma4:cloud",
        "description": "Ollama Gemma 4 — обычный чат",
    },
    "Ollama_Qwen_3_5_Cloud": {
        "model": "qwen3.5:cloud",
        "description": "Ollama Qwen 3.5 — обычный чат",
    },
    "Ollama_Nemotron_3_Super_Cloud": {
        "model": "nemotron-3-super:cloud",
        "description": "Ollama Nemotron 3 Super — обычный чат",
    },
    "Ollama_Kimi_K2_7_Code_Cloud": {
        "model": "kimi-k2.7-code:cloud",
        "description": "Ollama Kimi K2.7 Code — обычный чат",
    },
    "Ollama_Kimi_K2_6_Cloud": {
        "model": "kimi-k2.6:cloud",
        "description": "Ollama Kimi K2.6 — обычный чат",
    },
    "Ollama_Gpt_Oss_20B_Cloud": {
        "model": "gpt-oss:20b-cloud",
        "description": "Ollama GPT-OSS 20B — reasoning (thinking отдельным полем)",
    },
    "Ollama_Gpt_Oss_120B_Cloud": {
        "model": "gpt-oss:120b-cloud",
        "description": "Ollama GPT-OSS 120B — reasoning (thinking отдельным полем)",
    },
}


def _get_client() -> Client:
    """Создаёт Ollama Client с явным host/headers из config (не полагаемся на env SDK)."""
    headers = {"Authorization": f"Bearer {OLLAMA_API_KEY}"} if OLLAMA_API_KEY else None
    return Client(host=OLLAMA_HOST, timeout=_OLLAMA_TIMEOUT, headers=headers)


def _chat_sync(model: str, msg: str, temperature: float, num_predict: int) -> Tuple[str, int]:
    """Синхронный streaming-вызов ollama.chat (запускается через asyncio.to_thread).

    Возвращает ``(content, eval_count)``. Стриминг обязателен: без него длинные
    генерации ARM-solve (промпт ~5k символов + num_predict=4096) идут минутами
    без единого байта в ответ, и шлюз api.ollama.com закрывает соединение, не
    дождавшись готового ответа — httpx поднимает ServerDisconnectedError
    («Server disconnected without sending a response»). С потоковыми чанками
    соединение живёт, пока модель генерирует, а httpx-таймаут действует на
    каждый чанк, а не на весь ответ. eval_count приходит только в финальном
    чанке (done=True) — берём последний ненулевой.
    """
    client = _get_client()
    parts: list[str] = []
    tokens = 0
    for chunk in client.chat(
        model=model,
        messages=[{"role": "user", "content": msg}],
        options={"temperature": temperature, "num_predict": num_predict},
        stream=True,
    ):
        content = getattr(getattr(chunk, "message", None), "content", "") or ""
        if content:
            parts.append(content)
        if getattr(chunk, "eval_count", 0):
            tokens = int(chunk.eval_count)
    return "".join(parts), tokens


async def _ask_ollama(
    msg: str,
    user_id: int,
    model_id: str,
    *,
    temperature: float = 0.7,
    num_predict: int = 4096,
) -> Tuple[str, int, bool]:
    """Общий обработчик для всех моделей Ollama. Обычный чат без инструментов."""
    # Cloud-эндпоинт требует bearer-токен.
    if "api.ollama.com" in OLLAMA_HOST and not OLLAMA_API_KEY:
        return "Ollama API ключ не настроен. Добавьте OLLAMA_API_KEY в .env", 0, True

    try:
        content, tokens = await asyncio.to_thread(_chat_sync, model_id, msg, temperature, num_predict)
    except Exception as exc:  # ollama.ResponseError / httpx исключения / прочее
        # Импорт здесь, чтобы не падать при отсутствии SDK на старых окружениях.
        try:
            from ollama import ResponseError
        except Exception:  # pragma: no cover
            ResponseError = ()
        if ResponseError and isinstance(exc, ResponseError):
            logger.warning("Ollama API error for %s: %s", model_id, exc)
            return f"Ошибка Ollama API: {exc}", 0, True
        text = str(exc).lower()
        if "timeout" in text or "timed out" in text:
            return "Таймаут при подключении к Ollama. Попробуйте позже.", 0, True
        if "disconnected" in text:
            logger.warning("Ollama server disconnected for %s (long generation?): %s", model_id, exc)
            return (
                "Ollama оборвал соединение, не дождавшись ответа. "
                "Попробуйте позже или выберите другую модель.",
                0,
                True,
            )
        logger.exception("Ollama request failed for %s: %s", model_id, exc)
        return f"Ошибка Ollama: {exc}", 0, True

    if not content.strip():
        logger.warning("Ollama model %s returned empty content", model_id)
        return f"Модель Ollama ({model_id}) вернула пустой ответ. Попробуйте позже.", 0, True
    return content, int(tokens), False


# --- Автогенерация функций для каждой модели (через _base.make_table_handlers) ---


def _ollama_handler_factory(model_key: str, cfg: dict):
    model_id = cfg["model"]

    async def handler(msg: str, user_id: int) -> Tuple[str, int, bool]:
        return await _ask_ollama(msg, user_id, model_id)

    return handler


globals().update(
    make_table_handlers(
        OLLAMA_MODELS,
        _ollama_handler_factory,
        doc_fn=lambda key, cfg: cfg["description"],
    )
)