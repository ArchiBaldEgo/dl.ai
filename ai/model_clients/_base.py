"""Общие хелперы для клиентов AI-моделей.

Содержит:
- ``make_table_handlers(table, handler_factory, ...)`` — DRY-фабрика, которая
  по декларативной таблице моделей генерирует async-обработчики, выставляя им
  ``__name__``/``__qualname__``/``__doc__`` (замена 4 локальных копий
  ``_make_handler``/``_make_groq_handler`` в sambanova/openrouter/ollama/groq);
- ``bearer_headers(token, extra=None)`` — стандартный dict заголовков
  ``Authorization: Bearer ...`` + ``Content-Type: application/json``;
- ``proxy_bypass_session()`` — ``requests.Session`` с ``trust_env=False``
  (для внутренних HTTP-вызовов в обход HTTP_PROXY/HTTPS_PROXY).
"""

from typing import Callable, Coroutine, Dict, Optional

import requests

Handler = Callable[..., Coroutine]


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