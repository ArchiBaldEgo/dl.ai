"""Страница «Группы доступа» (/ai/admin/ai-groups/) — суперюзер или ключ page.groups.

Управление AIAccessGroup: создать группу, выдать ей права (ключи
AIAdminGroupPermission: вкладки-страницы, главы документации, действия) и
править СОСТАВ (поиск юзеров + построчный toggle — иммунитет к гонке
«подмножество чекбоксов перезаписало состав»). Все can_access_* (см.
ai/admin/permissions.py) резолвят ключи из этих групп, поэтому выдача
входит/выход вступает в силу сразу после сохранения.

Встроенные группы (is_builtin): нельзя ПЕРЕИМЕНОВАТЬ (проверки находят их
по имени), права правятся как у обычных; удалить можно и встроенную —
права при этом теряются, группа пересоздастся ПУСТОЙ при следующем
добавлении участника (get_or_create по имени). Режим «Посмотреть как…» не
проходит нигде (403).

Секция «Страницы» ДИНАМИЧЕСКАЯ (ТЗ 2026-10-19): источник — список
инструментов левого нава (ai_admin_site._ai_tool_defs), критерий выдачи —
URL реально зарегистрирован в urls'ах админки (resolve проходит). Новая
страница, добавленная в _ai_tool_defs со своим page.*/docs.*-ключом,
появляется здесь сама; «Документация» — так же по главам. В Списке
представлены ВСЕ страницы админки: и инструменты, и стоковые таблицы
(ModelAdmin'ы) — у каждой есть ключ (см. permissions/models).

Каждая страница раскрывается в ДЕЙСТВИЯ (9-й элемент кортежа инструмента:
вложенные (key, label)) — компактный аккордеон вместо плоского списка;
«читать» = сам чекбокс страницы, вложенные чекбоксы — создать/закрыть/
переписывать чужое/запускать прогоны и т.п. action-ключи показываются
ПОД СВОЕЙ страницей (в БД остаются прежними action.*-строками —
проверяющий код не менялся).
"""

from django.db import IntegrityError, transaction
from django.http import HttpResponseForbidden, JsonResponse
from django.template.response import TemplateResponse
from django.urls import resolve
from django.urls.exceptions import Resolver404
from django.views.decorators.http import require_GET, require_POST

from ..models import AIAccessGroup, AIAdminGroupPermission
from .guest_mode import is_viewing_as_guest
from .permissions import BASELINE_ACCESS_KEYS, can_access_access_groups
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


def _dynamic_page_doc_permissions(request):
    """Права «Страницы»/«Документация» из нава админки, синхронизированные с БД.

    Кандидаты — инструменты _ai_tool_defs с page.*/docs.*-ключом; выдаются
    только те, чей URL реально резолвится в urls'ах админки (критерий ТЗ:
    ссылка есть в urls.py И она в левом нав'е — второе верно по построению,
    _ai_tool_defs и есть нав). Строки БД подгоняются под реестр
    (get_or_create + освежение подписи) — миграции для новых страниц не
    нужны; вложенные действия страниц (9-й элемент кортежа) синхронизируются
    той же петлёй. Возврат: {key: {"label": str, "children": [(key, label)]}}.
    Ключи БД, не попавшие в реестр (устаревшие страницы/доки), НЕ
    показываются — выдача была бы бессмысленна.
    """
    available = {}
    for tool in ai_admin_site._ai_tool_defs(request):
        # (group, label, object_name, url, visible, icon, hint,
        #  access_key, actions)
        if len(tool) < 8 or not tool[7]:
            continue
        key, url = tool[7], tool[3]
        if not key.startswith(("page.", "docs.")):
            continue
        try:
            resolve(url)
        except Resolver404:
            continue  # маршрута в urls.py нет — ключ не выдаётся
        available[key] = {"label": tool[1], "children": list(tool[8] or ())}

    # Стоковые таблицы ModelAdmin'ов: ключ ACCESS_KEY на классе
    # (ai/admin/models.py::_StaffOnlyAdminMixin) — своей страницы в наве
    # у них нет, выдаются после нав-инструментов.
    for model, admin_obj in ai_admin_site._registry.items():
        key = getattr(admin_obj, "ACCESS_KEY", None)
        if key and key.startswith("page.") and key not in available:
            available[key] = {
                "label": str(model._meta.verbose_name_plural),
                "children": [],
            }

    if available:
        rows_to_sync = {}
        for key, entry in available.items():
            rows_to_sync[key] = (entry["label"], key)
            for child_key, child_label in entry["children"]:
                rows_to_sync.setdefault(child_key, (child_label, child_key))
        existing = {
            p.key: p
            for p in AIAdminGroupPermission.objects.filter(key__in=rows_to_sync)
        }
        for row_key, (label, key_for_section) in rows_to_sync.items():
            section_prefix = key_for_section.split(".")[0]
            section = {
                "docs": AIAdminGroupPermission.SECTION_DOCS,
                "action": AIAdminGroupPermission.SECTION_ACTION,
            }.get(section_prefix, AIAdminGroupPermission.SECTION_PAGE)
            row = existing.get(row_key)
            if row is None:
                AIAdminGroupPermission.objects.create(
                    key=row_key, label=label, section=section
                )
            elif row.label != label:
                row.label = label
                row.save(update_fields=["label"])
    return available


def _require_superuser(request):
    """Суперюзер ИЛИ держатель ключа page.groups; режим «Посмотреть как…» — нет."""
    return can_access_access_groups(request) and not is_viewing_as_guest(request)


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


# Ключи без собственной страницы (гейты): их не вывести из URL, они живут
# только в БД и не подрезаются динамическим реестром.
_ALWAYS_PAGE_KEYS = {"page.admin"}


def admin_access_groups_view(request):
    """Карточки-аккордеоны «Группы доступа»: свёрнуто = название + описание,
    раскрыто = форма (имя/описание/права) + редактор состава; ?group=<id>
    разворачивает нужную карточку (deep-link)."""
    if not _require_superuser(request):
        return HttpResponseForbidden("Access denied")

    groups = list(AIAccessGroup.objects.prefetch_related(
        "permissions", "users"
    ).order_by("is_builtin", "name"))

    dynamic = _dynamic_page_doc_permissions(request)
    try:
        selected_id = int(request.GET.get("group") or 0)
    except ValueError:
        selected_id = 0
    selected_group = next((g for g in groups if g.id == selected_id), None)

    permissions = list(AIAdminGroupPermission.objects.all())
    sections = [
        {"key": key, "title": title, "items": _section_items(permissions, key, dynamic)}
        for key, title in _SECTION_TITLES.items()
    ]
    # Правая колонка рисуется только когда есть что показывать: вложенные
    # действия страниц и/или действия-ключи без собственной страницы.
    has_actions_section = any(
        section["items"] for section in sections
        if section["key"] == AIAdminGroupPermission.SECTION_ACTION
    ) or any(
        item["children"]
        for section in sections
        if section["key"] == AIAdminGroupPermission.SECTION_PAGE
        for item in section["items"]
    )

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
            "open": group is selected_group,
            "save_url": f"{GROUPS_URL}{group.id}/save/" if group.id else "",
            "delete_url": f"{GROUPS_URL}{group.id}/delete/" if group.id else "",
            "toggle_url": f"{GROUPS_URL}{group.id}/members/toggle/" if group.id else "",
            "search_url": f"{GROUPS_URL}{group.id}/members/search/" if group.id else "",
            "members": [
                {"id": user.pk, "name": ai_admin_site._user_display_name(user)}
                for user in sorted(users, key=ai_admin_site._user_display_name)
            ],
        })

    context = {
        **ai_admin_site.each_context(request),
        "title": "Группы доступа",
        "rows": rows,
        "sections": sections,
        "create_url": f"{GROUPS_URL}0/save/",
        "groups_url": GROUPS_URL,
        # Базовые права помечаются в карточках «база» (включены и заблокированы):
        # набор галочек группы — это ТОЛЬКО явные ключи; baseline выдаётся
        # в КОДЕ любому с page.admin и в группу не пишется.
        "baseline_keys": BASELINE_ACCESS_KEYS,
        "has_actions_section": has_actions_section,
    }
    return TemplateResponse(request, "admin/ai/access_groups.html", context)


def _section_items(permissions, section, dynamic):
    """Элементы секции чекбоксов: {"key", "label", "children"}-словари.

    «Страницы»/«Документация» — ДИНАМИЧЕСКИ (порядок = порядок нава);
    у страницы, у которой есть вложенные действия, поле children несёт
    строки ключей — шаблон рисует их аккордеоном под страницей.
    «Действия» — только БД-ключи НЕ из реестра страниц (все action.*,
    привязанные к страницам, показываются вложенно, плоского списка нет).
    page.admin — гейт входа в админку, страницы не имеет: живёт в БД и
    идёт последним.
    """
    if section == AIAdminGroupPermission.SECTION_ACTION:
        nested = {
            child_key
            for entry in dynamic.values()
            for child_key, _ in entry["children"]
        }
        return [
            {"key": p.key, "label": p.label, "children": []}
            for p in permissions
            if p.section == section and p.key not in nested
        ]
    by_key = {p.key: p for p in permissions}
    items = []
    for key, entry in dynamic.items():
        row = by_key.get(key)
        if row is None or row.section != section:
            continue
        items.append({
            "key": row.key,
            "label": row.label,
            "children": [
                {"key": by_key[c_key].key, "label": by_key[c_key].label}
                for c_key, _ in entry["children"]
                if c_key in by_key
            ],
        })
    if section == AIAdminGroupPermission.SECTION_PAGE:
        # «Вход в админку ИИ» — гейт входа, самый верх списка (ТЗ: ключ
        # без него бессмыслен, он должен быть виден первым) — затем
        # страницы по порядку нава и стоковые таблицы.
        gate_items = [
            {"key": p.key, "label": p.label, "children": []}
            for p in permissions
            if p.key in _ALWAYS_PAGE_KEYS
        ]
        return gate_items + items
    return items


@require_POST
def admin_access_group_save_view(request, group_id):
    """Создать/обновить группу: имя, описание, набор прав (чекбоксы ключей).

    Builtin: имя не меняется (проверки ищут группу по имени через константы).

    Синхронизация реестра (_dynamic_page_doc_permissions) вызывается и здесь:
    иначе «Группы доступа», ни разу не открытые после добавления новой
    страницы, вернули бы 400 «Неизвестные права» на только что появившийся
    ключ (чекбокс-то рендерится динамикой, а БД ещё пуста).
    """
    if not _require_superuser(request):
        return _forbid()
    _dynamic_page_doc_permissions(request)
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
                if name != group.name:
                    return _forbid(
                        "Встроенную группу нельзя переименовать — проверки "
                        "находят её по имени"
                    )
            else:
                if not name:
                    return _forbid("Название группы обязательно")
            if not group.is_builtin:
                group.name = name
            group.description = description
            try:
                with transaction.atomic():
                    group.save(update_fields=(
                        ["description"] if group.is_builtin else ["name", "description"]
                    ))
            except IntegrityError:
                return _forbid(
                    "Группа с таким названием уже существует", status=400
                )
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
    """Удалить группу — в т.ч. встроенную (по решению владельца). Юзеры не
    удаляются — только M2M. Встроенная после удаления пересоздастся ПУСТОЙ
    при следующем добавлении участника (get_or_create по имени) — её права
    придётся перевыдавать, что подтверждается окном confirm."""
    if not _require_superuser(request):
        return _forbid()
    group = AIAccessGroup.objects.filter(pk=group_id).first()
    if group is None:
        return _forbid("Группа не найдена", status=404)
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


@require_GET
def admin_access_group_member_search_view(request, group_id):
    """AJAX-поиск юзеров НЕ в группе: GET {q} → {ok, users: [{id, name, toggle_url}]}.

    Раскрытая карточка ищет участников БЕЗ перезагрузки страницы
    (?group=&q= перегрузка убрана: страница-карточки рисует состав каждой
    группы, поиск — построчно по подстроке).
    """
    if not _require_superuser(request):
        return _forbid()
    group = AIAccessGroup.objects.filter(pk=group_id).first()
    if group is None:
        return _forbid("Группа не найдена", status=404)
    q = (request.GET.get("q") or "").strip()
    if not q:
        return JsonResponse({"ok": True, "users": []})
    return JsonResponse({"ok": True, "users": _search_users(group, q)})