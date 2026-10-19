"""Страница «Группы доступа» (/ai/admin/ai-groups/) — только суперюзер.

Управление AIAccessGroup: создать группу, выдать ей права (ключи
AIAdminGroupPermission: вкладки-страницы, главы документации, действия) и
править СОСТАВ (поиск юзеров + построчный toggle — иммунитет к гонке
«подмножество чекбоксов перезаписало состав»). Все can_access_* (см.
ai/admin/permissions.py) резолвят ключи из этих групп, поэтому выдача
входит/выход вступает в силу сразу после сохранения.

Встроенные группы (is_builtin): нельзя удалить и переименовать; права
правятся как у обычных. Гостевой режим не проходит нигде (403).
"""

from django.db import IntegrityError, transaction
from django.http import HttpResponseForbidden, JsonResponse
from django.template.response import TemplateResponse
from django.views.decorators.http import require_POST

from ..models import AIAccessGroup, AIAdminGroupPermission
from .guest_mode import is_viewing_as_guest
from .permissions import is_superuser_user
from .site import ai_admin_site

GROUPS_URL = "/ai/admin/ai-groups/"
# Потолок выдачи поиска юзеров (пагинации нет — поиск по подстроке).
_SEARCH_LIMIT = 100

_SECTION_TITLES = {
    AIAdminGroupPermission.SECTION_PAGE: "Страницы",
    AIAdminGroupPermission.SECTION_DOCS: "Документация",
    AIAdminGroupPermission.SECTION_ACTION: "Действия",
}


def _forbid(message="Access denied", status=403):
    return JsonResponse({"ok": False, "message": message}, status=status)


def _require_superuser(request):
    return is_superuser_user(request.user) and not is_viewing_as_guest(request)


def _search_users(group, q):
    """Юзеры по подстроке (username/имя/фамилия), ещё не в выбранной группе."""
    from django.contrib.auth import get_user_model
    from django.db.models import Q

    User = get_user_model()
    member_ids = set(
        AIAccessGroup.objects.filter(id=group.id)
        .values_list("users__pk", flat=True)
    )
    query = Q(username__icontains=q) | Q(first_name__icontains=q) \
        | Q(last_name__icontains=q)
    users = (
        User.objects.filter(is_active=True)
        .filter(query)
        .exclude(pk__in={pk for pk in member_ids if pk})
        .order_by("username")[:_SEARCH_LIMIT]
    )
    return [
        {
            "id": user.pk,
            "name": ai_admin_site._user_display_name(user),
            "toggle_url": f"{GROUPS_URL}{group.id}/members/toggle/",
        }
        for user in users
    ]


def admin_access_groups_view(request):
    """Список групп + формы прав; редактор состава выбранной группы (?group=&q=)."""
    if not _require_superuser(request):
        return HttpResponseForbidden("Access denied")

    groups = list(AIAccessGroup.objects.prefetch_related(
        "permissions", "users"
    ).order_by("is_builtin", "name"))

    permissions = list(AIAdminGroupPermission.objects.all())
    sections = [
        {"key": key, "title": title, "items": [p for p in permissions if p.section == key]}
        for key, title in _SECTION_TITLES.items()
    ]

    try:
        selected_id = int(request.GET.get("group") or 0)
    except ValueError:
        selected_id = 0
    selected_group = next((g for g in groups if g.id == selected_id), None)

    found_users = []
    q = (request.GET.get("q") or "").strip()
    if selected_group and q:
        found_users = _search_users(selected_group, q)

    rows = []
    for group in groups:
        keys = {p.key for p in group.permissions.all()}
        users = list(group.users.all())
        rows.append({
            "id": group.id,
            "name": group.name,
            "description": group.description,
            "is_builtin": group.is_builtin,
            "member_count": len(users),
            "permission_keys": keys,
            "selected": group is selected_group,
            "save_url": f"{GROUPS_URL}{group.id}/save/" if group.id else "",
            "delete_url": f"{GROUPS_URL}{group.id}/delete/" if group.id else "",
            "toggle_url": f"{GROUPS_URL}{group.id}/members/toggle/" if group.id else "",
            "members_link": f"{GROUPS_URL}?group={group.id}",
            "members": [
                {"id": user.pk, "name": ai_admin_site._user_display_name(user)}
                for user in sorted(users, key=ai_admin_site._user_display_name)
            ] if group is selected_group else [],
        })

    context = {
        **ai_admin_site.each_context(request),
        "title": "Группы доступа",
        "rows": rows,
        "sections": sections,
        "selected_group": selected_group,
        "q": q,
        "found_users": found_users,
        "create_url": f"{GROUPS_URL}0/save/",
        "groups_url": GROUPS_URL,
    }
    return TemplateResponse(request, "admin/ai/access_groups.html", context)


@require_POST
def admin_access_group_save_view(request, group_id):
    """Создать/обновить группу: имя, описание, набор прав (чекбоксы ключей).

    Builtin: имя не меняется (проверки ищут группу по имени через константы).
    """
    if not _require_superuser(request):
        return _forbid()
    try:
        keys_requested = set(request.POST.getlist("permissions"))
    except Exception:
        keys_requested = set()

    name = (request.POST.get("name") or "").strip()
    description = (request.POST.get("description") or "").strip()

    with transaction.atomic():
        if group_id:
            group = AIAccessGroup.objects.filter(pk=group_id).first()
            if group is None:
                return _forbid("Группа не найдена", status=404)
            if group.is_builtin:
                if not name:
                    return _forbid("Название группы обязательно")
                if name != group.name:
                    return _forbid(
                        "Встроенную группу нельзя переименовать — проверки "
                        "находят её по имени"
                    )
            elif not name:
                return _forbid("Название группы обязательно")
            group.description = description
            group.save(update_fields=["description"])
        else:
            if not name:
                return _forbid("Название группы обязательно")
            try:
                group = AIAccessGroup.objects.create(
                    name=name, description=description
                )
            except IntegrityError:
                return _forbid(
                    "Группа с таким названием уже существует", status=400
                )

        invalid = keys_requested - set(
            AIAdminGroupPermission.objects.values_list("key", flat=True)
        )
        if invalid:
            return _forbid(f"Неизвестные права: {', '.join(sorted(invalid))}", status=400)
        group.permissions.set(
            AIAdminGroupPermission.objects.filter(key__in=keys_requested)
        )
    return JsonResponse({"ok": True})


@require_POST
def admin_access_group_delete_view(request, group_id):
    """Удалить группу (встроенные нельзя). Юзеры не удаляются — только M2M."""
    if not _require_superuser(request):
        return _forbid()
    group = AIAccessGroup.objects.filter(pk=group_id).first()
    if group is None:
        return _forbid("Группа не найдена", status=404)
    if group.is_builtin:
        return _forbid("Встроенную группу удалить нельзя")
    group.delete()
    return JsonResponse({"ok": True})


@require_POST
def admin_access_group_member_toggle_view(request, group_id):
    """Добавить/убрать одного юзера: POST {user_id, action: add|remove}.

    Построчный toggle вместо «чекбоксы по всем найденным»: сабмит частичного
    списка не может затереть состав, которого не видно в поисковой выдаче.
    """
    if not _require_superuser(request):
        return _forbid()
    group = AIAccessGroup.objects.filter(pk=group_id).first()
    if group is None:
        return _forbid("Группа не найдена", status=404)
    try:
        user_id = int(request.POST.get("user_id") or 0)
    except (TypeError, ValueError):
        return _forbid("Некорректный user_id", status=400)
    action = request.POST.get("action") or "add"
    from django.contrib.auth import get_user_model

    user = get_user_model().objects.filter(pk=user_id).first()
    if user is None:
        return _forbid("Пользователь не найден", status=404)
    if action == "remove":
        group.users.remove(user)
    else:
        group.users.add(user)
    return JsonResponse({"ok": True})