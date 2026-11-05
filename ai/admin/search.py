"""Универсальный поиск по пользователю для ModelAdmin (changelist-поиск).

Миксин добавляется к админкам, где в строке поиска ищут человека. Он
ОБЪЕДИНЯЕТ штатный поиск Django (``search_fields``) с универсальным
совпадением по пользователю: id (pk / DL-id / логин) и ФИО в любом порядке
(«Иванов Иван», «Иван Иванов», «Иванов И.»).

Объявление:

* ``user_search_relations`` — пути связи с ``User`` (``owner``, ``editors``);
* ``user_search_flat_fields`` — денормализованные поля самой модели
  (``external_user_id``, ``username``, ``user_full_name``);
* ``user_search_self`` — сама модель и есть ``User``.

Сама логика запроса живёт в ``ai/services/user_search.py`` (DRY).
"""

from django.db.models import Q

from ..services.user_search import (
    MIN_TEXT_QUERY_LEN,
    flat_user_q,
    user_identity_q,
)


class UniversalUserSearchMixin:
    user_search_relations: tuple[str, ...] = ()
    user_search_flat_fields: tuple[str, ...] = ()
    user_search_self = False

    def get_universal_user_q(self, search_term: str) -> Q | None:
        """Q «человек подходит под запрос» или None (нечем искать)."""
        search_term = (search_term or "").strip()
        if not search_term:
            return None
        # Одиночная буква матчила бы почти всех (username/first_name), и
        # «q=A» тащил бы в выдачу промпты всех пользователей с «a» в логине.
        # Короткий TEXT-запрос оставляем штатному поиску; числовой id — всегда.
        if not search_term.isdigit() and len(search_term) < MIN_TEXT_QUERY_LEN:
            return None

        q = None
        if self.user_search_self:
            q = user_identity_q(search_term)

        for relation in self.user_search_relations:
            relation_q = user_identity_q(search_term, prefix=relation)
            if relation_q is None:
                continue
            q = relation_q if q is None else (q | relation_q)

        if self.user_search_flat_fields:
            flat_q = flat_user_q(self.user_search_flat_fields, search_term)
            if flat_q is not None:
                q = flat_q if q is None else (q | flat_q)
        return q

    def get_search_results(self, request, queryset, search_term):
        results, may_have_duplicates = super().get_search_results(
            request, queryset, search_term,
        )
        universal_q = self.get_universal_user_q(search_term)
        if universal_q is None:
            return results, may_have_duplicates

        # Если у админки нет search_fields, штатный поиск вернул ВЕСЬ queryset —
        # объединять с ним универсальную ветку нельзя (получили бы всё).
        if not self.get_search_fields(request):
            return queryset.filter(universal_q).distinct(), True

        universal_results = queryset.filter(universal_q)
        combined = (results | universal_results).distinct()
        return combined, True
