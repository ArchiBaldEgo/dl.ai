"""Авто-роль «Разработчик» для исполнителей тасков (доска /ai/admin/tasks/).

Единственный владелец правила (CLAUDE.md, DRY): суперюзер может поручить
таск ЛЮБОМУ активному пользователю; чтобы исполнитель увидел админку и
доску, он автоматически зачисляется в встроенную группу «Разработчик».
Членство, выданное так, помечается ``AIDevTaskDeveloperGrant`` — только
такие членства снимаются, когда у человека не осталось активных тасков.
Ручное членство через «Группы доступа» маркера не имеет и не трогается.

Сервис НЕ импортирует ``ai.admin`` на уровне модуля (админка делегирует
сюда): helper зачисления подтягивается лениво внутри функции.
"""

from __future__ import annotations

import logging

from ..constants import BUILTIN_GROUP_DEVELOPER
from ..models import AIDevTask, AIDevTaskDeveloperGrant, AIAccessGroup

logger = logging.getLogger(__name__)


def is_developer_member(user) -> bool:
    """Членство пользователя в группе «Разработчик» (без кэшей admin-слоя)."""
    if user is None or not getattr(user, "pk", None):
        return False
    return AIAccessGroup.objects.filter(
        name=BUILTIN_GROUP_DEVELOPER, users=user
    ).exists()


def has_active_tasks(user) -> bool:
    """Есть ли у пользователя незакрытый таск (он — исполнитель)."""
    if user is None or not getattr(user, "pk", None):
        return False
    return AIDevTask.objects.filter(
        taken_by=user, closed_at__isnull=True
    ).exists()


def ensure_task_developer(user, *, granted_by=None) -> bool:
    """Выдать авто-роль «Разработчик»; True — членство добавлено СЕЙЧАС.

    Уже член группы → False и маркер НЕ ставится: членство могло быть
    ручным, и автоматика не должна получить право его снимать.
    """
    if user is None or not getattr(user, "pk", None):
        return False
    if is_developer_member(user):
        return False
    from ..admin.permissions import add_user_to_builtin_ai_group

    add_user_to_builtin_ai_group(user, BUILTIN_GROUP_DEVELOPER)
    AIDevTaskDeveloperGrant.objects.get_or_create(
        user=user, defaults={"granted_by": granted_by}
    )
    return True


def release_task_developer_if_idle(user) -> bool:
    """Снять авто-роль, если активных тасков не осталось; True — сняли.

    Снимается ТОЛЬКО помеченное авто-членство: без строки-маркера
    пользователь добавлен вручную через «Группы доступа» — не трогаем.
    """
    if user is None or not getattr(user, "pk", None):
        return False
    grant = AIDevTaskDeveloperGrant.objects.filter(user=user).first()
    if grant is None:
        return False
    if has_active_tasks(user):
        return False
    group = AIAccessGroup.objects.filter(
        name=BUILTIN_GROUP_DEVELOPER, users=user
    ).first()
    if group is not None:
        group.users.remove(user)
    grant.delete()
    return True


def clear_auto_grant(user) -> None:
    """Забыть маркер: членство подтверждено вручную (или снято вручную)."""
    if user is None or not getattr(user, "pk", None):
        return
    AIDevTaskDeveloperGrant.objects.filter(user=user).delete()
