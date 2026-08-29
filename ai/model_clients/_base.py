"""Общие хелперы для клиентов AI-моделей.

Содержит:
- ``make_table_handlers(table, handler_factory, ...)`` — DRY-фабрика, которая
  по декларативной таблице моделей генерирует async-обработчики, выставляя им
  ``__name__``/``__qualname__``/``__doc__`` (замена 4 локальных копий
  ``_make_handler``/``_make_groq_handler`` в sambanova/openrouter/ollama/groq);
- ``bearer_headers(token, extra=None)`` — стандартный dict заголовков
  ``Authorization: Bearer ...`` + ``Content-Type: application/json``;
- ``proxy_bypass_session()`` — ``requests.Session`` с ``trust_env=False``
  (для внутренних HTTP-вызовов в обход HTTP_PROXY/HTTPS_PROXY);
- ``BotPoolClient`` — общий HTTP-клиент Puppeteer-бот-пула (WebDeepseek/,
  WebKimi/): POST /api/send с ретраями и POST /api/restart; веб-модули
  задают только конфигурацию (DRY вместо двух копий логики).
"""

import asyncio
import logging
from typing import Callable, Coroutine, Dict, Optional, Tuple

import requests

from .exceptions import map_http_error, safe_parse_response

Handler = Callable[..., Coroutine]

logger = logging.getLogger(__name__)


def bearer_headers(token: str, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Вернуть стандартные заголовки для bearer-token API-запроса.

    Базовый набор: ``Authorization: Bearer <token>`` + ``Content-Type: application/json``.
    ``extra`` (опционально) дополняет/переопределяет ключи.
    """
    headers: Dict[str, str] = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    if extra:
        headers.update(extra)
    return headers


def proxy_bypass_session() -> requests.Session:
    """Вернуть ``requests.Session`` с ``trust_env=False``.

    Используется для service-вызовов (бот-пул Web DeepSeek и т.п.), которые
    не должны идти через env-прокси (HTTP_PROXY/HTTPS_PROXY).
    """
    session = requests.Session()
    session.trust_env = False
    return session


def make_table_handlers(
    table: Dict[str, object],
    handler_factory: Callable[[str, object], Handler],
    *,
    name_template: str = "ask_{key}_async",
    doc_fn: Optional[Callable[[str, object], str]] = None,
) -> Dict[str, Handler]:
    """Сгенерировать async-обработчики для каждой записи таблицы моделей.

    Для каждой пары ``(model_key, cfg)`` из ``table`` вызывает
    ``handler_factory(model_key, cfg)`` — она возвращает async-функцию
    ``(messages, user_id) -> ...``, делегирующую в generic-обработчик провайдера.
    Затем выставляет ``__name__``/``__qualname__``/``__doc__`` и регистрирует
    обработчик в возвращаемом dict под именем ``name_template.format(key=...)``.

    Args:
        table: dict ``{model_key: cfg}`` (форма cfg зависит от провайдера).
        handler_factory: ``(model_key, cfg) -> async callable``; возвращённая
            функция делегирует в ``ask_fn`` провайдера с нужными аргументами.
        name_template: шаблон имени функции (``{key}`` → ключ таблицы);
            по умолчанию ``"ask_{key}_async"`` (совпадает со старыми именами).
        doc_fn: опциональная ``(model_key, cfg) -> str`` для ``__doc__``;
            если None — ``cfg.get("description")`` (для dict-cfg) или
            ``f"{model_key} → {cfg.get('model', model_key)}"``.

    Returns:
        dict ``{handler_name: handler}`` для экспорта в модуль.
    """
    handlers: Dict[str, Handler] = {}
    for model_key, cfg in table.items():
        handler = handler_factory(model_key, cfg)
        name = name_template.format(key=model_key)
        handler.__name__ = name
        handler.__qualname__ = name
        if doc_fn is not None:
            handler.__doc__ = doc_fn(model_key, cfg)
        elif isinstance(cfg, dict):
            handler.__doc__ = cfg.get("description") or f"{model_key} → {cfg.get('model', model_key)}"
        else:
            handler.__doc__ = f"{model_key}"
        handlers[name] = handler
    return handlers


class BotPoolClient:
    """HTTP-клиент Puppeteer-бот-пула (WebDeepseek/, WebKimi/).

    Общий протокол у пулов одинаковый: ``POST /api/send`` (с ретраями,
    Retry-After backoff и распознаванием устаревших селекторов) и
    ``POST /api/restart`` (автоподъём воркеров для health-check).
    Модули-провайдеры (``web_deepseek.py``, ``web_kimi.py``) задают только
    конфигурацию — вся логика живёт здесь.

    Args:
        base_url: URL пула (BOT_POOL_URL / KIMI_POOL_URL, loopback).
        model: строка модели в payload пула ("deepseek" / "kimi").
        label: человекочитаемое имя для сообщений об ошибках ("Web DeepSeek").
        provider: ключ для ``map_http_error`` ("web_deepseek" / "web_kimi").
        site_label: имя сайта для сообщения об изменённой вёрстке
            ("DeepSeek" / "Kimi").
        selector_doc: путь к файлу селекторов в этом сообщении
            ("WebDeepseek/worker/data.json" / "WebKimi/worker/data.json").
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        label: str,
        provider: str,
        site_label: str,
        selector_doc: str,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.label = label
        self.provider = provider
        self.site_label = site_label
        self.selector_doc = selector_doc

    def _retry_after_seconds(self, response) -> int:
        """Retry-After header от bot-пула (429/503 — боты заняты/стартуют), 0 если нет."""
        try:
            val = int((response.headers.get("Retry-After") or "").strip())
            if val > 0:
                return val
        except (TypeError, ValueError):
            pass
        return 0

    def _post_to_bot_pool(self, payload: dict, timeout_seconds: int = 120) -> requests.Response:
        """Internal service call must bypass env proxies (HTTP_PROXY/HTTPS_PROXY)."""
        session = proxy_bypass_session()
        try:
            return session.post(
                f"{self.base_url}/api/send",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=timeout_seconds,
            )
        finally:
            session.close()

    def restart(self, timeout_seconds: int = 30) -> bool:
        """Ask the bot pool to restart its workers (автоподъём).

        Returns True if the pool acknowledged the restart, False on network/HTTP
        failure. Never raises — callers (the health check) treat a failed restart
        as "still down" and log it.
        """
        session = proxy_bypass_session()
        try:
            response = session.post(
                f"{self.base_url}/api/restart",
                json={},
                headers={"Content-Type": "application/json"},
                timeout=timeout_seconds,
            )
            if response.status_code < 300:
                logger.info("%s bot pool restart acknowledged: %s", self.label, response.text[:200])
                return True
            logger.warning("%s bot pool restart returned HTTP %s", self.label, response.status_code)
            return False
        except Exception as exc:
            logger.warning("%s bot pool restart failed: %s", self.label, exc)
            return False
        finally:
            session.close()

    async def ask(self, msg: str, user_id: int, thinking: bool) -> Tuple[str, int, bool]:
        """Отправить сообщение в бот-пул и вернуть (text, tokens, is_error).

        До 3 попыток: таймаут/обрыв соединения и 429/5xx — с backoff
        (уважаем Retry-After); устаревшие селекторы сайта — сразу понятная
        ошибка без ретраев.
        """
        payload = {
            "model": self.model,
            "user_id": user_id,
            "thinking": thinking,
            "message": msg,
        }
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                response = await asyncio.to_thread(self._post_to_bot_pool, payload, 300)
            except requests.Timeout:
                if attempt < max_attempts:
                    wait = min(attempt * 5, 20)
                    logger.warning("%s bot pool timeout (300s), retrying in %ss (attempt %s/%s)", self.label, wait, attempt, max_attempts)
                    await asyncio.sleep(wait)
                    continue
                return f"Таймаут при подключении к {self.label} (300с). Попробуйте позже.", 0, True
            except requests.ConnectionError as exc:
                if attempt < max_attempts:
                    wait = min(attempt * 3, 15)
                    logger.warning("%s bot pool connection error: %s, retrying in %ss (attempt %s/%s)", self.label, exc, wait, attempt, max_attempts)
                    await asyncio.sleep(wait)
                    continue
                return f"Ошибка подключения к {self.label}: {exc}", 0, True

            logger.debug("%s bot pool response status: %s (attempt %s/%s)", self.label, response.status_code, attempt, max_attempts)

            if response.status_code == 200:
                obj, error_message = safe_parse_response(response.text)
                if obj is None:
                    return error_message, 0, True
                return obj["data"]["content"], 0, False

            if response.status_code in (429, 500, 502, 503, 504):
                # 429 — все боты pool заняты (pool шлёт Retry-After, ~3с): транзитная
                # загрузка, короткое ожидание часто даёт боту освободиться — ретраимся.
                # 5xx — серверная ошибка pool/бота. Особый случай: сайт изменил
                # вёрстку → селекторы устарели, retry бесполезен. Сразу вернём
                # понятную ошибку (экономит ~15 мин 3×300с retry-ей).
                reason = ""
                if response.status_code != 429:
                    try:
                        obj, _ = safe_parse_response(response.text)
                        if obj:
                            reason = obj.get("reason") or ""
                    except Exception:
                        pass
                    if "UI may have changed" in reason or "All answer XPath selectors failed" in reason:
                        return (f"{self.site_label} изменил интерфейс сайта — селекторы bot-пула устарели, "
                                f"нужно обновить {self.selector_doc}."), 0, True
                if attempt < max_attempts:
                    # Уважаем Retry-After (429/503 — бот освобождается/стартует),
                    # иначе мягкий backoff; ограничиваем сверху, чтобы не висеть.
                    wait = self._retry_after_seconds(response) or min(attempt * 3, 15)
                    wait = min(wait, 20)
                    logger.warning("%s bot pool returned %s, retrying in %ss (attempt %s/%s)", self.label, response.status_code, wait, attempt, max_attempts)
                    await asyncio.sleep(wait)
                    continue

            return map_http_error(response.status_code, self.provider), 0, True
