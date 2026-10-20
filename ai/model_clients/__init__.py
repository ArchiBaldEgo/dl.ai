"""Реестр AI-моделей и публичные экспорты model_clients.

Пример использования:
    >>> from ai.model_clients import registry
    >>> handler = registry.get("DeepSeek_V3_1")
    >>> response = await handler("hello", user_id=42)
"""

from .registry import registry

__all__ = ["registry", "is_output_truncated", "get_no_think_handler", "supports_think"]

# Провайдеры с известным потолком генерации (токенов ответа): модуль обработчика
# → функция потолка. Определяем провайдера по __module__ handler'а из реестра
# (у SambaNova ключи без префикса — префиксный матчинг ненадёжен), с запасным
# матчингом по префиксам ключей для неактуальных/чужих реестров.
_PROVIDER_PREFIXES = {"ollama": ("Ollama_",), "sambanova": ()}


def _provider_module(handler) -> str:
    """Хвост __module__ handler'а ('ai.model_clients.ollama' → 'ollama')."""
    module = getattr(handler, "__module__", "") or ""
    return module.rsplit(".", 1)[-1]


def _output_ceiling(model_key: str) -> int:
    """Потолок генерации (токенов ответа) для ключа модели; 0 — неизвестен.

    ollama → num_predict (env OLLAMA_NUM_PREDICT);
    sambanova/openrouter → cfg["max_tokens"] из таблицы моделей
    (ключ = ключ реестра); groq → GROQ_MODELS[key][1]; остальные
    провайдеры → 0 (семантика tokens неоднородная — ложный «обрезан»
    хуже пропущенного).
    """
    key = model_key or ""
    provider = ""
    info = registry.get(key)
    handler = info.get("handler") if info else None
    if handler is not None:
        provider = _provider_module(handler)
    else:
        for cand, prefixes in _PROVIDER_PREFIXES.items():
            if key.startswith(prefixes):
                provider = cand
                break
    try:
        if provider == "ollama":
            from . import ollama as _ollama
            return _ollama._default_num_predict()
        if provider == "sambanova":
            from . import sambanova as _sambanova
            cfg = _sambanova.SAMBANOVA_MODELS.get(key)
            if isinstance(cfg, dict):
                return int(cfg.get("max_tokens") or 0)
        if provider == "openrouter":
            from . import openrouter as _openrouter
            cfg = _openrouter.OPENROUTER_FREE_MODELS.get(key)
            if isinstance(cfg, dict):
                return int(cfg.get("max_tokens") or 0)
        if provider == "groq":
            from . import groq as _groq
            cfg = _groq.GROQ_MODELS.get(key)
            if isinstance(cfg, (tuple, list)) and len(cfg) > 1:
                return int(cfg[1] or 0)
    except (TypeError, ValueError, ImportError, AttributeError):
        return 0
    return 0


def is_output_truncated(model_key: str, tokens: int) -> bool:
    """Правда ли, что ответ модели оборван потолком генерации провайдера.

    Сравнение по факту: tokens > 0 и tokens >= потолка (Ollama num_predict /
    SambaNova max_tokens). Стрим, остановленный ровно на лимите, — верный
    признак обреза посреди текста: код после рассуждений мог не успеть
    родиться. Для моделей без известного потолка — False.

    Используется ARM-раннером (arm_runner): флаг «обрезан» на паре +
    один авто-ретрай с усиленной припиской «сразу код, без рассуждений».
    """
    tokens = int(tokens or 0)
    if tokens <= 0:
        return False
    ceiling = _output_ceiling(model_key)
    return ceiling > 0 and tokens >= ceiling


def supports_think(handler) -> bool:
    """True — провайдер умеет переключать рассуждения полем ``think``
    (chat/generate Ollama, docs.ollama.com/capabilities/thinking).

    Проводная точка для клиентского переключателя: ModelCaller передаёт kwarg
    ``think=True/False`` в хендлер только при True (ollama-хендлеры принимают
    его keyword-аргументом), прочим провайдерам поле недоступно по API. Хвост
    ``__module__`` — как в _output_ceiling / get_no_think_handler.
    """
    return _provider_module(handler) == "ollama"


def get_no_think_handler(handler):
    """Хендлер РЕТРАЯ «без think» того же вызова — или None, если провайдер
    переключателя рассуждений не имеет (и ретрай пойдёт обычным хендлером).

    ARM-раннер вторично отправляет задачу, когда код из ответа не извлёкся:
    думающие модели подводят, заливая ответ протекшим reasoning'ом. По докам
    Ollama (docs.ollama.com/capabilities/thinking) это глушится полем
    ``think: false`` в запросе — «request no thinking output»; у прочих
    провайдеров (web-пулы, OpenRouter) API-переключателя нет, а запрет
    рассуждений уже сидит в базовом solve-сообщении (_NO_RAMBLE_INSTRUCTION).
    Провайдер определяется по хвосту ``__module__`` хендлера — как в
    _output_ceiling.
    """
    if _provider_module(handler) == "ollama":
        from . import ollama as _ollama
        return _ollama.nothink_handler(handler)
    return None