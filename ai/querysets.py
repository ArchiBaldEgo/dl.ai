"""Shared queryset helpers for the AI app."""

from django.db.models import Q

from .models import Prompt


def prompt_queryset_for_user(queryset, user):
    """Return prompts visible to the given user.

    Superusers/staff see all prompts. Prompt developers see prompts they own
    or are editors of. Anonymous users see nothing.
    """
    if not user or not getattr(user, "is_authenticated", False):
        return queryset.none()

    if user.is_superuser or user.is_staff:
        return queryset

    return queryset.filter(Q(owner=user) | Q(editors=user)).distinct()
