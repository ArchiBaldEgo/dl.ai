"""Справочник пользователей: отображаемое имя, DL-id и поиск для доступа.

Один владелец логики «как показать пользователя» (CLAUDE.md, DRY): то же
имя/DL-id нужны в шапке админки, в списках доступа к личным моделям и в
результатах поиска получателя. Сервис НЕ импортирует ``ai.admin`` — наоборот,
админка делегирует сюда.

Поиск ограничен сознательно: только активные пользователи, без email, групп
и прав; в ответе — исключительно ``{id, display, dl_id}``. Целое число
трактуется как точный DL-id (его можно ввести коротким), иначе — подстрока
от 2 символов по username/ФИО.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.db.models import Q

#: Потолок выдачи поиска: выпадающий список в форме, а не справочник.
MAX_SEARCH_RESULTS = 20

#: Минимальная длина текстового запроса (короче — слишком много совпадений).
MIN_QUERY_LEN = 2


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
    """Найти получателей доступа по DL-id (точно) или ФИО/логину (подстрока).

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

    if text.isdigit():
        # Точный DL-id: пользователь вводит его целиком, длина не важна.
        queryset = queryset.filter(external_dl_account__external_user_id=text)
    else:
        if len(text) < MIN_QUERY_LEN:
            return []
        queryset = queryset.filter(
            Q(username__icontains=text)
            | Q(first_name__icontains=text)
            | Q(last_name__icontains=text)
            | Q(external_dl_account__external_first_name__icontains=text)
            | Q(external_dl_account__external_last_name__icontains=text)
        )

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
