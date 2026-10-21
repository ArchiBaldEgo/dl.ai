"""Названия вуза/школы и группы/класса — из данных DL.

Начиная с версии 1.4.2 REST API (dl-rest-api.json, EducationInfo) DL сам
отдаёт человекочитаемые названия учебного заведения и группы:
``schoolName``/``formName``/``groupName`` — рядом с ID (``schoolId``/
``groupMaskId``). Они сохраняются в ``ExternalDLAccount`` при входе
пользователя и дозабираются массово через ``GET /restapi/get-id-user-info``
(этот endpoint не требует sessionId — в отличие от ``get-user-info``).

Раньше названий в API не было, поэтому их вели вручную в справочнике
``DLEducationName``. Справочник остался в базе (его данные полезны как
историческая привязка «группа → заведение» и как запасной источник, если
название у конкретного студента не пришло), но рендер больше НЕ зависит от
его актуальности: сначала берётся название из DL, потом — из справочника,
и только потом — сырой ID.

Читатель — единственный: этот модуль.
"""

from django.core.cache import cache

from ..constants import AI_CACHE_KEY_PREFIX
from ..models import DLEducationName

_PLACEHOLDER = "—"

# Ключ кеша справочника. Инвалидация — по post_save/post_delete
# DLEducationName (ai/signals.py), поэтому TTL — только страховка.
_EDU_MAPS_CACHE_KEY = f"{AI_CACHE_KEY_PREFIX}:education_title_maps"
_EDU_MAPS_CACHE_TTL = 300


def education_title_maps():
    """``(schools, groups, groups_school)`` — словари ``{key: title}`` и
    «группа → заведение» (один запрос к БД, дальше — кеш).

    Справочник — десятки строк; он читался целиком на КАЖДЫЙ рендер (дневной
    отчёт журнала, «Таски») без кеша. Теперь результат кешируется в
    django-cache; правка справочника сбрасывает кеш через сигнал
    (ai/signals.py), так что изменения видны сразу.

    ``groups_school`` — привязка группы к вузу/школе (key school-строки,
    заполняена у group-строк справочника, см. ``DLEducationName.school``).
    """
    cached = cache.get(_EDU_MAPS_CACHE_KEY)
    if cached is not None:
        return cached
    schools, groups, groups_school = {}, {}, {}
    for row in DLEducationName.objects.all():
        if row.kind == DLEducationName.KIND_SCHOOL:
            schools[row.key] = row.title
        elif row.kind == DLEducationName.KIND_GROUP:
            groups[row.key] = row.title
            if row.school:
                groups_school[row.key] = row.school
    cache.set(_EDU_MAPS_CACHE_KEY, (schools, groups, groups_school), _EDU_MAPS_CACHE_TTL)
    return schools, groups, groups_school


def _dl_school_name(account) -> str:
    """Название учебного заведения, присланное DL (``education.schoolName``)."""
    if account is None:
        return ""
    return (getattr(account, "education_school_name", "") or "").strip()


def _dl_group_name(account) -> str:
    """Название группы/класса, присланное DL (``education.groupName``)."""
    if account is None:
        return ""
    return (getattr(account, "education_group_name", "") or "").strip()


def _school_base(schools, account):
    """Школьные поля студента: ``(display, key, has_title)``.

    ``display`` — что показываем, ``key`` — ID вуза/школы в DL (пусто, если
    его у студента нет), ``has_title`` — есть ли человекочитаемое название
    (False → в UI честная пометка «нет названия»).

    Цепочка: название из DL (``schoolName``) → справочник (по
    ``education_school_id``) → сырой schoolId → «kind №no» → «—».
    """
    if account is None:
        return _PLACEHOLDER, "", False
    dl_name = _dl_school_name(account)
    school_id = (account.education_school_id or "").strip()
    if dl_name:
        return dl_name, school_id, True
    if school_id:
        title = schools.get(school_id)
        if title:
            return title, school_id, True
        # Ключа нет в справочнике — показываем его как есть.
        return school_id, school_id, False
    kind = (account.education_school_kind or "").strip()
    no = (account.education_school_no or "").strip()
    if kind and no:
        return f"{kind} №{no}", "", False
    if no:
        return f"№{no}", "", False
    if kind:
        return kind, "", False
    return _PLACEHOLDER, "", False


def school_display(schools, account):
    """Название вуза/школы студента для рендера.

    Цепочка: название из DL → справочник (по education_school_id) → сырой
    schoolId → «kind №no» → «—». ``account`` — ExternalDLAccount или None
    (у записи нет внешнего аккаунта).
    """
    return _school_base(schools, account)[0]


def school_info(schools, groups_school, account):
    """Вуз/школа студента с её ключом: ``(display, key, has_title)``.

    Школьные поля студента пусты → заведение ОТ ГРУППЫ (группа знает своё
    заведение, ``DLEducationName.school``). Сырой школьный id и «kind №N» не
    перекрываются: он честнее привязки группы.
    """
    display, key, has_title = _school_base(schools, account)
    if display != _PLACEHOLDER or account is None:
        return display, key, has_title
    mask = (account.education_group_mask_id or "").strip()
    if not mask:
        return _PLACEHOLDER, "", False
    school_key = groups_school.get(mask)
    if not school_key:
        return _PLACEHOLDER, "", False
    title = schools.get(school_key)
    if title:
        return title, school_key, True
    return school_key, school_key, False


def group_display(groups, account):
    """Название группы/класса студента для рендера.

    Цепочка: название из DL (``groupName``) → справочник (по
    ``education_group_mask_id``) → сырой groupMaskId → «—».
    """
    return group_info(groups, account)[0]


def group_info(groups, account):
    """Группа/класс студента с её ключом: ``(display, key, has_title)``.

    ``has_title`` — False, если названия группы нет ни в DL, ни в справочнике
    (тогда ``display`` — сырой groupMaskId, и UI честно пишет «нет названия»).
    """
    if account is None:
        return _PLACEHOLDER, "", False
    dl_name = _dl_group_name(account)
    mask = (account.education_group_mask_id or "").strip()
    if dl_name:
        return dl_name, mask, True
    if not mask:
        return _PLACEHOLDER, "", False
    title = groups.get(mask)
    if title:
        return title, mask, True
    return mask, mask, False


def school_title_for(schools, groups_school, account):
    """Школа/вуз студента для рендера: ``school_display`` + фолбэк «школа
    от группы» (см. ``school_info`` — он же отдаёт ключ и признак
    «названия нет»).
    """
    return school_info(schools, groups_school, account)[0]
