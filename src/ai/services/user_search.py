"""Универсальный поиск по пользователю: id / «Фамилия Имя» / «Имя Фамилия» / «Фамилия И.О.».

Один владелец логики «как понять введённый пользователем текст» (DRY,
CLAUDE.md): его используют поиск changelist'ов (``UniversalUserSearchMixin``),
текстовые фильтры журнала/токенов/дневного отчёта, справочник получателей
доступа и поиск по автору коммитов.

Правила запроса:

* целиком числовой запрос — это **точный** id (Django pk, DL-id или логин
  ``user_<id>``), а не подстрока: ``10`` не находит пользователя ``101``;
  явная форма ``id_10`` равнозначна числу ``10`` (тот же точный id);
* текстовый запрос разбивается на слова; каждое слово должно найтись в
  ЛЮБОМ из полей ФИО/логина (AND по словам, OR по полям) — поэтому
  «Иванов Иван» и «Иван Иванов» находят одного человека;
* слово-инициал с точками (``И.``, ``И.О.``) сравнивается с началом слов
  ФИО, а не как подстрока: ``Иванов И.`` находит «Иван Иванович…», если
  в данных есть имя/фамилия на «И».

Сервис НЕ импортирует ``ai.admin`` — наоборот, админка делегирует сюда.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from django.contrib.auth import get_user_model
from django.db.models import Q

#: Минимальная длина текстового запроса для автодополнения получателя.
MIN_TEXT_QUERY_LEN = 2

#: Поля, по которым ищется ФИО (User + связанный DL-аккаунт).
NAME_FIELDS = (
    "username",
    "first_name",
    "last_name",
    "external_dl_account__external_first_name",
    "external_dl_account__external_last_name",
)

#: Поля только имён — для инициалов (логин не участвует).
_PERSON_NAME_FIELDS = (
    "first_name",
    "last_name",
    "external_dl_account__external_first_name",
    "external_dl_account__external_last_name",
)

#: Поля идентификатора пользователя.
_ID_FIELDS = (
    "pk",
    "username",
    "external_dl_account__external_user_id",
)

_TOKEN_SPLIT_RE = re.compile(r"\s+")

#: Явная форма id: ``id_12345`` (= ``12345``). Регистр не важен; сам текст
#: всё равно сравнивается с логином целиком — локальные логины ``id_*``
#: находятся как раньше.
_ID_PREFIX_RE = re.compile(r"id_(\d+)", re.IGNORECASE)


def _tokens(query: str) -> list[str]:
    return [token for token in _TOKEN_SPLIT_RE.split((query or "").strip()) if token]


def _id_digits(text: str) -> str | None:
    """``id_12345`` → ``"12345"``; иначе None."""
    match = _ID_PREFIX_RE.fullmatch((text or "").strip())
    return match.group(1) if match else None


def _prefix(prefix: str) -> str:
    """``owner`` → ``owner__``; пустой префикс — обращение к самому User."""
    prefix = (prefix or "").strip()
    return f"{prefix}__" if prefix else ""


def _initial_parts(token: str) -> list[str] | None:
    """``И.`` → ``["И"]``, ``И.О.`` → ``["И", "О"]``; иначе None.

    Инициалами считаем только токен из букв и точек (1–3 буквы в каждой
    части) — «ivanov.» или «1.5» остаются обычным текстом.
    """
    if "." not in token:
        return None
    parts = [part for part in token.split(".") if part]
    if not parts:
        return None
    if not all(1 <= len(part) <= 3 and part.isalpha() for part in parts):
        return None
    return parts


def exact_user_id_q(query: str, prefix: str = "") -> Q | None:
    """Q точного совпадения id: pk, DL-id, логин или форма ``id_<id>``."""
    text = (query or "").strip()
    if not text:
        return None
    base = _prefix(prefix)
    q = Q(**{f"{base}username__iexact": text})
    q |= Q(**{f"{base}external_dl_account__external_user_id__iexact": text})
    digits = text if text.isdigit() else _id_digits(text)
    if digits is not None:
        try:
            q |= Q(**{f"{base}pk": int(digits)})
        except (TypeError, ValueError):
            pass
        q |= Q(**{f"{base}external_dl_account__external_user_id__iexact": digits})
        q |= Q(**{f"{base}username__iexact": f"user_{digits}"})
    return q


def _term_q(term: str, prefix: str) -> Q:
    """Одно слово запроса: OR по всем полям ФИО/логина (или инициалы)."""
    base = _prefix(prefix)
    parts = _initial_parts(term)
    if parts:
        q = Q()
        for part in parts:
            part_q = Q()
            for field_name in _PERSON_NAME_FIELDS:
                part_q |= Q(**{f"{base}{field_name}__istartswith": part})
            q &= part_q
        return q

    q = Q()
    for field_name in NAME_FIELDS:
        q |= Q(**{f"{base}{field_name}__icontains": term})
    if term.isdigit() or _id_digits(term) is not None:
        id_q = exact_user_id_q(term, prefix)
        if id_q is not None:
            q |= id_q
    return q


def user_identity_q(query: str, prefix: str = "") -> Q | None:
    """Универсальный Q для одного User (``prefix`` — путь связи, напр. owner).

    Возвращает ``None`` для пустого запроса (тогда фильтр не применяется).
    """
    text = (query or "").strip()
    if not text:
        return None
    if text.isdigit() or _id_digits(text) is not None:
        return exact_user_id_q(text, prefix)

    q = Q()
    for term in _tokens(text):
        q &= _term_q(term, prefix)
    return q


def filter_users(queryset, query: str):
    """Отфильтровать queryset пользователей универсальным запросом."""
    q = user_identity_q(query)
    if q is None:
        return queryset
    return queryset.filter(q)


def flat_text_q(fields, query: str) -> Q | None:
    """Q по «плоским» текстовым полям (без FK): AND по словам, OR по полям.

    Для денормализованных ФИО/логинов в журнале, авторе коммита и т.п.
    Числовой запрос ищется как точное значение любого поля.
    """
    text = (query or "").strip()
    if not text:
        return None
    fields = tuple(fields)
    if not fields:
        return None
    terms = _tokens(text)
    if text.isdigit():
        q = Q()
        for field_name in fields:
            q |= Q(**{f"{field_name}__iexact": text})
        return q

    q = Q()
    for term in terms:
        term_q = Q()
        for field_name in fields:
            term_q |= Q(**{f"{field_name}__icontains": term})
        q &= term_q
    return q


def identity_index_q(query: str):
    """Подзапрос «пользователи, подходящие под запрос» для ``__in``.

    Нужен там, где хранится не FK, а денормализованные DL-id/логин
    (``AIRequestLog``, ``TaskSolution``).
    """
    q = user_identity_q(query)
    if q is None:
        return None
    return get_user_model().objects.filter(q)


def flat_user_q(fields, query: str) -> Q | None:
    """Q по денормализованным полям пользователя (id/логин/ФИО).

    Совмещает пословный поиск по полям с разрешением запроса в реальных
    пользователей: так ``Фамилия И.`` находит и денормализованное «Иван
    Иванов», потому что инициал разрешается через таблицу User.
    """
    text = (query or "").strip()
    if not text:
        return None
    fields = tuple(fields)
    q = flat_text_q(fields, text)
    users = identity_index_q(text)
    if users is None:
        return q
    if "external_user_id" in fields:
        q |= Q(
            external_user_id__in=users.values(
                "external_dl_account__external_user_id"
            )
        )
    if "username" in fields:
        q |= Q(username__in=users.values("username"))
    return q


def identity_matches(query: str, *, texts=(), ids=()) -> bool:
    """Python-проверка строки (daily report, где фильтр идёт после агрегации).

    ``texts`` — ФИО/логины строки, ``ids`` — её внешние id. Семантика та же:
    числовой запрос — точный id, слова — AND по подстрокам, инициалы — по
    началу слов.
    """
    text = (query or "").strip()
    if not text:
        return True
    id_values = {str(value) for value in ids if value not in (None, "")}
    digits = text if text.isdigit() else _id_digits(text)
    if digits is not None:
        return digits in id_values

    word_sources = [str(source or "") for source in texts]
    words = [
        word
        for source in word_sources
        for word in _TOKEN_SPLIT_RE.split(source.strip())
        if word
    ]
    folded_sources = [source.casefold() for source in word_sources]
    for term in _tokens(text):
        parts = _initial_parts(term)
        if parts:
            for part in parts:
                if not any(
                    word.casefold().startswith(part.casefold()) for word in words
                ):
                    return False
        else:
            needle = term.casefold()
            if not any(needle in source for source in folded_sources):
                return False
    return True


@dataclass
class UserIdentity:
    """Набор совпавших пользователей (для фильтрации строк в Python)."""

    pks: set[int] = field(default_factory=set)
    usernames: set[str] = field(default_factory=set)
    dl_ids: set[str] = field(default_factory=set)

    @classmethod
    def resolve(cls, query: str) -> "UserIdentity":
        instance = cls()
        users = identity_index_q(query)
        if users is None:
            return instance
        for row in users.select_related("external_dl_account").values(
            "pk", "username", "external_dl_account__external_user_id",
        ):
            instance.pks.add(row["pk"])
            username = (row["username"] or "").strip()
            if username:
                instance.usernames.add(username)
            dl_id = (row["external_dl_account__external_user_id"] or "").strip()
            if dl_id:
                instance.dl_ids.add(dl_id)
        return instance

    def contains(self, *, pk=None, username="", external_id="") -> bool:
        return bool(
            (pk and pk in self.pks)
            or (username and username in self.usernames)
            or (external_id and str(external_id) in self.dl_ids)
        )
