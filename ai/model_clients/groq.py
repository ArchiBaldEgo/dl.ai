"""Groq API клиент — бесплатные модели через OpenAI-совместимый endpoint.

Groq предоставляет бесплатный доступ к моделям Llama, Qwen, GPT-OSS
через LPU (Language Processing Unit) — ~2600 токенов/сек.

Base URL: https://api.groq.com/openai/v1
Без кредитки, без телефона. Лимиты: 30 RPM, 14,400 RPD для большинства моделей.

Rate-limit заголовки Groq (обновляются в каждом ответе):
  x-ratelimit-remaining-tokens  — осталось токенов в окне
  x-ratelimit-limit-tokens      — лимит токенов на окно
  x-ratelimit-remaining-requests — осталось запросов
  x-ratelimit-limit-requests    — лимит запросов на окно
  x-ratelimit-reset-tokens      — когда сбросятся токены (ISO 8601)
  x-ratelimit-reset-requests    — когда сбросятся запросы (ISO 8601)

Окно сброса: 1 минута (free tier).

Архитектура: generic _ask_groq() + декларативная таблица GROQ_MODELS.
Внешние функции-обёртки генерируются автоматически.
"""

import logging
import time
from typing import Tuple

import httpx

from ._base import bearer_headers, make_table_handlers
from .config import GROQ_TOKEN, proxies
from .exceptions import map_http_error

logger = logging.getLogger(__name__)

GROQ_BASE_URL = "https://api.groq.com/openai/v1"

# --- Декларативная таблица моделей ---
# Ключ → (Groq API model name, max_tokens)
# max_tokens подобран так, чтобы prompt + max_tokens не превышал TPM-лимит модели.
#   llama-3.1-8b-instant: TPM=6000 (free tier), поэтому max_tokens=2048
GROQ_MODELS: dict[str, tuple[str, int]] = {
    "Groq_Llama_3_3_70B": ("llama-3.3-70b-versatile", 4096),
    "Groq_Llama_3_1_8B": ("llama-3.1-8b-instant", 2048),
    "Groq_Gpt_Oss_120B": ("openai/gpt-oss-120b", 4096),
    "Groq_Gpt_Oss_20B": ("openai/gpt-oss-20b", 4096),
    "Groq_Qwen_3_6_27B": ("qwen/qwen3.6-27b", 4096),
}

# In-memory кэш rate-limit заголовков по каждой модели.
_rate_limit_cache: dict[str, dict] = {}


def get_rate_limits() -> dict[str, dict]:
    """Возвращает текущие rate-limit данные по всем Groq моделям."""
    return dict(_rate_limit_cache)


def _update_rate_limit_cache(model_key: str, headers: httpx.Headers) -> None:
    """Сохраняет rate-limit заголовки из ответа Groq в кэш."""
    _rate_limit_cache[model_key] = {
        "remaining_tokens": int(headers.get("x-ratelimit-remaining-tokens", 0) or 0),
        "limit_tokens": int(headers.get("x-ratelimit-limit-tokens", 0) or 0),
        "remaining_requests": int(headers.get("x-ratelimit-remaining-requests", 0) or 0),
        "limit_requests": int(headers.get("x-ratelimit-limit-requests", 0) or 0),
        "reset_tokens": headers.get("x-ratelimit-reset-tokens", ""),
        "reset_requests": headers.get("x-ratelimit-reset-requests", ""),
        "updated_at": time.time(),
    }


def _get_headers() -> dict:
    return bearer_headers(GROQ_TOKEN)


def _get_proxy():
    if proxies and proxies.get("http"):
        return proxies["http"]
    return None


async def _ask_groq(model_name: str, msg: str, user_id: int, model_key: str = "") -> Tuple[str, int]:
    """Общий обработчик для всех моделей Groq.

    Отправляет chat completion запрос через OpenAI-совместимый API Groq.
    """
    if not GROQ_TOKEN:
        return "Groq API токен не настроен. Добавьте GROQ_TOKEN в .env", 0

    # max_tokens берётся из GROQ_MODELS; по умолчанию 4096 для обратной совместимости
    max_tokens = 4096
    if model_key and model_key in GROQ_MODELS:
        max_tokens = GROQ_MODELS[model_key][1]

    payload = {
        "model": model_name,
        "messages": [{"role": "user", "content": msg}],
        "temperature": 0.7,
        "max_tokens": max_tokens,
    }

    try:
        proxy = _get_proxy()
        async with httpx.AsyncClient(
            proxy=proxy,
            timeout=120.0,
            trust_env=False,
        ) as client:
            response = await client.post(
                f"{GROQ_BASE_URL}/chat/completions",
                json=payload,
                headers=_get_headers(),
                timeout=120.0,
            )

        if model_key:
            _update_rate_limit_cache(model_key, response.headers)

        if response.status_code != 200:
            # Логируем только неспецифические статусы (413/429/401 в оригинале
            # возвращались без warning — сохраняем это поведение).
            if response.status_code not in (401, 413, 429):
                logger.warning("Groq API error: status=%s body=%s", response.status_code, response.text[:500])
            return map_http_error(response.status_code, "groq"), 0

        data = response.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        usage = data.get("usage", {})
        total_tokens = usage.get("total_tokens", 0)
        return content, total_tokens

    except httpx.TimeoutException:
        return "Таймаут при подключении к Groq API. Попробуйте позже.", 0
    except Exception as exc:
        logger.exception("Groq API request failed: %s", exc)
        return f"Ошибка Groq API: {exc}", 0


# --- Автогенерация функций для каждой модели (через _base.make_table_handlers) ---


def _groq_handler_factory(model_key: str, cfg: tuple):
    """Создаёт async-функцию-обработчик для Groq модели по ключу."""
    groq_model, _max_tokens = cfg

    async def handler(msg: str, user_id: int) -> str:
        response, _ = await _ask_groq(groq_model, msg, user_id, model_key)
        return response

    return handler


globals().update(
    make_table_handlers(
        GROQ_MODELS,
        _groq_handler_factory,
        doc_fn=lambda key, cfg: f"Groq {key} → {cfg[0]}",
    )
)


async def probe_rate_limits() -> dict[str, dict]:
    """Пробивает rate-limit заголовки для всех Groq моделей лёгким запросом."""
    if not GROQ_TOKEN:
        return {}

    results = {}
    proxy = _get_proxy()
    async with httpx.AsyncClient(
        proxy=proxy,
        timeout=30.0,
        trust_env=False,
    ) as client:
        for model_key, (groq_model, _max_tokens) in GROQ_MODELS.items():
            try:
                payload = {
                    "model": groq_model,
                    "messages": [{"role": "user", "content": "1"}],
                    "max_tokens": 1,
                }
                response = await client.post(
                    f"{GROQ_BASE_URL}/chat/completions",
                    json=payload,
                    headers=_get_headers(),
                    timeout=30.0,
                )
                _update_rate_limit_cache(model_key, response.headers)
                results[model_key] = _rate_limit_cache[model_key]
            except Exception as exc:
                logger.warning("Groq probe failed for %s: %s", model_key, exc)

    return results