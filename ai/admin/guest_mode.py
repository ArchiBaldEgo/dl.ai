"""Режим «Посмотреть как…» (бывший «Зайти как гость») — полная эмуляция.

Только для суперюзера: кнопка в шапке открывает окно выбора ГРУППЫ доступа;
выбор пишется в сессию, и админка на время запроса показывает РОВНО то, что
видит участник этой группы: супер-обход отключён (staff/super-проверки
возвращают False), ключи = ключи выбранной группы (+ baseline при page.admin),
членство в ролевых группах — только выбранная группа (см.
permissions._view_as_group_pk + user_access_keys/is_prompt_developer_user).

Механика честности: request.user — НАСТОЯЩИЙ Model instance (ORM-потоки —
owner=…, m2m add — работают как у пользователя), но с атрибутом
``_ai_view_as_group_pk``, проставляемым до wrapped_view в
AIAdminSite.admin_view и только для ВИДА: он не сохраняется ни в БД, ни в
сессии, поэтому полная админка возвращается снятием ключа сессии
(«Полная админка» в том же окне).

Эндпоинты переключения (select/groups) получают реальный суперпользователь:
их url-имена (и путь /view-as/) исключены из наложения эмуляции — иначе
is_superuser_user(request.user) был бы False, и выйти из режима нельзя.
"""

from django.db.models import Count
from django.http import JsonResponse
from django.views.decorators.http import require_GET, require_POST

from ..constants import (
    BUILTIN_GROUP_DEVELOPER,
    BUILTIN_GROUP_PROMPT_DEV,
)
from ..models import AIAccessGroup
from .permissions import is_superuser_user
from .site import ai_admin_site

# Ключ сессии: pk группы («Посмотреть как участник этой группы»).
SESSION_KEY = "ai_view_as_group"
# Атрибут на request.user на время запроса (честная эмуляция).
VIEW_AS_ATTR = "_ai_view_as_group_pk"
# Легаси-ключ визуального гостевого режима (полн. замена — просто игнорим).
LEGACY_SESSION_KEY = "ai_view_as_guest"


def is_viewing_as_guest(request) -> bool:
    """Активен режим «Посмотреть как…» (только суперюзер может включить)."""
    return bool(getattr(request.user, "is_superuser", False)) and bool(
        getattr(request.user, VIEW_AS_ATTR, None)
        or request.session.get(SESSION_KEY)
    )


def view_as_group(request):
    """Объект группы-эмуляции этого запроса (или None)."""
    pk = getattr(request.user, VIEW_AS_ATTR, None) or request.session.get(SESSION_KEY)
    if not pk:
        return None
    try:
        return AIAccessGroup.objects.filter(pk=int(pk)).first()
    except (TypeError, ValueError):
        return None


def apply_view_as_user(request):
    """Проставить честный контекст эмуляции на request.user.

    Ставится В НАЧАЛЕ admin_view до всех проверок; атрибут живёт в памяти
    инстанса юзера, который Django пересоздаёт на каждый запрос — между
    запросами ничего не утекает. Сессионный флаг проверяется на
    суперюзерство здесь (не суперу ставить эмуляцию нечего).
    """
    request.session.pop(LEGACY_SESSION_KEY, None)
    pk = request.session.get(SESSION_KEY)
    if pk and getattr(request.user, "is_superuser", False):
        try:
            request.user.__setattr__(VIEW_AS_ATTR, int(pk))
        except (TypeError, ValueError):
            pass


@ai_admin_site.admin_view
@require_GET
def admin_view_as_groups_view(request):
    """GET-JSON для окна выбора: группы с флагом page.admin и текущей."""

    if not is_superuser_user(request.user):
        return JsonResponse({"ok": False}, status=403)
    current = view_as_group(request)
    rows = []
    # members — через annotate(Count), а не group.users.count() на каждую
    # группу (был N+1: по запросу на группу).
    groups_qs = (
        AIAccessGroup.objects
        .prefetch_related("permissions")
        .annotate(members_count=Count("users"))
        .order_by("is_builtin", "name")
    )
    for group in groups_qs:
        keys = {p.key for p in group.permissions.all()}
        if "page.admin" not in keys:
            continue  # члены такой группы в админку не входят — показывать нечего
        rows.append({
            "id": group.pk,
            "name": group.name,
            "builtin": group.is_builtin,
            "members": group.members_count,
            "current": bool(current and current.pk == group.pk),
            "keys": sorted(keys),
        })
    return JsonResponse({"ok": True, "groups": rows})


@ai_admin_site.admin_view
@require_POST
def admin_view_as_select_view(request):
    """POST {group_id: <id>|0} — включить/выключить режим «Посмотреть как…»."""

    if not getattr(request.user, "is_superuser", False):
        return JsonResponse({"ok": False}, status=403)
    raw = request.POST.get("group_id") or "0"
    try:
        group_id = int(raw)
    except ValueError:
        return JsonResponse({"ok": False, "message": "Некорректная группа"}, status=400)
    if group_id == 0:
        request.session.pop(SESSION_KEY, None)
        request.user.__setattr__(VIEW_AS_ATTR, None)
    else:
        group = AIAccessGroup.objects.filter(pk=group_id).first()
        if group is None:
            return JsonResponse({"ok": False, "message": "Группа не найдена"}, status=404)
        request.session[SESSION_KEY] = group.pk
        request.user.__setattr__(VIEW_AS_ATTR, group.pk)
    return JsonResponse({"ok": True})