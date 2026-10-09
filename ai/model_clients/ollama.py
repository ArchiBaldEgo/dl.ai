"""Ollama API клиент — cloud-модели (вид ``<name>:cloud``) и локальный Ollama.

Использует официальную Python-библиотеку ``ollama`` (``from ollama import Client``).
Cloud-модели (glm-5.2:cloud, deepseek-v4.1-flash:cloud, deepseek-v4-pro:cloud,
gemma4:cloud, nemotron-3-super:cloud, kimi-k2.7-code:cloud, kimi-k2.6:cloud,
gpt-oss:20b-cloud, gpt-oss:120b-cloud) требуют bearer-токен ``OLLAMA_API_KEY`` и
хост ``https://ollama.com`` (облачный REST живёт на ``https://ollama.com/api/*``).
Локальный Ollama работает без ключа на ``http://localhost:11434``.

Архитектура: generic ``_ask_ollama()`` + декларативная таблица ``OLLAMA_MODELS``;
внешние функции-обёртки генерируются автоматически через ``_make_handler`` (по
образцу openrouter.py). Обычный чат БЕЗ tool-calling и БЕЗ истории диалога —
препромпт подставляется на каждое сообщение (как у Groq).

Контракт handler'а (см. ai/services/model_caller.py:75):
``async def handler(msg: str, client_id: str) -> Tuple[str, int, bool]``.
"""

import asyncio
import logging
import os
import random
import threading
from typing import Tuple

from ollama import Client

from ._base import make_table_handlers
from .config import OLLAMA_HOST, OLLAMA_API_KEY
from .exceptions import map_http_error

logger = logging.getLogger(__name__)

# Серверный таймаут одного chat-запроса (сек). Cloud-модели обычно быстрые,
# но длинные промпты могут требовать больше.
_OLLAMA_TIMEOUT = 120.0

# --- Защита от HTTP 429 (rate limit) Ollama Cloud ---
# Ollama Cloud лимитирует не «запросы в минуту», а ОДНОВРЕМЕННЫЕ запросы по тарифу
# (Free≈1, Pro≈3-4, Max≈10). Сверх лимита — 429. Поэтому шейпим конкурентность
# (пер-process threading-семафор, очередь вместо отказа) и ретраим 429 с
# эскалирующим backoff. Значения читаются лениво из os.environ (НЕ в import-time
# константы, чтобы тесты переопределяли через patch.dict; load_dotenv в config.py
# отрабатывает при импорте раньше первого запроса, так что .env-значения видны).
_RETRY_DELAYS = (5.0, 15.0, 30.0)  # 429 держится, пока не освободится слот тарифа
_QUEUE_LOG_AFTER = 10.0            # сек ожидания в очереди, после которых пишем в лог

_SEM: threading.BoundedSemaphore | None = None
_SEM_SIZE = 0
_SEM_LOCK = threading.Lock()


def _get_semaphore() -> threading.BoundedSemaphore:
    """Per-process ограничитель одновременных Ollama-вызовов.

    Локальный Ollama (localhost:11434) проходит через тот же семафор — осознанно:
    один кодовый путь (KISS), а локальному это тоже не вредит (генерация всё равно
    сериализуется GPU). Кому нужен параллельный локальный — поднимет env-лимит.
    """
    global _SEM, _SEM_SIZE
    limit = max(1, int(os.getenv("OLLAMA_MAX_CONCURRENCY", "3")))
    if _SEM is None or _SEM_SIZE != limit:
        with _SEM_LOCK:  # double-checked locking
            if _SEM is None or _SEM_SIZE != limit:
                _SEM = threading.BoundedSemaphore(limit)
                _SEM_SIZE = limit
    return _SEM


def _reset_limiter() -> None:
    """Сброс кэша семафора — только для тестов (module state между кейсами)."""
    global _SEM
    with _SEM_LOCK:
        _SEM = None


def _retry_delay(attempt: int) -> float:
    """Пауза перед повтором при 429: ~5 / ~15 / ~30 сек с джиттером ±20%."""
    base = _RETRY_DELAYS[min(attempt - 1, len(_RETRY_DELAYS) - 1)]
    return base * random.uniform(0.8, 1.2)


async def _sleep(seconds: float) -> None:
    """Обёртка asyncio.sleep — чтобы тесты патчили паузу, не спали реально.

    Ретраить можно только так: blocking sleep в корутине заморозил бы весь
    event loop Daphne на 5-30 сек.
    """
    await asyncio.sleep(seconds)


def _is_rate_limited(exc: Exception) -> bool:
    """True только для ollama.ResponseError с HTTP-кодом 429."""
    try:
        from ollama import ResponseError
    except Exception:  # pragma: no cover — SDK отсутствует
        return False
    return isinstance(exc, ResponseError) and getattr(exc, "status_code", None) == 429

# --- Декларативная таблица моделей ---
# registry-ключ → {model: имя модели Ollama, description: для __doc__}
OLLAMA_MODELS: dict[str, dict] = {
    "Ollama_Glm_5_3_Flash_Cloud": {
        "model": "glm-5.3-flash:cloud",
        "description": "Ollama GLM 5.3 Flash — обычный чат",
    },
    "Ollama_Glm_5_2_Cloud": {
        "model": "glm-5.2:cloud",
        "description": "Ollama GLM 5.2 — обычный чат",
    },
    "Ollama_DeepSeek_V4_1_Flash_Cloud": {
        "model": "deepseek-v4.1-flash:cloud",
        "description": "Ollama DeepSeek 4.1 Flash — обычный чат",
    },
    "Ollama_DeepSeek_V4_Pro_Cloud": {
        "model": "deepseek-v4-pro:cloud",
        "description": "Ollama DeepSeek V4 Pro — обычный чат",
    },
    "Ollama_Gemma_4_Cloud": {
        "model": "gemma4:cloud",
        "description": "Ollama Gemma 4 — обычный чат",
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


def _stream_once(model: str, msg: str, temperature: float, num_predict: int) -> Tuple[str, int, str]:
    """Синхронный streaming-вызов ollama.chat (запускается через asyncio.to_thread).

    Возвращает ``(content, eval_count, thinking)``. Стриминг обязателен: без
    него длинные генерации ARM-solve (промпт ~5k символов + num_predict=4096)
    идут минутами без единого байта в ответ, и шлюз api.ollama.com закрывает
    соединение, не дождавшись готового ответа — httpx поднимает
    ServerDisconnectedError («Server disconnected without sending a response»).
    С потоковыми чанками соединение живёт, пока модель генерирует, а
    httpx-таймаут действует на каждый чанк, а не на весь ответ. eval_count
    приходит только в финальном чанке (done=True) — берём последний ненулевой.

    ``thinking`` — накопленное содержимое одноимённого поля (gpt-oss:cloud
    стримит рассуждение отдельным полем, не think-тегами в content);
    обычно отбрасывается, но спасает ответ, когда content пуст.
    """
    client = _get_client()
    parts: list[str] = []
    thinking_parts: list[str] = []
    tokens = 0
    for chunk in client.chat(
        model=model,
        messages=[{"role": "user", "content": msg}],
        options={"temperature": temperature, "num_predict": num_predict},
        stream=True,
    ):
        message = getattr(chunk, "message", None)
        content = getattr(message, "content", "") or ""
        if content:
            parts.append(content)
        thinking = getattr(message, "thinking", "") or ""
        if thinking:
            thinking_parts.append(thinking)
        if getattr(chunk, "eval_count", 0):
            tokens = int(chunk.eval_count)
    return "".join(parts), tokens, "".join(thinking_parts)


def _chat_sync(model: str, msg: str, temperature: float, num_predict: int) -> Tuple[str, int, str]:
    """Запускает ``_stream_once`` под per-process семафором конкурентности.

    Блокирующий acquire — ВНУТРИ worker-потока (asyncio.to_thread), не в корутине:
    иначе ожидающие корутины того же event loop заблокировали бы Daphne на acquire.
    Слот держится всю стрим-сессию — тариф считает открытые соединения.
    try/finally обязателен: исключение (в т.ч. 429) не должно утащить слот
    (BoundedSemaphore сам кинет ValueError при лишнем release).
    """
    sem = _get_semaphore()
    waited = 0.0
    while not sem.acquire(timeout=1):  # цикл ради лог-наблюдаемости очереди
        waited += 1.0
        if waited >= _QUEUE_LOG_AFTER:
            logger.info(
                "Ollama concurrency queue: waiting %.0fs (limit=%d, model=%s)",
                waited, _SEM_SIZE, model,
            )
    try:
        return _stream_once(model, msg, temperature, num_predict)
    finally:
        sem.release()


def _handle_ollama_error(exc: Exception, model_id: str) -> Tuple[str, int, bool]:
    """Преобразует исключение вызова модели в (текст_ошибки, 0, True).

    Поведение прежнее (таймаут/disconnected/generic), кроме ResponseError 429,
    дошедшего сюда после исчерпания ретраев, — для него friendly-сообщение из
    map_http_error вместо сырого «... (status code: 429)».
    """
    # Импорт здесь, чтобы не падать при отсутствии SDK на старых окружениях.
    try:
        from ollama import ResponseError
    except Exception:  # pragma: no cover
        ResponseError = ()
    if ResponseError and isinstance(exc, ResponseError):
        if getattr(exc, "status_code", None) == 429:
            logger.error("Ollama 429 for %s после всех ретраев", model_id)
            return map_http_error(429, "ollama"), 0, True
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


async def _ask_ollama(
    msg: str,
    user_id: int,
    model_id: str,
    *,
    temperature: float = 0.7,
    num_predict: int = 4096,
) -> Tuple[str, int, bool]:
    """Общий обработчик для всех моделей Ollama. Обычный чат без инструментов.

    Ретраит ollama.ResponseError 429 (rate limit тарифа) с эскалирующим backoff;
    конкурентность входящих вызова ограничена per-process семафором в _chat_sync.
    """
    # Cloud-эндпоинт (ollama.com/api/*) требует bearer-токен.
    if "ollama.com" in OLLAMA_HOST and not OLLAMA_API_KEY:
        return "Ollama API ключ не настроен. Добавьте OLLAMA_API_KEY в .env", 0, True

    attempts = max(1, int(os.getenv("OLLAMA_MAX_RETRIES", "3")) + 1)
    result: Tuple[str, int, str] | None = None
    for attempt in range(1, attempts + 1):
        try:
            result = await asyncio.to_thread(
                _chat_sync, model_id, msg, temperature, num_predict,
            )
            break
        except Exception as exc:  # ollama.ResponseError / httpx исключения / прочее
            if _is_rate_limited(exc) and attempt < attempts:
                delay = _retry_delay(attempt)
                logger.warning(
                    "Ollama 429 для %s (попытка %d/%d), повтор через %.1fs",
                    model_id, attempt, attempts, delay,
                )
                await _sleep(delay)
                continue
            return _handle_ollama_error(exc, model_id)

    content, tokens, thinking = result

    if not content.strip():
        # Reasoning-модели (gpt-oss:cloud) иногда кладут
        # весь ответ в поле ``thinking``, оставляя content пустым — отдаём его
        # пользователю вместо ошибки «пустой ответ». Если это рассуждения без
        # кода, они вырезаются think-фильтром в arm_runner (не попадают в код).
        if thinking.strip():
            logger.warning(
                "Ollama model %s returned empty content, falling back to thinking (%d chars)",
                model_id, len(thinking),
            )
            return thinking, int(tokens), False
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