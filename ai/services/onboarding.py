"""Онбординг-wizard: «показывать ли тур» и отметка «просмотрено».

Единственный владелец правила «каждый пользователь хоть раз видит wizard»:
тур показывается, когда для scope нет строки AIWizardSeen или её version
меньше WIZARD_VERSION. Отмечается любое закрытие тура (Готово/Скрыть/Escape).
Сессию для флага использовать нельзя: она живёт 8 часов и сбрасывается при
инвалидации DLSID, а флаг должен переживать и то, и другое.

Смена роли админки хука не требует: общий тур разделён по правам
(admin_pd/admin_staff/admin_super) — новая роль
даёт новый scope без отметки → тур показывается; понижение роли возвращает
старый scope с отметкой → заново ничего не показывается.
"""

import json
import logging

from django.utils import timezone

from ..constants import WIZARD_VERSION
from ..models import AIWizardSeen

logger = logging.getLogger(__name__)

#: scope'ы — ключ шага «где показывать тур» (реестр шагов — в ai-wizard.js).
#: Общий тур админки разделён по правам (admin_pd/admin_staff/admin_super):
#: повышение роли → свой тур ещё не был пройден → показ; понижение — строка
#: младшего тура уже есть → заново не показывается.
SCOPES = (
    "chat",
    "solve",
    "find_error",
    "admin_pd",
    "admin_staff",
    "admin_super",
    "admin_arm_solve",
    "admin_model_status",
)

#: Юзерские scope'ы с «общими» шагами (язык/модель/переключатель страниц):
#: первый просмотренный из них закрывает общие шаги для остальных (cfg.basics_seen).
USER_SCOPES = ("chat", "solve", "find_error")

MARK_URL_USER = "/ai/api/wizard-seen/"
MARK_URL_ADMIN = "/ai/admin/wizard/seen/"


def scope_name_valid(scope):
    """True — scope входит в SCOPES."""
    return scope in SCOPES


def user_basics_seen(user, exclude_scope):
    """True — юзер уже видел тур любой ДРУГОЙ юзер-страницы.

    Шаги-«общие» (язык/модель/переключатель, common в реестре движка) одинаковы
    во всех юзер-турах: показав их один раз, повтор не нужен.
    """
    if exclude_scope not in USER_SCOPES:
        return False
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    others = tuple(s for s in USER_SCOPES if s != exclude_scope)
    try:
        return AIWizardSeen.objects.filter(user=user, scope__in=others).exists()
    except Exception:
        # Нет таблицы до миграции — не повод ронять ради подсказок страницу.
        logger.exception("Failed to read wizard basics: exclude=%s", exclude_scope)
        return False


def should_show_wizard(user, scope):
    """Нет отметки (или старая версия) → True.

    Аноним/фейк-пользователь (нет ``is_authenticated``) → False,
    сам флаг не пишем.
    """
    if not scope_name_valid(scope):
        return False
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    try:
        row = AIWizardSeen.objects.filter(user=user, scope=scope).only("version").first()
    except Exception:
        # Таблицы может не быть до миграции — не роняем ради тура страницу.
        logger.exception("Failed to read wizard state: scope=%s", scope)
        return False
    return row is None or row.version < WIZARD_VERSION


def record_wizard_seen(user, scope):
    """Закрытие тура → отметка текущей версии (idempotent, бампит version)."""
    if not scope_name_valid(scope):
        return
    if user is None or not getattr(user, "is_authenticated", False):
        return
    try:
        AIWizardSeen.objects.update_or_create(
            user=user,
            scope=scope,
            defaults={"version": WIZARD_VERSION, "created_at": timezone.now()},
        )
    except Exception:
        logger.exception("Failed to record wizard seen: scope=%s", scope)


def wizard_boot_payload(user, scope, mark_url, role="", basics_seen=False):
    """Конфиг для {{ ai_wizard|json_script }} в шаблоне (ai-wizard.js читает его)."""
    return {
        "scope": scope,
        "version": WIZARD_VERSION,
        "show": should_show_wizard(user, scope),
        "role": role,
        "basics_seen": bool(basics_seen),
        "mark_url": mark_url,
    }


def scope_from_request_body(body):
    """scope из JSON-тела POST либо None (битый JSON/неизвестный scope)."""
    try:
        payload = json.loads(body or b"{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    scope = payload.get("scope")
    return scope if scope_name_valid(scope) else None