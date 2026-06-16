"""Lightweight serializers for AI app API responses."""

from .i18n import get_localized_name


def programming_language(language):
    return {
        "id": language.id,
        "language_name": language.language_name,
        "name": language.language_name,
    }


def topic(obj, ui_language=""):
    return {
        "id": obj.id,
        "topic_name": obj.topic_name,
        "name": get_localized_name(obj, ui_language, "topic_name"),
        "programming_language": obj.programming_language_id,
    }


def prompt(obj, ui_language=""):
    return {
        "id": obj.id,
        "topic_id": obj.topic_id,
        "topic__programming_language": obj.topic.programming_language_id if obj.topic else None,
        "prompt_name": obj.prompt_name,
        "name": get_localized_name(obj, ui_language, "prompt_name"),
        "prompt_text": obj.prompt_text,
        "effective_text": obj.get_effective_text(ui_language, ""),
        "shared_prompt_id": obj.shared_prompt_id,
        "shared_prompt__prompt_name": obj.shared_prompt.prompt_name if obj.shared_prompt else None,
        "is_shared": bool(obj.shared_prompt),
    }


def shared_prompt(obj, ui_language=""):
    return {
        "id": obj.id,
        "prompt_name": obj.prompt_name,
        "name": get_localized_name(obj, ui_language, "prompt_name"),
        "prompt_text": obj.prompt_text,
        "effective_text": obj.get_effective_text(ui_language, ""),
        "language_ids": list(obj.programming_languages.values_list("id", flat=True)),
        "mode": obj.mode or "",
    }


def shared_prompt_with_dates(obj, ui_language=""):
    data = shared_prompt(obj, ui_language)
    data["created_at"] = obj.created_at.isoformat() if obj.created_at else None
    data["updated_at"] = obj.updated_at.isoformat() if obj.updated_at else None
    return data
