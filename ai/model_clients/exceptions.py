"""Общие хелперы обработки ошибок для клиентов AI-моделей.

Содержит функции для классификации сетевых ошибок, извлечения контента
из ответов API (SambaNova-style choices), и преобразования технических
ошибок API в понятные сообщения (humanize_model_error).

Локализация: ``humanize_model_error`` принимает ``language`` (имя из
#selectLang). ``map_http_error``/``classify_network_error`` и прочие
хелперы внутри клиентов моделей остаются русскими — язык интерфейса туда
не протекает (хендлеры имеют фиксированную сигнатуру handler(message,
client_id)), а их тексты до-humanize'ятся выше в consumer'е уже с языком
пользователя. ARM/админские вызовы получают русский по умолчанию.
"""

from typing import Tuple, Optional


def _t(table: dict, language: str) -> str:
    """Выбрать текст из таблицы ``{"ru":…, "en":…, "fr":…}`` по языку UI."""
    suffix = _ui_suffix(language)
    return table[suffix] if suffix in table else table["ru"]


def _ui_suffix(language: str) -> str:
    """Suffix 'ru'/'en'/'fr' без импорта ai.i18n (плоский модуль, без циклов)."""
    low = (language or "").lower()
    if low.startswith("en"):
        return "en"
    if low.startswith("fr"):
        return "fr"
    return "ru"


def classify_network_error(error: Exception) -> str:
    """Return a user-facing Russian error string for network-related issues."""
    error_str = str(error)
    if "NameResolutionError" in error_str or "Failed to resolve" in error_str:
        return "Отсутствует подключение к интернету."
    if "Max retries exceeded" in error_str:
        return "Отсутствует интернет-соединение."
    return "Ошибка подключения. Ваш контекст сохранен, попробуйте позже."


def is_network_error(error: Exception) -> bool:
    """Return True when the exception looks like a network/timeout problem."""
    error_str = str(error)
    error_type = type(error).__name__
    network_phrases = (
        "NameResolutionError",
        "Failed to resolve",
        "Max retries exceeded",
        "HTTPSConnectionPool",
        "Name or service not known",
        "ConnectionError",
        "timeout",
        "Timeout",
    )
    return any(phrase in error_str for phrase in network_phrases) or "ConnectionError" in error_type


def is_missing_choices_error(error: Exception) -> bool:
    """Return True when the response appears to lack the expected choices key."""
    error_type = type(error).__name__
    error_str = str(error)
    return error_type == "KeyError" and ("'choices'" in error_str or "choices" in error_str)


def safe_parse_response(response_text: str) -> Tuple[Optional[dict], str]:
    """Try to parse JSON response; return (obj, error_message)."""
    import json

    if not response_text:
        return None, "Пустой ответ от сервера."
    try:
        return json.loads(response_text), ""
    except json.JSONDecodeError as e:
        return None, f"Что-то пошло не так с обработкой JSON: {e}"


def extract_choice_content(obj: dict) -> str:
    """Extract assistant content from a SambaNova-style completion response.

    Returns ``""`` when no content/reasoning_content/reasoning is present —
    НЕ подставляет плейсхолдер. Раньше возвращала ``"Пустой ответ от модели."``,
    но эта непустая строка обходила нижестоящие empty-гарды (ModelCaller/
    consumer) и доходила до пользователя как «успешный» ответ. Теперь пустой
    ответ детектится вызывающим обработчиком и возвращается как is_error=True.
    """
    choices = obj.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message", {})
    content = message.get("content")
    if not content:
        content = message.get("reasoning_content") or ""
    if not content:
        content = message.get("reasoning") or ""
    return content or ""


def extract_api_error_text(response_text: str) -> str:
    """Return a short Russian error based on common HTTP/API failure patterns."""
    low = response_text.lower()
    if "rate limit" in low or "превышен лимит" in low:
        return "Превышен лимит запросов. Попробуйте позже."
    if response_text.startswith("5") or response_text.startswith("http 5"):
        return "Ошибка сервера API. Попробуйте позже."
    if response_text == "402":
        return f"Ошибка API (код {response_text}): закончились кредиты у провайдера модели. Пополните баланс, чтобы продолжить."
    return f"Ошибка API (код {response_text})."


# --- HTTP status → friendly Russian message (per-provider) ---
#
# Консолидирует почти-дублирующиеся if-лестницы из openrouter/groq/web_deepseek.
# Сообщения, совпадающие у провайдеров, — общие (413); отличающиеся —
# индивидуальны по ``provider``. ``map_http_error`` возвращает None только для
# status == 200 (успех); для любого не-200 всегда есть friendly-fallback.
# Тексты остаются русскими: клиенты моделей не знают язык интерфейса, а в
# чатовой цепочке текст до-humanize'ится с языком пользователя (см. модульный
# докстринг).

# 413 — общий для OpenRouter и Groq (одинаковая формулировка в обоих клиентах).
_TOO_MANY_TOKENS = "Превышен лимит токенов для модели. Попробуйте сократить запрос или выберите другую модель."


def map_http_error(status: int, provider: str) -> Optional[str]:
    """Вернуть понятное русское сообщение об ошибке для HTTP-статуса.

    Args:
        status: HTTP status code ответа.
        provider: один из ``"openrouter"``, ``"groq"``, ``"web_deepseek"``,
            ``"web_kimi"``, ``"ollama"`` (определяет формулировки для 401/429/5xx и
            generic-fallback).

    Returns:
        Friendly-сообщение; для ``status == 200`` — None (вызов успешен).
        Для прочих не-200 — всегда непустая строка (включая generic-fallback
        ``"Ошибка <provider> API (код N)."``).
    """
    if status == 200:
        return None

    if status == 400 and provider in ("web_deepseek", "web_kimi"):
        return "Неправильный запрос"

    if status == 401:
        if provider in ("web_deepseek", "web_kimi"):
            return "Бот не авторизован. Проверьте логин/пароль"
        if provider == "openrouter":
            return "OpenRouter API ключ недействителен. Проверьте OPENROUTER_API_KEY."
        if provider == "groq":
            return "Groq API ключ недействителен. Проверьте GROQ_TOKEN."

    if status == 413:
        # Общее сообщение для OpenRouter/Groq; web-пулы не используют 413
        # и попадут в generic-fallback ниже.
        if provider in ("openrouter", "groq"):
            return _TOO_MANY_TOKENS

    if status == 429:
        if provider in ("web_deepseek", "web_kimi"):
            return "Все боты заняты"
        if provider == "openrouter":
            return "Превышен лимит запросов OpenRouter (free tier). Попробуйте позже."
        if provider == "groq":
            return "Превышен лимит запросов Groq. Попробуйте позже."
        if provider == "ollama":
            return ("Ollama Cloud перегружен: слишком много одновременных запросов "
                    "на тарифе. Попробуйте ещё раз через пару минут.")

    if status >= 503 and provider in ("web_deepseek", "web_kimi"):
        return "Бот инициализируется слишком долго. Попробуйте позже."

    # generic-fallback (формат совпадает с прежними per-провайдер сообщениями).
    if provider == "openrouter":
        return f"Ошибка OpenRouter API (код {status})."
    if provider == "groq":
        return f"Ошибка Groq API (код {status})."
    if provider == "web_deepseek":
        return f"Ошибка сервиса Web DeepSeek (код {status})."
    if provider == "web_kimi":
        return f"Ошибка сервиса Web Kimi (код {status})."
    return f"Ошибка API (код {status})."


# --- Паттерны технического текста → понятное сообщение (три языка) ---
#
# (substrings, slug); сами тексты — в _MODEL_ERROR_TR. Словарь-хелпер
# держит ru/en/fr версии одной формулировки рядом (одна строка правки
# меняет сразу три языка — правило триязычной клиентской части).

_MODEL_ERROR_TR = {
    "bad_request": {
        "ru": "Ошибка запроса к модели (400). Обычно это значит, что запрос слишком длинный, "
              "содержит неподдерживаемый формат или лишние спецсимволы. "
              "Попробуйте сократить условие/код и отправить снова.",
        "en": "Model request error (400). Usually this means the request is too long, "
              "contains an unsupported format or extra special characters. "
              "Try to shorten the task/code and send it again.",
        "fr": "Erreur de requête au modèle (400). Cela signifie généralement que la requête est "
              "trop longue, contient un format non pris en charge ou des caractères spéciaux "
              "superflus. Essayez de raccourcir l'énoncé/le code et de renvoyer.",
    },
    "unauthorized": {
        "ru": "Ошибка авторизации модели (401). Проверьте API-ключ/токен и права доступа к модели.",
        "en": "Model authorization error (401). Check the API key/token and access rights to the model.",
        "fr": "Erreur d'autorisation du modèle (401). Vérifiez la clé API/le jeton et les droits d'accès au modèle.",
    },
    "credits": {
        "ru": "Закончились кредиты у провайдера модели (402). Пополните баланс, чтобы продолжить.",
        "en": "The model provider's credits are exhausted (402). Top up the balance to continue.",
        "fr": "Les crédits du fournisseur du modèle sont épuisés (402). Rechargez le solde pour continuer.",
    },
    "forbidden": {
        "ru": "Доступ к модели запрещен (403). У текущего ключа нет нужных прав или доступ ограничен политикой сервиса.",
        "en": "Access to the model is forbidden (403). The current key lacks the required rights or access is restricted by the service policy.",
        "fr": "L'accès au modèle est interdit (403). La clé actuelle n'a pas les droits requis ou l'accès est restreint par la politique du service.",
    },
    "not_found": {
        "ru": "Модель не найдена (404). Возможно, имя модели устарело или эта модель сейчас недоступна у провайдера.",
        "en": "Model not found (404). The model name may be outdated or the model is currently unavailable at the provider.",
        "fr": "Modèle introuvable (404). Le nom du modèle est peut-être obsolète ou ce modèle est actuellement indisponible chez le fournisseur.",
    },
    "rate_limit": {
        "ru": "Сервис модели ограничил частоту запросов (429). Подождите немного и запустите проверку снова.",
        "en": "The model service has limited the request rate (429). Wait a moment and run the check again.",
        "fr": "Le service du modèle a limité la fréquence des requêtes (429). Patientez un peu et relancez la vérification.",
    },
    "timeout": {
        "ru": "Модель не ответила вовремя (таймаут). Попробуйте повторить запрос позже или сократить объем задачи/кода.",
        "en": "The model did not respond in time (timeout). Try repeating the request later or reducing the size of the task/code.",
        "fr": "Le modèle n'a pas répondu à temps (délai dépassé). Réessayez plus tard ou réduisez le volume de la tâche/du code.",
    },
    "unavailable_5xx": {
        "ru": "Сервис модели временно недоступен (5xx). Это серверная ошибка провайдера, попробуйте позже.",
        "en": "The model service is temporarily unavailable (5xx). This is a provider-side error, try again later.",
        "fr": "Le service du modèle est temporairement indisponible (5xx). C'est une erreur côté fournisseur, réessayez plus tard.",
    },
    "connection": {
        "ru": "Ошибка подключения к сервису модели. Проверьте сеть/прокси и доступность внешнего API.",
        "en": "Connection error to the model service. Check the network/proxy and the external API availability.",
        "fr": "Erreur de connexion au service du modèle. Vérifiez le réseau/le proxy et la disponibilité de l'API externe.",
    },
    "empty_raw": {
        "ru": "Модель вернула пустой ответ. Попробуйте позже.",
        "en": "The model returned an empty response. Please try again later.",
        "fr": "Le modèle a renvoyé une réponse vide. Réessayez plus tard.",
    },
    "api_generic": {
        "ru": "Сервис модели вернул ошибку API. Проверьте параметры запроса и повторите попытку чуть позже.",
        "en": "The model service returned an API error. Check the request parameters and try again a bit later.",
        "fr": "Le service du modèle a renvoyé une erreur API. Vérifiez les paramètres de la requête et réessayez un peu plus tard.",
    },
}
_DETAIL_LABEL = {
    "ru": "Техническая деталь",
    "en": "Technical detail",
    "fr": "Détail technique",
}

_MODEL_ERROR_PATTERNS = (
    # (sequence of substrings, slug в _MODEL_ERROR_TR)
    (
        ("неправильный запрос", "код 400", "error 400", "status 400", "bad request"),
        "bad_request",
    ),
    (
        ("код 401", "error 401", "status 401", "unauthorized", "не авториз"),
        "unauthorized",
    ),
    (
        ("код 402", "error 402", "status 402", "credits_exhausted", "закончились кредиты", "payment required"),
        "credits",
    ),
    (
        ("код 403", "error 403", "status 403", "forbidden", "доступ запрещ"),
        "forbidden",
    ),
    (
        ("код 404", "error 404", "status 404", "not found", "не найден"),
        "not_found",
    ),
    (
        ("код 429", "error 429", "status 429", "rate limit", "превышен лимит", "все боты заняты"),
        "rate_limit",
    ),
    (
        ("таймаут", "timeout", "timed out", "код 408", "status 408"),
        "timeout",
    ),
    (
        (
            "код 500", "error 500", "status 500",
            "код 502", "error 502", "status 502",
            "код 503", "error 503", "status 503",
            "код 504", "error 504", "status 504",
            "bad gateway", "gateway", "server error", "ошибка сервера",
            "временно недоступ", "инициализируется слишком долго",
        ),
        "unavailable_5xx",
    ),
    (
        (
            "отсутствует подключение к интернету", "отсутствует интернет",
            "connectionerror", "failed to resolve", "name resolution",
            "max retries exceeded", "httpsconnectionpool", "не удалось подключ",
        ),
        "connection",
    ),
)


def humanize_model_error(raw_text: str, include_detail: bool = False, language: str = "") -> tuple[str, str]:
    """Return a user-friendly error message and, optionally, a detailed variant.

    The detailed message keeps the original technical text after the friendly
    explanation.  When ``include_detail`` is False both returned values are the
    same friendly string.  ``language`` — имя UI-языка («Русский»/«English»/
    «Français», см. #selectLang); пустое значение = русский (ARM/админские
    вызовы и старые потребители).
    """
    text = (raw_text or "").strip()
    if not text:
        empty = _t(_MODEL_ERROR_TR["empty_raw"], language)
        return empty, empty

    low = text.lower()

    # Long natural-language responses from models should not be interpreted as transport errors.
    if len(low) > 350 and not low.startswith(("ошибка", "error", "exception", "traceback")):
        return text, text

    for markers, slug in _MODEL_ERROR_PATTERNS:
        if any(marker in low for marker in markers):
            friendly_text = _t(_MODEL_ERROR_TR[slug], language)
            if not include_detail:
                return friendly_text, friendly_text
            detailed_text = friendly_text
            if text and text != friendly_text:
                detailed_text += f"\n\n{_t(_DETAIL_LABEL, language)}: {text}"
            return friendly_text, detailed_text

    if low.startswith("ошибка api") or "api (код" in low:
        friendly_text = _t(_MODEL_ERROR_TR["api_generic"], language)
        if not include_detail:
            return friendly_text, friendly_text
        detailed_text = f"{friendly_text}\n\n{_t(_DETAIL_LABEL, language)}: {text}"
        return friendly_text, detailed_text

    return text, text