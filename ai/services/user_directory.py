"""Справочник пользователей: отображаемое имя, DL-id и поиск для доступа.

Один владелец логики «как показать пользователя» (CLAUDE.md, DRY): то же
имя/DL-id нужны в шапке админки, в списках доступа к личным моделям и в
результатах поиска получателя. Сервис НЕ импортирует ``ai.admin`` — наоборот,
админка делегирует сюда.

Поиск ограничен сознательно: только активные пользователи, без email, групп
и прав; в ответе — исключительно ``{id, display, dl_id}``. Правила запроса —
в ``ai/services/user_search.py``: число — точный id (pk/DL-id/логин), текст
от 2 символов — ФИО/логин в любом порядке («Иванов Иван» = «Иван Иванов»).
"""

from __future__ import annotations

from django.contrib.auth import get_user_model

from .user_search import MIN_TEXT_QUERY_LEN, user_identity_q

#: Потолок выдачи поиска: выпадающий список в форме, а не справочник.
MAX_SEARCH_RESULTS = 20


def _external_account(user):
    """``ExternalDLAccount`` пользователя или None (лениво, без исключений)."""
    if not user or not getattr(user, "is_authenticated", False):
        return None
    # Обратный OneToOne: отсутствие записи поднимает
    # RelatedObjectDoesNotExist (подкласс AttributeError) — getattr гасит.
    return getattr(user, "external_dl_account", None)


def display_name_for_user(user) -> str:
    """Человекочитаемое имя: ФИО → имя+фамилия → DL-имена → username.

    DL-провижненные аккаунты — ``user_<id>``; префикс в имени не показываем.
    """
    if not user or not getattr(user, "is_authenticated", False):
        return ""
    full = (getattr(user, "get_full_name", lambda: "")() or "").strip()
    if full:
        return full
    first = (getattr(user, "first_name", "") or "").strip()
    last = (getattr(user, "last_name", "") or "").strip()
    if first or last:
        return f"{first} {last}".strip()
    account = _external_account(user)
    if account is not None:
        ext_first = (getattr(account, "external_first_name", "") or "").strip()
        ext_last = (getattr(account, "external_last_name", "") or "").strip()
        if ext_first or ext_last:
            return f"{ext_first} {ext_last}".strip()
    username = getattr(user, "get_username", lambda: "")() or ""
    if username.startswith("user_"):
        return username[5:]
    return username


def surname_for_user(user) -> str:
    """Фамилия для сортировки списков (ФИО, DL-справочник, первое слово).

    ``last_name`` может быть пустым у DL-провижненных аккаунтов — тогда
    берём ``external_last_name``; если и его нет, первое слово отображаемого
    имени (для формата «Фамилия Имя Отчество» это фамилия).
    """
    if not user:
        return ""
    last = (getattr(user, "last_name", "") or "").strip()
    if last:
        return last
    account = _external_account(user)
    if account is not None:
        ext_last = (getattr(account, "external_last_name", "") or "").strip()
        if ext_last:
            return ext_last
    display = display_name_for_user(user)
    return display.split()[0] if display else ""


def dl_id_for_user(user) -> str:
    """DL-идентификатор пользователя (``external_user_id`` либо username).

    Для DL-провижненных аккаунтов username — ``user_<id>``, поэтому префикс
    отбрасываем; локальным пользователям отдаём username, пустой — pk.
    """
    if not user or not getattr(user, "is_authenticated", False):
        return ""
    account = _external_account(user)
    external = (getattr(account, "external_user_id", "") or "").strip() if account else ""
    if external:
        return external
    username = getattr(user, "get_username", lambda: "")() or ""
    if username.startswith("user_"):
        return username[5:]
    return username or str(getattr(user, "pk", "") or "")


def user_brief(user) -> dict:
    """Минимальное описание пользователя для списков доступа."""
    return {
        "id": getattr(user, "pk", None),
        "display": display_name_for_user(user),
        "dl_id": dl_id_for_user(user),
    }


def search_users_for_sharing(query, exclude_user=None,
                             limit: int = MAX_SEARCH_RESULTS) -> list[dict]:
    """Найти получателей доступа: id точно, ФИО/логин в любом порядке.

    Возвращает ``[{id, display, dl_id}]`` — без email, групп и прав.
    Неактивные и сам инициатор исключены; пустой/слишком короткий текстовый
    запрос — пустой список (не отдаём весь справочник).
    """
    text = (query or "").strip()
    if not text:
        return []

    User = get_user_model()
    queryset = User.objects.filter(is_active=True)
    if exclude_user is not None and getattr(exclude_user, "pk", None):
        queryset = queryset.exclude(pk=exclude_user.pk)

    if not text.isdigit() and len(text) < MIN_TEXT_QUERY_LEN:
        return []
    # Единые правила (ai/services/user_search.py): число — точный id
    # (pk / DL-id / логин user_<id>), текст — ФИО/логин в любом порядке,
    # «Фамилия И.» — инициал по началу имени.
    identity_q = user_identity_q(text)
    if identity_q is None:
        return []
    queryset = queryset.filter(identity_q)

    try:
        limit = max(1, min(int(limit), MAX_SEARCH_RESULTS))
    except (TypeError, ValueError):
        limit = MAX_SEARCH_RESULTS

    users = (
        queryset.select_related("external_dl_account")
        .distinct()
        .order_by("username")[:limit]
    )
    return [user_brief(user) for user in users]
