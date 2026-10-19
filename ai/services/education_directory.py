"""Справочник DL «id → название» для учебных полей (вуз/школа, группа/класс).

REST get-user-info отдаёт только коды: schoolId (строка), schoolKind, schoolNo
и groupMaskId — настоящих названий там нет. Названия ведутся вручную в
``DLEducationName`` (/ai/admin/); рендер-резиденты (дневной отчёт журнала и
далее другие страницы) зовут helpers отсюда, а не читают модель напрямую
(DRY — единственный читатель модели).
"""

from ..models import DLEducationName

_PLACEHOLDER = "—"


def education_title_maps():
    """``(schools, groups, groups_school)`` — словари ``{key: title}`` и
    «группа → заведение» (один запрос к БД).

    Справочник — десятки строк, читаем целиком на запрос без кеша.
    ``groups_school`` — привязка группы к вузу/школе (key school-строки,
    заполнена у group-строк справочника, см. ``DLEducationName.school``).
    """
    schools, groups, groups_school = {}, {}, {}
    for row in DLEducationName.objects.all():
        if row.kind == DLEducationName.KIND_SCHOOL:
            schools[row.key] = row.title
        elif row.kind == DLEducationName.KIND_GROUP:
            groups[row.key] = row.title
            if row.school:
                groups_school[row.key] = row.school
    return schools, groups, groups_school


def school_display(schools, account):
    """Название вуза/школы студента для рендера.

    Цепочка: справочник (по education_school_id) → сырой schoolId →
    «kind №no» из schoolKind/schoolNo → «—». ``account`` — ExternalDLAccount
    или None (у записи нет внешнего аккаунта).
    """
    if account is None:
        return _PLACEHOLDER
    school_id = (account.education_school_id or "").strip()
    if school_id and school_id in schools:
        return schools[school_id]
    if school_id:
        return school_id
    kind = (account.education_school_kind or "").strip()
    no = (account.education_school_no or "").strip()
    if kind and no:
        return f"{kind} №{no}"
    if no:
        return f"№{no}"
    if kind:
        return kind
    return _PLACEHOLDER


def group_display(groups, account):
    """Название группы/класса студента для рендера.

    Цепочка: справочник (по education_group_mask_id) → сырой groupMaskId → «—».
    """
    if account is None:
        return _PLACEHOLDER
    mask = (account.education_group_mask_id or "").strip()
    if not mask:
        return _PLACEHOLDER
    return groups.get(mask, mask)


def school_title_for(schools, groups_school, account):
    """Школа/вуз студента для рендера: ``school_display`` + фолбэк «школа
    от группы».

    Если у студента школа не заполнена вовсе (``school_display`` вернул «—»),
    но его группа разрешима и привязана к заведению в справочнике
    (``groups_school``: groupMaskId → schoolId), показываем школу ОТ ГРУППЫ —
    группа знает своё заведение (``DLEducationName.school``). Сырой школьный
    id и «kind №N» не перекрываются: он честнее привязки группы.
    """
    title = school_display(schools, account)
    if title != _PLACEHOLDER or account is None:
        return title
    mask = (account.education_group_mask_id or "").strip()
    if not mask:
        return _PLACEHOLDER
    school_key = groups_school.get(mask)
    return schools.get(school_key, _PLACEHOLDER) if school_key else _PLACEHOLDER