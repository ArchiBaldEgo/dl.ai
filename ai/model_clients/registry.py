"""Реестр AI-моделей, используемый WebSocket consumer и health-checker.

Сопоставляет внутренние ключи моделей (DeepSeek_V3_1, Web_DeepSeek, ...)
с их обработчиками (async-функциями), отображаемыми названиями и capabilities
(text/vision/reasoning). ModelRegistry — основной API для получения
обработчика модели по ключу.

Провайдеры SambaNova и Groq по умолчанию отключены и включаются через
настройки ``AI_ENABLE_SAMBANOVA`` / ``AI_ENABLE_GROQ`` (см. DjangoTest/settings).
"""

import re
from typing import Callable, Coroutine, Dict

from django.conf import settings

from . import openrouter, web_deepseek, web_kimi, ollama

Handler = Callable[..., Coroutine]

# Default capability set for a plain text-only, non-reasoning model.
_TEXT_ONLY = {"text": True, "vision": False, "reasoning": False}
# Reasoning / "thinking" models: text-only but advertised as reasoning-capable.
_REASONING = {"text": True, "vision": False, "reasoning": True}


_MODELS: Dict[str, Dict[str, object]] = {
    # --- Web DeepSeek (бот-пул через Puppeteer) — бесплатные, идут первыми ---
    "Web_DeepSeek": {
        "title": "Web DeepSeek",
        "handler": web_deepseek.ask_Web_DeepSeek_async,
        "capabilities": _TEXT_ONLY,
    },
    "Web_DeepSeek_Thinking": {
        "title": "Web DeepSeek Thinking",
        "handler": web_deepseek.ask_Web_DeepSeek_Thinking_async,
        "capabilities": _REASONING,
    },
    # --- Web Kimi (бот-пул через Puppeteer, kimi.moonshot.cn) ---
    "Web_Kimi": {
        "title": "Web Kimi K2.7",
        "handler": web_kimi.ask_Web_Kimi_async,
        "capabilities": _TEXT_ONLY,
    },
    # --- OpenRouter (бесплатные модели) ---
    "OR_Nemotron_Ultra_550B": {
        "title": "OR Nemotron Ultra 550B",
        "handler": openrouter.ask_OR_Nemotron_Ultra_550B_async,
        "capabilities": _TEXT_ONLY,
    },
    "OR_Ling_3_Flash": {
        "title": "OR Ling 3.0 Flash",
        "handler": openrouter.ask_OR_Ling_3_Flash_async,
        "capabilities": _TEXT_ONLY,
    },
    "OR_Gemma_4_31B": {
        "title": "OR Gemma 4 31B",
        "handler": openrouter.ask_OR_Gemma_4_31B_async,
        "capabilities": _TEXT_ONLY,
    },
    "OR_Gemma_4_26B": {
        "title": "OR Gemma 4 26B",
        "handler": openrouter.ask_OR_Gemma_4_26B_async,
        "capabilities": _TEXT_ONLY,
    },
    "OR_Nemotron_Super_120B": {
        "title": "OR Nemotron Super 120B",
        "handler": openrouter.ask_OR_Nemotron_Super_120B_async,
        "capabilities": _TEXT_ONLY,
    },
    "OR_North_Mini_Code": {
        "title": "OR North Mini Code",
        "handler": openrouter.ask_OR_North_Mini_Code_async,
        "capabilities": _TEXT_ONLY,
    },
    "OR_Nemotron_Nano_30B_Reasoning": {
        "title": "OR Nemotron Nano 30B Reasoning",
        "handler": openrouter.ask_OR_Nemotron_Nano_30B_Reasoning_async,
        "capabilities": _REASONING,
    },
    "OR_Nemotron_Nano_30B": {
        "title": "OR Nemotron Nano 30B",
        "handler": openrouter.ask_OR_Nemotron_Nano_30B_async,
        "capabilities": _TEXT_ONLY,
    },
    "OR_GPT_OSS_20B": {
        "title": "OR GPT-OSS 20B",
        "handler": openrouter.ask_OR_GPT_OSS_20B_async,
        "capabilities": _TEXT_ONLY,
    },
    "OR_Nemotron_Nano_12B_VL": {
        "title": "OR Nemotron Nano 12B VL",
        "handler": openrouter.ask_OR_Nemotron_Nano_12B_VL_async,
        "capabilities": {"text": True, "vision": True, "reasoning": False},
    },
    "OR_Nemotron_Nano_9B": {
        "title": "OR Nemotron Nano 9B",
        "handler": openrouter.ask_OR_Nemotron_Nano_9B_async,
        "capabilities": _TEXT_ONLY,
    },
    "OR_Free_Router": {
        "title": "OR Free Router",
        "handler": openrouter.ask_OR_Free_Router_async,
        "capabilities": _TEXT_ONLY,
    },
    # --- Ollama (cloud-модели вида <name>:cloud, обычный чат без инструментов) ---
    "Ollama_Glm_5_2_Cloud": {
        "title": "Ollama GLM 5.2",
        "handler": ollama.ask_Ollama_Glm_5_2_Cloud_async,
        "capabilities": _TEXT_ONLY,
    },
    "Ollama_Glm_5_3_Flash_Cloud": {
        "title": "Ollama GLM 5.3 Flash",
        "handler": ollama.ask_Ollama_Glm_5_3_Flash_Cloud_async,
        # glm-5.3-flash:cloud стримит отдельное поле ``thinking`` (не think-теги
        # в content) — помечаем как reasoning, content остаётся чистым.
        "capabilities": _REASONING,
    },
    "Ollama_Gemma_4_Cloud": {
        "title": "Ollama Gemma 4",
        "handler": ollama.ask_Ollama_Gemma_4_Cloud_async,
        "capabilities": _TEXT_ONLY,
    },
    "Ollama_Qwen_3_5_Cloud": {
        "title": "Ollama Qwen 3.5",
        "handler": ollama.ask_Ollama_Qwen_3_5_Cloud_async,
        "capabilities": _TEXT_ONLY,
    },
    "Ollama_Nemotron_3_Super_Cloud": {
        "title": "Ollama Nemotron 3 Super",
        "handler": ollama.ask_Ollama_Nemotron_3_Super_Cloud_async,
        "capabilities": _TEXT_ONLY,
    },
    "Ollama_Kimi_K2_7_Code_Cloud": {
        "title": "Ollama Kimi K2.7 Code",
        "handler": ollama.ask_Ollama_Kimi_K2_7_Code_Cloud_async,
        "capabilities": _TEXT_ONLY,
    },
    "Ollama_Kimi_K2_6_Cloud": {
        "title": "Ollama Kimi K2.6",
        "handler": ollama.ask_Ollama_Kimi_K2_6_Cloud_async,
        "capabilities": _TEXT_ONLY,
    },
    "Ollama_Gpt_Oss_20B_Cloud": {
        "title": "Ollama GPT-OSS 20B",
        "handler": ollama.ask_Ollama_Gpt_Oss_20B_Cloud_async,
        # gpt-oss:cloud (harmony) стримит отдельное поле ``thinking`` (как
        # glm-5.3-flash:cloud) — помечаем как reasoning, content остаётся чистым.
        "capabilities": _REASONING,
    },
    "Ollama_Gpt_Oss_120B_Cloud": {
        "title": "Ollama GPT-OSS 120B",
        "handler": ollama.ask_Ollama_Gpt_Oss_120B_Cloud_async,
        # gpt-oss:cloud (harmony) стримит отдельное поле ``thinking`` (как
        # glm-5.3-flash:cloud) — помечаем как reasoning, content остаётся чистым.
        "capabilities": _REASONING,
    },
}


def _groq_models() -> Dict[str, Dict[str, object]]:
    """Записи реестра для Groq-провайдера (включается через AI_ENABLE_GROQ)."""
    from . import groq

    return {
        "Groq_Llama_3_3_70B": {
            "title": "Groq Llama 3.3 70B",
            "handler": groq.ask_Groq_Llama_3_3_70B_async,
            "capabilities": _TEXT_ONLY,
        },
        "Groq_Llama_3_1_8B": {
            "title": "Groq Llama 3.1 8B",
            "handler": groq.ask_Groq_Llama_3_1_8B_async,
            "capabilities": _TEXT_ONLY,
        },
        "Groq_Gpt_Oss_120B": {
            "title": "Groq GPT-OSS 120B",
            "handler": groq.ask_Groq_Gpt_Oss_120B_async,
            "capabilities": _TEXT_ONLY,
        },
        "Groq_Gpt_Oss_20B": {
            "title": "Groq GPT-OSS 20B",
            "handler": groq.ask_Groq_Gpt_Oss_20B_async,
            "capabilities": _TEXT_ONLY,
        },
        "Groq_Qwen_3_6_27B": {
            "title": "Groq Qwen 3.6 27B",
            "handler": groq.ask_Groq_Qwen_3_6_27B_async,
            "capabilities": _TEXT_ONLY,
        },
    }


def _sambanova_models() -> Dict[str, Dict[str, object]]:
    """Записи реестра для SambaNova-провайдера (включается через AI_ENABLE_SAMBANOVA)."""
    from . import sambanova

    return {
        "DeepSeek_V3_1": {
            "title": "Samba DeepSeek-V3.1",
            "handler": sambanova.ask_DeepSeek_V3_1_async,
            "capabilities": _TEXT_ONLY,
        },
        "DeepSeek_V3_2": {
            "title": "Samba DeepSeek-V3.2",
            "handler": sambanova.ask_DeepSeek_V3_2_async,
            "capabilities": _TEXT_ONLY,
        },
        "Meta_Llama_3_3_70B_Instruct": {
            "title": "Samba Meta-Llama-3.3-70B-Instruct",
            "handler": sambanova.ask_Meta_Llama_3_3_70B_Instruct_async,
            "capabilities": _TEXT_ONLY,
        },
        "MiniMax_M2_7": {
            "title": "Samba MiniMax-M2.7",
            "handler": sambanova.ask_MiniMax_M2_7_async,
            "capabilities": _TEXT_ONLY,
        },
        "Gemma_3_12b_it": {
            "title": "Samba gemma-4-31B-it",
            "handler": sambanova.ask_Gemma_3_12b_it_async,
            "capabilities": _TEXT_ONLY,
        },
        "Gpt_oss_120b": {
            "title": "Samba gpt-oss-120b",
            "handler": sambanova.ask_Gpt_oss_120b_async,
            "capabilities": _TEXT_ONLY,
        },
        "DeepSeek_R1_Distill_Llama_70B": {
            "title": "Samba DeepSeek-R1-Distill-Llama-70B",
            "handler": sambanova.ask_DeepSeek_R1_Distill_Llama_70B_async,
            "capabilities": _REASONING,
        },
        "DeepSeek_V3_1_cb": {
            "title": "Samba DeepSeek-V3.1 (cloud buffer)",
            "handler": sambanova.ask_DeepSeek_V3_1_cb_async,
            "capabilities": _TEXT_ONLY,
        },
        "Llama_4_Maverick_17B_128E_Instruct": {
            "title": "Samba Llama-4-Maverick-17B-128E-Instruct",
            "handler": sambanova.ask_Llama_4_Maverick_17B_128E_Instruct_async,
            "capabilities": _TEXT_ONLY,
        },
        "MiniMax_M2_5": {
            "title": "Samba MiniMax-M2.5",
            "handler": sambanova.ask_MiniMax_M2_5_async,
            "capabilities": _TEXT_ONLY,
        },
    }


# Условная регистрация отключённых по умолчанию провайдеров.
if getattr(settings, "AI_ENABLE_GROQ", False):
    _MODELS.update(_groq_models())

if getattr(settings, "AI_ENABLE_SAMBANOVA", False):
    _MODELS.update(_sambanova_models())

_DEFAULT_CAPABILITIES = _TEXT_ONLY


class ModelRegistry:
    """Реестр AI-моделей: сопоставляет ключ → {title, handler, capabilities}."""

    def __init__(self, models: Dict[str, Dict[str, object]]):
        self._models = dict(models)

    def keys(self):
        return self._models.keys()

    def items(self):
        return self._models.items()

    def title_to_key(self) -> dict:
        """Вернуть обратный map ``{title: key}`` для всех зарегистрированных моделей.

        Используется вместо прямого доступа к ``_models`` там, где нужен
        обратный lookup по отображаемому названию (см. ai/views.py).
        """
        return {self.title(key): key for key in self._models}

    def get(self, key: str):
        return self._models.get(key)

    def handler(self, key: str) -> Handler | None:
        info = self._models.get(key)
        if info is None:
            return None
        return info.get("handler")  # type: ignore[return-value]

    def title(self, key: str) -> str:
        info = self._models.get(key)
        if info is None:
            return key
        return str(info.get("title") or key)

    def capabilities(self, key: str) -> dict:
        """Return the capability dict (text/vision/reasoning) for a model.

        Always returns a dict with the three boolean keys; unknown models get
        the conservative text-only default.
        """
        info = self._models.get(key)
        caps = info.get("capabilities") if info else None
        if not isinstance(caps, dict):
            return dict(_DEFAULT_CAPABILITIES)
        return {
            "text": bool(caps.get("text", True)),
            "vision": bool(caps.get("vision", False)),
            "reasoning": bool(caps.get("reasoning", False)),
        }

    def register(self, key: str, title: str, handler: Handler, capabilities: dict | None = None) -> None:
        self._models[key] = {
            "title": title,
            "handler": handler,
            "capabilities": capabilities or dict(_DEFAULT_CAPABILITIES),
        }


registry = ModelRegistry(_MODELS)


# ---------------------------------------------------------------------------
# Сокращённые названия моделей (для XLSX-сводки /arm/solve/).
# ---------------------------------------------------------------------------

# Ручные сокращения — точные примеры пользователя; имеют приоритет над
# автоматическим алгоритмом.
SHORT_MODEL_TITLE_OVERRIDES = {
    "Ollama GLM 5.2": "OG5.2",
    "Ollama GPT-OSS 120B": "GPTO120",
}

# Первые токены-провайдеры в названиях моделей → короткий префикс.
_PROVIDER_ABBRS = {
    "ollama": "O",
    "web": "W",
    "or": "OR",
    "openrouter": "OR",
    "groq": "GQ",
    "sambanova": "SN",
}

_TITLE_TOKEN_RE = re.compile(r"[A-Za-z]+|\d+(?:\.\d+)?")


def short_model_title(title: str) -> str:
    """Сокращённое название модели: «Ollama GLM 5.2» → «OG5.2».

    Детерминированный алгоритм: токен-провайдер → аббревиатура
    (``_PROVIDER_ABBRS``), остальные буквенные токены → первая буква,
    числовые токены (включая дробные «5.2») → целиком. Точные совпадения из
    ``SHORT_MODEL_TITLE_OVERRIDES`` имеют приоритет. Читаемость гарантирует
    лист-расшифровка XLSX, так что алгоритм намеренно агрессивно короткий.
    """
    variants = _abbr_variants(str(title or "").strip())
    return variants[0] if variants else ""


def short_model_titles(titles) -> dict:
    """``{title: abbr}`` с разрешением коллизий внутри одного набора.

    При столкновении берём следующий вариант сокращения (буквенные токены
    раскрываются по одной букве: «OG5.2» → «OGL5.2» → …), в крайнем случае
    добавляя числовой суффикс. Возвращает одну мапу для обоих листов XLSX,
    чтобы сокращения совпадали.
    """
    titles = [str(t or "").strip() for t in titles if str(t or "").strip()]
    # Дубликаты названий встречаются (одна модель на много задач): второе
    # вхождение перетёрло бы мапу следующим вариантом сокращения — дедуплим.
    seen_titles = []
    for t in titles:
        if t not in seen_titles:
            seen_titles.append(t)
    abbrs = {}
    used = set()
    for title in seen_titles:
        variants = _abbr_variants(title)
        abbr = next((v for v in variants if v not in used), None)
        if abbr is None:
            base = variants[0]
            suffix = 2
            while f"{base}{suffix}" in used:
                suffix += 1
            abbr = f"{base}{suffix}"
        used.add(abbr)
        abbrs[title] = abbr
    return abbrs


def _abbr_variants(title: str) -> list:
    """Все варианты сокращения по возрастанию «раскрытости».

    Уровень 0 — базовый (первая буква каждого токена), далее буквенные
    токены раскрываются на 2, 3, … букв. Ручные override-ы дают ровно один
    вариант — коллизия с override разрешается числовым суффиксом.
    """
    if not title:
        return [""]
    override = SHORT_MODEL_TITLE_OVERRIDES.get(title)
    if override:
        return [override]
    tokens = _TITLE_TOKEN_RE.findall(title)
    if not tokens:
        return [title[:6]]
    letter_tokens = [t for t in tokens if not t[0].isdigit()]
    max_take = max((len(t) for t in letter_tokens), default=1)
    variants = []
    for take in range(1, max_take + 1):
        parts = []
        provider_consumed = False
        for token in tokens:
            if token[0].isdigit():
                parts.append(token)
            elif not provider_consumed and token.lower() in _PROVIDER_ABBRS:
                parts.append(_PROVIDER_ABBRS[token.lower()])
                provider_consumed = True
            else:
                parts.append(token[:take].upper())
        variants.append("".join(parts))
    return variants or [title[:6]]