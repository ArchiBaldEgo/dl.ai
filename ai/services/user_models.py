"""Личные AI-модели пользователя: учёт, безопасность, вызов.

Владелец: пользователь приносит СВОЮ модель (провайдер + ID модели + свой
ключ). Модель доступна ТОЛЬКО ему: чужие её не видят (селектор), не могут
вызвать (проверка владельца) и не могут отредактировать.

БЕЗОПАСНОСТЬ (владелец отдельно потребовал «защиту от атак»):

* ``base_url`` — единственная точка риска (SSRF). Проверяется:
  - только ``https`` (http — отказ; локальный endpoint без TLS не нужен
    пользователю, а без TLS ключ улетел бы открытым текстом);
  - хост — не IP-адрес литералом и не приватный/локальный/линковый адрес
    (10/8, 172.16/12, 192.168/16, 127/8, 169.254/16, 0.0.0.0/8, 100.64/10,
    ::1, fc00::/7, fe80::/10);
  - DNS-резолв host'а не попадает в приватные диапазоны (проверяются ВСЕ
    A/AAAA-записи — иначе «example.com → 127.0.0.1» обходит фильтр);
  - никаких credentials в URL (``user:pass@host``), никаких нестандартных
    схем;
  - порт — из белого списка (443, 8443) либо отсутствует.
* Ключ API хранится шифром Fernet (ключ — ``AI_USER_MODEL_SECRET_KEY`` в
  .env). В открытом виде ключ не показывается никогда, в лог не пишется.
* Вызов идёт с ``trust_env=False`` (мимо HTTP_PROXY из окружения) и без
  следования редиректам (редирект на внутренний адрес — классический SSRF).
* Ответ модели экранируется на клиенте как и любой HTML модели (CLAUDE.md).
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urlparse

import httpx

from ..constants import MOSCOW_TZ  # noqa: F401  (единый источник TZ проекта)

logger = logging.getLogger(__name__)

# Белый список портов для пользовательского endpoint'а.
ALLOWED_PORTS = (443, 8443)

# Максимальная длина URL и ID модели (защита от абсурдных значений).
MAX_BASE_URL_LEN = 500
MAX_MODEL_ID_LEN = 255

# Ошибки валидации — с человеческими сообщениями (страница показывает их
# пользователю, поэтому текст на русском).
ERR_NOT_HTTPS = "Адрес модели должен быть по https (например https://api.example.com/v1)."
ERR_BAD_URL = "Адрес модели указан неверно."
ERR_CREDENTIALS_IN_URL = "Адрес модели не должен содержать логин/пароль."
ERR_PRIVATE_HOST = "Адрес модели не может указывать на локальный или внутренний адрес."
ERR_BAD_PORT = "Адрес модели должен использовать порт 443 (или 8443)."
ERR_BAD_HOST = "Не удалось разрешить адрес модели (проверьте домен)."

# Провайдеры с известным публичным endpoint'ом: base_url можно не указывать.
PROVIDER_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "ollama": "https://ollama.com",
    "groq": "https://api.groq.com/openai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "sambanova": "https://api.sambanova.ai/v1",
}


def _is_private_ip(ip: ipaddress._BaseAddress) -> bool:
    """Приватный/локальный/линковый адрес — такой endpoint'у запрещён."""
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
        return True
    if ip.is_multicast or ip.is_unspecified:
        return True
    # Carrier-grade NAT (100.64/10) — тоже не публичный интернет.
    if ip.version == 4 and ip in ipaddress.ip_network("100.64.0.0/10"):
        return True
    return False


def _resolved_ips(host: str) -> list:
    """A/AAAA-записи host'а (пустой список — если разрешить не удалось)."""
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError, OSError):
        return []
    ips = []
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        try:
            ips.append(ipaddress.ip_address(sockaddr[0]))
        except ValueError:
            continue
    return ips


def validate_base_url(raw_url: str) -> tuple[str, str]:
    """Проверить пользовательский endpoint. Возвращает ``(url, error)``.

    ``error`` — пустая строка, если адрес корректен; иначе — текст причины
    (показывается пользователю). Проверка SSRF выполняется ЗДЕСЬ, при
    сохранении: адрес из БД считается уже проверенным (но при вызове мы всё
    равно не следуем редиректам и не пускаем env-прокси).
    """
    url = (raw_url or "").strip()
    if not url:
        return "", ERR_BAD_URL
    if len(url) > MAX_BASE_URL_LEN:
        return "", ERR_BAD_URL
    if url.lower().startswith("http://"):
        return "", ERR_NOT_HTTPS

    try:
        parsed = urlparse(url)
    except ValueError:
        return "", ERR_BAD_URL
    if parsed.scheme != "https":
        return "", ERR_NOT_HTTPS
    if parsed.username or parsed.password:
        return "", ERR_CREDENTIALS_IN_URL
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return "", ERR_BAD_URL
    # Хост-литерал IP: пропускаем только публичные адреса.
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return "", ERR_PRIVATE_HOST

    try:
        port = parsed.port
    except ValueError:
        return "", ERR_BAD_URL
    if port is not None and port not in ALLOWED_PORTS:
        return "", ERR_BAD_PORT

    # DNS-резолв: ВСЕ адреса должны быть публичными (иначе — SSRF через
    # «свой» домен, у которого A-запись смотрит во внутреннюю сеть).
    ips = _resolved_ips(host)
    if not ips:
        return "", ERR_BAD_HOST
    if any(_is_private_ip(ip) for ip in ips):
        return "", ERR_PRIVATE_HOST

    return url, ""


def default_base_url(provider: str) -> str:
    """Публичный endpoint провайдера (пустая строка — неизвестный)."""
    return PROVIDER_BASE_URLS.get(provider, "")


def _secret_key() -> bytes:
    """Ключ шифрования из env. Пустой ключ = секреты не шифруются (лок)."""
    import os

    raw = (os.getenv("AI_USER_MODEL_SECRET_KEY") or "").strip()
    if not raw:
        return b""
    return raw.encode("utf-8")


def encrypt_secret(plain: str) -> str:
    """Зашифровать ключ API (Fernet). Пустой ключ → пустая строка."""
    text = (plain or "").strip()
    if not text:
        return ""
    key = _secret_key()
    if not key:
        # Ключа шифрования в окружении нет: НЕ храним ключ открытым —
        # возвращаем пустую строку и просим задать AI_USER_MODEL_SECRET_KEY.
        logger.warning("AI_USER_MODEL_SECRET_KEY не задан — ключ модели не сохранён")
        return ""
    from cryptography.fernet import Fernet

    token = Fernet(key).encrypt(text.encode("utf-8")).decode("ascii")
    return token


def decrypt_secret(cipher: str) -> str:
    """Расшифровать ключ API. Ошибка расшифровки → пустая строка."""
    text = (cipher or "").strip()
    if not text:
        return ""
    key = _secret_key()
    if not key:
        return ""
    from cryptography.fernet import Fernet, InvalidToken

    try:
        return Fernet(key).decrypt(text.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError):
        logger.warning("Не удалось расшифровать ключ личной модели")
        return ""


def user_models_for(user) -> list:
    """Активные личные модели пользователя (для селектора моделей)."""
    from ..models import AIUserModel

    if not user or not getattr(user, "is_authenticated", False):
        return []
    # Объект пользователя может быть «не-джанговским» (тесты, сервисные
    # обёртки) — тогда это не экземпляр модели User, и фильтр по user= упал бы
    # с TypeError. Таких пользователей просто считаем «без личных моделей».
    if not hasattr(user, "_meta") or not isinstance(getattr(user, "pk", None), int):
        return []
    return list(
        AIUserModel.objects.filter(user=user, is_active=True).order_by("title")
    )


def user_model_options(user) -> list:
    """Опции личных моделей для селектора (JSON для фронта)."""
    return [
        {
            "key": f"User_{model.pk}",
            "title": model.title,
            "model_id": model.model_id,
            "provider": model.provider,
            "supports_vision": bool(model.supports_vision),
            "supports_think": bool(model.supports_think),
        }
        for model in user_models_for(user)
    ]


def find_user_model(user, model_key: str):
    """Найти ЛИЧНУЮ модель пользователя по ключу ``User_<pk>``.

    Владелец проверяется ЗДЕСЯ: чужой ключ → None (вызов не состоится).
    """
    from ..models import AIUserModel

    if not user or not getattr(user, "is_authenticated", False):
        return None
    text = str(model_key or "").strip()
    if not text.startswith("User_"):
        return None
    raw_id = text[5:]
    if not raw_id.isdigit():
        return None
    return (
        AIUserModel.objects.filter(pk=int(raw_id), user=user, is_active=True).first()
    )


# === CRUD личных моделей (единственный владелец операций) ====================
#
# Логика живёт ЗДЕСЬ, а не во вьюхах: страница доступна и из админки
# (/ai/admin/user-models/ — для персонала) и с клиентской стороны
# (/ai/my-models/ — для обычного пользователя), поэтому вьюхи должны
# звать общий сервис, а не дублировать правила (DRY, CLAUDE.md).

#: Максимальная длина названия модели (подпись в селекторе).
MAX_TITLE_LEN = 255

#: Потолок генерации по умолчанию и границы (защита от абсурдных значений).
DEFAULT_MAX_TOKENS = 8000
MIN_MAX_TOKENS = 256
MAX_MAX_TOKENS = 200000


def clean_user_text(value, limit):
    """Обрезать и убрать управляющие символы из пользовательского ввода."""
    text = (value or "").strip()
    text = "".join(ch for ch in text if ch == "\n" or ch == "\t" or ord(ch) >= 32)
    return text[:limit]


def user_model_rows(user) -> list:
    """Строки таблицы «Мои модели» (только модели ВЛАДЕЛЬЦА)."""
    from ..models import AIUserModel

    if not user or not getattr(user, "is_authenticated", False):
        return []
    rows = []
    for model in AIUserModel.objects.filter(user=user).order_by("title"):
        rows.append({
            "id": model.pk,
            "title": model.title,
            "provider": model.provider,
            "provider_label": model.get_provider_display(),
            "model_id": model.model_id,
            "base_url": model.base_url or default_base_url(model.provider),
            "max_tokens": model.max_tokens,
            "supports_vision": model.supports_vision,
            "supports_think": model.supports_think,
            "is_active": model.is_active,
            "has_key": bool(model.api_key_encrypted),
            "created_at": model.created_at,
            "updated_at": model.updated_at,
        })
    return rows


def save_user_model(user, payload: dict) -> tuple:
    """Создать/обновить личную модель ВЛАДЕЛЬЦА.

    Возвращает ``(ok, message, model_pk)``. Валидация — здесь же: чужую
    запись по ``id`` не обновить (её для этого пользователя не существует).

    ``payload`` — словарь с полями: id, title, provider, model_id,
    base_url, api_key, max_tokens, supports_vision, supports_think,
    is_active.
    """
    from ..models import AIUserModel

    if not user or not getattr(user, "is_authenticated", False):
        return False, "Access denied", None
    payload = payload or {}

    title = clean_user_text(payload.get("title"), MAX_TITLE_LEN)
    if not title:
        return False, "Название модели обязательно", None

    provider = clean_user_text(payload.get("provider"), 32) or AIUserModel.PROVIDER_OPENAI
    if provider not in dict(AIUserModel.PROVIDER_CHOICES):
        return False, "Неизвестный провайдер", None

    model_id = clean_user_text(payload.get("model_id"), MAX_MODEL_ID_LEN)
    if not model_id:
        return False, "ID модели у провайдера обязателен", None

    base_url = clean_user_text(payload.get("base_url"), MAX_BASE_URL_LEN)
    if base_url:
        checked_url, error = validate_base_url(base_url)
        if error:
            return False, error, None
        base_url = checked_url
    else:
        base_url = default_base_url(provider)

    api_key = (payload.get("api_key") or "").strip()
    encrypted = ""
    if api_key:
        encrypted = encrypt_secret(api_key)
        if not encrypted:
            return False, (
                "Сервер не настроен на хранение ключей: задайте "
                "AI_USER_MODEL_SECRET_KEY в .env и перезапустите контейнер."
            ), None

    try:
        max_tokens = int(payload.get("max_tokens") or DEFAULT_MAX_TOKENS)
    except (TypeError, ValueError):
        max_tokens = DEFAULT_MAX_TOKENS
    max_tokens = max(MIN_MAX_TOKENS, min(max_tokens, MAX_MAX_TOKENS))

    model_pk = payload.get("id")
    if model_pk:
        try:
            model_pk = int(model_pk)
        except (TypeError, ValueError):
            return False, "Некорректный идентификатор", None
        # Чужая запись для этого пользователя НЕ существует → «не найдена»
        # (существование чужих записей не подтверждаем).
        instance = AIUserModel.objects.filter(pk=model_pk, user=user).first()
        if instance is None:
            return False, "Модель не найдена", None
    else:
        instance = AIUserModel(user=user)

    instance.title = title
    instance.provider = provider
    instance.model_id = model_id
    instance.base_url = base_url
    if encrypted:
        instance.api_key_encrypted = encrypted
    instance.max_tokens = max_tokens
    instance.supports_vision = bool(payload.get("supports_vision"))
    instance.supports_think = bool(payload.get("supports_think"))
    instance.is_active = bool(payload.get("is_active", True))
    instance.save()
    return True, "", instance.pk


def delete_user_model(user, model_pk) -> tuple:
    """Удалить личную модель (только свою). ``(ok, message)``."""
    from ..models import AIUserModel

    if not user or not getattr(user, "is_authenticated", False):
        return False, "Access denied"
    try:
        model_pk = int(model_pk or 0)
    except (TypeError, ValueError):
        return False, "Некорректный идентификатор"
    deleted, _ = AIUserModel.objects.filter(pk=model_pk, user=user).delete()
    if not deleted:
        return False, "Модель не найдена"
    return True, ""


async def call_user_model(model, message: str, client_id: str = "") -> tuple:
    """Вызов ЛИЧНОЙ модели (OpenAI-совместимый chat/completions).

    Возвращает кортеж провайдера: ``(content, completion_tokens, is_error,
    cut_flag, prompt_tokens)`` — тот же контракт, что у глобальных моделей
    (ai/model_clients), чтобы ModelCaller работал с ними одинаково.

    Anthropic — исключение: у него свой формат сообщений; здесь поддержан
    OpenAI-совместимый режим (что покрывает Ollama, Groq, OpenRouter,
    SambaNova, LM Studio, vLLM, Together и пр.). Для Anthropic-провайдера
    base_url должен быть указан явно совместимым эндпоинтом.
    """
    api_key = decrypt_secret(model.api_key_encrypted)
    base_url = (model.base_url or default_base_url(model.provider) or "").rstrip("/")
    if not base_url:
        return "Не задан адрес провайдера модели.", 0, True, False, 0

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model.model_id,
        "messages": [{"role": "user", "content": message}],
        "max_tokens": int(model.max_tokens or 8000),
        "stream": False,
    }
    url = f"{base_url}/chat/completions"
    try:
        async with httpx.AsyncClient(
            timeout=120.0,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            response = await client.post(url, json=payload, headers=headers)
    except httpx.TimeoutException:
        return "Таймаут при подключении к модели. Попробуйте позже.", 0, True, False, 0
    except Exception as exc:  # noqa: BLE001
        logger.warning("Личная модель %s: ошибка вызова: %s", model.pk, exc)
        return f"Ошибка подключения к модели: {exc}", 0, True, False, 0

    if response.status_code != 200:
        return (
            f"Модель вернула ошибку {response.status_code}.",
            0,
            True,
            False,
            0,
        )
    try:
        data = response.json()
    except ValueError:
        return "Модель вернула ответ в неожиданном формате.", 0, True, False, 0
    choices = data.get("choices") or []
    if not choices:
        return "Модель вернула пустой ответ.", 0, True, False, 0
    choice = choices[0]
    content = (choice.get("message") or {}).get("content", "") or ""
    usage = data.get("usage") or {}
    completion_tokens = int(usage.get("completion_tokens", 0) or 0)
    prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
    if not content.strip():
        return "Модель вернула пустой ответ.", 0, True, False, 0
    return (
        content,
        completion_tokens,
        False,
        choice.get("finish_reason") == "length",
        prompt_tokens,
    )
