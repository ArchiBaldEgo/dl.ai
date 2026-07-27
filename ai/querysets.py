"""Общие хелперы queryset для AI-приложения.

Содержит функцию prompt_queryset_for_user для фильтрации промптов по правам
пользователя (владелец/редактор/суперпользователь).
"""

from django.db.models import Q

from .models import Prompt


def prompt_queryset_for_user(queryset, user):
    """Возвращает промпты, видимые данному пользователю.

    Суперпользователи и staff видят все промпты. Разработчики промптов видят
    только те, которыми владеют или редакторами которых являются.
    Анонимные пользователи не видят ничего.
    """
    if not user or not getattr(user, "is_authenticated", False):
        return queryset.none()

    if user.is_superuser or user.is_staff:
        return queryset

    return queryset.filter(Q(owner=user) | Q(editors=user)).distinct()
