"""Личные AI-модели пользователя: учёт, безопасность, вызов.

Владелец: пользователь приносит СВОЮ модель (провайдер + ID модели + свой
ключ). По умолчанию модель доступна ТОЛЬКО ему: чужие её не видят
(селектор), не могут вызвать (проверка доступа) и не могут отредактировать.
Владелец может дать доступ адресно (``shared_with``) или всем
(``is_public``) — только на пользовательских страницах, не в ARM.

БЕЗОПАСНОСТЬ (владелец отдельно потребовал «защиту от атак»):

* ``base_url`` — единственная точка риска (SSRF). Проверяется:
  - только ``https`` (http — отказ; локальный endpoint без TLS не нужен
    пользователю, а без TLS ключ улетел бы открытым текстом);
  - хост — не IP-адрес литералом и не приватный/локальный/линковый адрес
    (10/8, 172.16/12, 192.168/16, 127/8, 169.254/16, 0.0.0.0/8, 100.64/10,
    ::1, fc00::/7, fe80::/10);
  - DNS-резолв host'а не попадает в приватные диапазоны (проверяются ВСЕ
    A/AAAA-записи — иначе «example.com → 127.0.0.1» обходит фильтр);
  - если системный DNS недоступен (корпоративная сеть: выход в интернет
    только через прокси, прямой резолв внешних имён не работает), адрес
    резолвится через DNS-over-HTTPS по IP-литеральным эндпоинтам ЧЕРЕЗ тот
    же прокси — SSRF-проверка не отключается из-за особенностей сети;
  - никаких credentials в URL (``user:pass@host``), никаких нестандартных
    схем;
  - порт — из белого списка (443, 8443) либо отсутствует.
* Ключ API хранится шифром Fernet (ключ — ``AI_USER_MODEL_SECRET_KEY`` в
  .env). В открытом виде ключ не показывается никогда, в лог не пишется.
* Вызов идёт через тот же ``PROXY``, что и остальные провайдеры (в проде
  прямой выход закрыт — без прокси модель недостижима), с
  ``trust_env=False`` (явная конфигурация, а не случайные env-переменные) и
  без следования редиректам (редирект на внутренний адрес — классический
  SSRF).
* Ответ модели экранируется на клиенте как и любой HTML модели (CLAUDE.md).
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urlparse

import httpx
from django.db.models import Q

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
# Прокси настроен (прод), но и прямой DNS, и DoH через прокси не ответили:
# это сбой сети/прокси, а не ошибка в домене — не сбиваем пользователя с толку.
ERR_DNS_UNAVAILABLE = (
    "Не удалось проверить домен модели: сервер не достучался до DNS. "
    "Попробуйте позже."
)

# DoH-эндпоинты по IP-литералам: их резолвить не нужно, а запрос идёт через
# тот же прокси, что и вызов модели. Нужны там, где системный DNS внешние
# имена не разрешает (корпоративная сеть: выход только через прокси).
# Формат ответа — JSON DNS API Cloudflare/Google.
DOH_ENDPOINTS = (
    "https://1.1.1.1/dns-query",
    "https://8.8.8.8/resolve",
)
# Короткий таймаут: это интерактивное сохранение формы, а не фоновая задача.
DOH_TIMEOUT = 3.0

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


def _egress_proxy() -> str:
    """Прокси для внешних вызовов (тот же, что у остальных провайдеров).

    ``ai/model_clients/config.py`` — единственный владелец конфигурации
    внешних AI API (CLAUDE.md), поэтому берём прокси оттуда, а не читаем
    env заново. Импорт ленивый: пакет ``model_clients`` тянет реестр
    моделей, а сервис личных моделей не должен от него зависеть при загрузке.
    """
    try:
        from ..model_clients.config import proxies
    except Exception:  # noqa: BLE001 — конфиг недоступен → прямой выход
        return ""
    return ((proxies or {}).get("https") or (proxies or {}).get("http") or "").strip()


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


def _resolved_ips_via_doh(host: str, proxy: str) -> list:
    """A/AAAA host'а через DNS-over-HTTPS (JSON DNS API) с выходом в прокси.

    Эндпоинты — IP-литералы (резолв не нужен). Любая ошибка конкретного
    резолвера — пробуем следующий; ни один не ответил → пустой список.
    """
    ips = []
    for endpoint in DOH_ENDPOINTS:
        for record_type in ("A", "AAAA"):
            try:
                response = httpx.get(
                    endpoint,
                    params={"name": host, "type": record_type},
                    headers={"accept": "application/dns-json"},
                    proxy=proxy or None,
                    timeout=DOH_TIMEOUT,
                    trust_env=False,
                )
                if response.status_code != 200:
                    break
                data = response.json()
            except Exception:  # noqa: BLE001 — резолвер недоступен
                break
            for answer in data.get("Answer") or []:
                # 1 = A, 28 = AAAA; CNAME (5) пропускаем.
                if int(answer.get("type") or 0) not in (1, 28):
                    continue
                try:
                    ips.append(ipaddress.ip_address(answer.get("data") or ""))
                except ValueError:
                    continue
        if ips:
            return ips
    return ips


def validate_base_url(raw_url: str) -> tuple[str, str]:
    """Проверить пользовательский endpoint. Возвращает ``(url, error)``.

    ``error`` — пустая строка, если адрес корректен; иначе — текст причины
    (показывается пользователю). Проверка SSRF выполняется ЗДЕСЬ, при
    сохранении: адрес из БД считается уже проверенным (но при вызове мы всё
    равно не следуем редиректам, а прокси берём из конфига явно, не из env).
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
    proxy = ""
    if not ips:
        # Прямой DNS внешние имена не разрешает (типовая корпоративная сеть:
        # выход только через прокси). Резолвим тем же путём, которым пойдёт
        # вызов, — DoH через прокси; без прокси фоллбэка нет (прямой вызов
        # всё равно был бы недостижим, а проверка SSRF — важнее).
        proxy = _egress_proxy()
        if proxy:
            ips = _resolved_ips_via_doh(host, proxy)
    if not ips:
        return "", (ERR_DNS_UNAVAILABLE if proxy else ERR_BAD_HOST)
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


def _option(model, access: str) -> dict:
    """Опция модели для селектора (JSON для фронта)."""
    return {
        "key": f"User_{model.pk}",
        "title": model.title,
        "model_id": model.model_id,
        "provider": model.provider,
        "supports_vision": bool(model.supports_vision),
        "supports_think": bool(model.supports_think),
        "access": access,
    }


def _owner_suffix_title(model) -> str:
    """«Название — владелец»: одинаковые названия у разных людей различимы."""
    from .user_directory import display_name_for_user

    owner = display_name_for_user(model.user)
    return f"{model.title} — {owner}" if owner else model.title


def user_model_options(user) -> list:
    """Опции личных моделей для селектора (JSON для фронта)."""
    return [_option(model, "owner") for model in user_models_for(user)]


def shared_user_models_for(user) -> list:
    """Активные модели, доступ к которым выдан этому пользователю лично."""
    from ..models import AIUserModel

    if not user or not getattr(user, "is_authenticated", False):
        return []
    if not hasattr(user, "_meta") or not isinstance(getattr(user, "pk", None), int):
        return []
    return list(
        AIUserModel.objects.filter(
            shared_with=user, is_active=True, user__is_active=True,
        )
        .exclude(user=user)
        .select_related("user", "user__external_dl_account")
        .order_by("title")
        .distinct()
    )


def public_user_models_for(user) -> list:
    """Публичные модели ДРУГИХ пользователей (не свои, не уже выданные)."""
    from ..models import AIUserModel

    if not user or not getattr(user, "is_authenticated", False):
        return []
    if not hasattr(user, "_meta") or not isinstance(getattr(user, "pk", None), int):
        return []
    return list(
        AIUserModel.objects.filter(
            is_public=True, is_active=True, user__is_active=True,
        )
        .exclude(user=user)
        .exclude(shared_with=user)
        .select_related("user", "user__external_dl_account")
        .order_by("title")
        .distinct()
    )


def shared_user_model_options(user) -> list:
    """Опции «Доступные мне» (грант владельца)."""
    return [
        {**_option(model, "shared"), "title": _owner_suffix_title(model)}
        for model in shared_user_models_for(user)
    ]


def public_user_model_options(user) -> list:
    """Опции «Публичные» (владелец открыл модель всем)."""
    return [
        {**_option(model, "public"), "title": _owner_suffix_title(model)}
        for model in public_user_models_for(user)
    ]


def find_user_model(user, model_key: str):
    """Найти ЛИЧНУЮ модель по ключу ``User_<pk>`` с учётом доступа.

    Доступна владельцу, получателю гранта и любому пользователю, если
    владелец сделал модель публичной. Приватная чужая модель → None
    (существование записи не подтверждаем). Выключенная модель или
    выключенный владелец — тоже None.
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
        AIUserModel.objects.filter(pk=int(raw_id), is_active=True)
        .filter(
            Q(user=user)
            | Q(user__is_active=True, is_public=True)
            | Q(user__is_active=True, shared_with=user)
        )
        .distinct()
        .first()
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

#: Потолок числа получателей доступа на одну модель: список доступа — не
#: рассылка на весь поток, а адресная выдача нескольким людям.
MAX_SHARED_USERS = 100


def clean_user_text(value, limit):
    """Обрезать и убрать управляющие символы из пользовательского ввода."""
    text = (value or "").strip()
    text = "".join(ch for ch in text if ch == "\n" or ch == "\t" or ord(ch) >= 32)
    return text[:limit]


def _grant_rows(model) -> list:
    """Кому выдан доступ: ``[{id, display, dl_id}]`` (без email/прав)."""
    from .user_directory import user_brief

    return [user_brief(grantee) for grantee in model.shared_with.all()]


def user_model_rows(user) -> list:
    """Строки таблицы «Мои модели» (только модели ВЛАДЕЛЬЦА)."""
    from ..models import AIUserModel

    if not user or not getattr(user, "is_authenticated", False):
        return []
    rows = []
    models = (
        AIUserModel.objects.filter(user=user)
        .prefetch_related("shared_with", "shared_with__external_dl_account")
        .order_by("title")
    )
    for model in models:
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
            "is_public": model.is_public,
            "grants": _grant_rows(model),
            "has_key": bool(model.api_key_encrypted),
            "created_at": model.created_at,
            "updated_at": model.updated_at,
        })
    return rows


def admin_user_model_rows() -> list:
    """Все личные модели для суперюзерской таблицы «Все модели».

    Только идентичность и доступ: НИКАКИХ base_url/api_key/model_id/
    max_tokens/supports_* — суперюзер управляет доступом, но не читает
    чужие настройки и ключи.
    """
    from ..models import AIUserModel
    from .user_directory import display_name_for_user, dl_id_for_user

    rows = []
    models = (
        AIUserModel.objects.select_related("user", "user__external_dl_account")
        .prefetch_related("shared_with", "shared_with__external_dl_account")
        .order_by("title")
    )
    for model in models:
        rows.append({
            "id": model.pk,
            "title": model.title,
            "owner_display": display_name_for_user(model.user),
            "owner_dl_id": dl_id_for_user(model.user),
            "is_public": model.is_public,
            "is_active": model.is_active,
            "grants": _grant_rows(model),
        })
    return rows


def _coerce_bool(value, default: bool = False) -> bool:
    """Булево из JSON: строка ``"false"`` не должна включать публичность."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "on", "yes", "да")
    return bool(value)


def _resolve_share_users(owner, raw_ids) -> tuple:
    """Нормализовать список id получателей: ``(users, error)``.

    Не-int значения молча отбрасываются, дубли схлопываются, неактивные и
    сам владелец исключаются, список обрезается до ``MAX_SHARED_USERS``.
    """
    from django.contrib.auth import get_user_model

    if raw_ids is None:
        return None, ""
    if not isinstance(raw_ids, (list, tuple)):
        return None, "Некорректный список получателей доступа"
    ids = []
    for value in raw_ids:
        try:
            pk = int(value)
        except (TypeError, ValueError):
            continue
        if pk > 0 and pk not in ids:
            ids.append(pk)
    ids = ids[:MAX_SHARED_USERS]
    if not ids:
        return [], ""
    users = list(
        get_user_model().objects.filter(pk__in=ids, is_active=True)
        .exclude(pk=getattr(owner, "pk", None))
    )
    return users, ""


def save_user_model(user, payload: dict) -> tuple:
    """Создать/обновить личную модель ВЛАДЕЛЬЦА.

    Возвращает ``(ok, message, model_pk)``. Валидация — здесь же: чужую
    запись по ``id`` не обновить (её для этого пользователя не существует).

    ``payload`` — словарь с полями: id, title, provider, model_id,
    base_url, api_key, max_tokens, supports_vision, supports_think,
    is_active, is_public, shared_with (список id получателей). Поля доступа
    необязательны: если их НЕ передали, текущий доступ не трогаем (старые
    клиенты и тесты продолжают работать).
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

    # Доступ валидируется ДО save(): ошибка не должна оставлять
    # полусозданную запись. ``None`` = поле не передано, значение не меняем.
    wants_public = payload.get("is_public")
    share_users, share_error = _resolve_share_users(user, payload.get("shared_with"))
    if share_error:
        return False, share_error, None

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
    if wants_public is not None:
        instance.is_public = _coerce_bool(wants_public)
    instance.save()
    if share_users is not None:
        instance.shared_with.set(share_users)
    return True, "", instance.pk


def set_user_model_access(actor, model_pk, is_public, shared_with_ids) -> tuple:
    """Изменить ТОЛЬКО доступ к модели: публичность и список получателей.

    Разрешено владельцу и реальному суперюзеру (режим «Посмотреть как…» —
    нет). Конфигурация модели (название, провайдер, URL, ключ, настройки)
    этой операцией не меняется НИКОГДА: суперюзер управляет доступом, но не
    читает и не правит чужие ключи.

    Возвращает ``(ok, message)``; чужая приватная модель для не-суперюзера
    — «Модель не найдена» (существование не подтверждаем).
    """
    from ..models import AIUserModel

    if not actor or not getattr(actor, "is_authenticated", False):
        return False, "Access denied"
    try:
        model_pk = int(model_pk or 0)
    except (TypeError, ValueError):
        return False, "Некорректный идентификатор"
    if model_pk <= 0:
        return False, "Некорректный идентификатор"

    instance = AIUserModel.objects.filter(pk=model_pk).first()
    if instance is None:
        return False, "Модель не найдена"

    # Ленивый импорт: сервисы не должны тянуть admin на загрузке модуля.
    from ..admin.permissions import is_superuser_user

    is_owner = instance.user_id == getattr(actor, "pk", None)
    if not is_owner and not is_superuser_user(actor):
        return False, "Модель не найдена"

    share_users, share_error = _resolve_share_users(instance.user, shared_with_ids)
    if share_error:
        return False, share_error

    # ``None`` = поле не передано → не трогаем соответствующую настройку
    # (запрос «только публичность» не должен молча снимать все гранты).
    if is_public is not None:
        instance.is_public = _coerce_bool(is_public)
        instance.save(update_fields=["is_public", "updated_at"])
    if share_users is not None:
        instance.shared_with.set(share_users)
    return True, ""


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
    # Тот же выход, что у остальных провайдеров: в проде прямой доступ
    # закрыт, работает только прокси (config.proxies). trust_env=False —
    # прокси задаём явно, а не подхватываем из окружения.
    proxy = _egress_proxy()
    try:
        async with httpx.AsyncClient(
            timeout=120.0,
            proxy=proxy or None,
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
