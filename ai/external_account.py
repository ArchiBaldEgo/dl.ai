"""
External DL account model and utilities for user provisioning.
"""
from django.contrib.auth import get_user_model
from django.db import IntegrityError
from .models import ExternalDLAccount
from .constants import PROMPT_DEVELOPER_GROUP
import logging

logger = logging.getLogger(__name__)
User = get_user_model()


def _find_available_username(base_username: str) -> str:
    """Find an available username by appending numeric suffix if needed."""
    if not User.objects.filter(username=base_username).exists():
        return base_username
    
    counter = 1
    while True:
        candidate = f"{base_username}_{counter}"
        if not User.objects.filter(username=candidate).exists():
            return candidate
        counter += 1


def _extract_external_login(user_info: dict) -> str:
    """Extract a login/nickname from API payload using known candidate keys."""
    for key in ("login", "username", "userName", "nickname", "nick"):
        value = (user_info.get(key) or "").strip()
        if value:
            return value
    return ""


def _normalize_name(value) -> str:
    return (value or "").strip()


def _extract_first_last_name(user_info: dict) -> tuple[str, str]:
    first_name = ""
    last_name = ""

    for key in ("firstName", "first_name", "firstname", "givenName", "given_name", "givenname"):
        first_name = _normalize_name(user_info.get(key))
        if first_name:
            break

    for key in ("lastName", "last_name", "lastname", "surname", "familyName", "family_name", "familyname"):
        last_name = _normalize_name(user_info.get(key))
        if last_name:
            break

    name_value = _normalize_name(user_info.get("name"))
    if name_value and (not first_name and not last_name):
        parts = [part for part in name_value.split() if part]
        if len(parts) >= 2:
            last_name = parts[0]
            first_name = parts[1]
        else:
            first_name = name_value

    full_name = ""
    for key in ("fullName", "full_name", "fullname", "fio", "FIO", "displayName", "display_name"):
        full_name = _normalize_name(user_info.get(key))
        if full_name:
            break

    if full_name and (not first_name or not last_name):
        parts = [part for part in full_name.split() if part]
        if len(parts) >= 2:
            last_name = last_name or parts[0]
            first_name = first_name or parts[1]
        elif len(parts) == 1 and not first_name:
            first_name = parts[0]

    return first_name, last_name


def get_or_create_user_from_external(user_info: dict) -> tuple[User, bool]:
    """
    Get or create Django User from external DL API response.
    
    Args:
        user_info: Response from dl.gsu.by/restapi/get-user-info containing:
                   - userId (int) - required
                   - login (str) - optional
                   - other fields (courseID, nodeId, taskId, etc.)
    
    Returns:
        (user, created) tuple where created=True if user was newly created
    
    After extracting names from user_info, also calls
    /restapi/get-id-user-info?userId=<id> to enrich first/last name
    when the initial payload lacks them.
    """
    external_user_id = str(user_info.get('userId'))
    external_login = _extract_external_login(user_info)
    external_first_name, external_last_name = _extract_first_last_name(user_info)
    
    # Enrich from /restapi/get-id-user-info when names are missing.
    if external_user_id and not (external_first_name and external_last_name):
        try:
            from .dl_api_client import fetch_user_names
            names = fetch_user_names(external_user_id)
            if not external_first_name and names.get("first_name"):
                external_first_name = names["first_name"]
            if not external_last_name and names.get("last_name"):
                external_last_name = names["last_name"]
        except Exception as exc:
            logger.warning("fetch_user_names failed for userId=%s: %s", external_user_id, exc)
    
    if not external_user_id or external_user_id == "None":
        logger.error(f"Invalid user_info: userId={external_user_id}")
        raise ValueError("userId is required")
    
    user = None
    created = False
    
    # 1. Primary lookup by external userId.
    try:
        ext_account = ExternalDLAccount.objects.select_related('user').get(
            external_user_id=external_user_id
        )
        user = ext_account.user
        
        # Store latest external login if API provides it.
        updates = {}
        if external_login and ext_account.external_login != external_login:
            updates["external_login"] = external_login
        if external_first_name and ext_account.external_first_name != external_first_name:
            updates["external_first_name"] = external_first_name
        if external_last_name and ext_account.external_last_name != external_last_name:
            updates["external_last_name"] = external_last_name
        if updates:
            for key, value in updates.items():
                setattr(ext_account, key, value)
            ext_account.save(update_fields=list(updates.keys()))

        # Prefer external nickname as Django username when available.
        if external_login and user.username != external_login:
            candidate = external_login
            if User.objects.filter(username=candidate).exclude(pk=user.pk).exists():
                candidate = _find_available_username(external_login)
            if user.username != candidate:
                user.username = candidate
                user.save(update_fields=['username'])
    
    except ExternalDLAccount.DoesNotExist:
        # 2. Create new user with nickname if available, otherwise user_<userId>.
        base_username = external_login or f"user_{external_user_id}"
        username = _find_available_username(base_username)
        try:
            user = User.objects.create_user(
                username=username,
                email='',
            )
            user.set_unusable_password()
            user.save()
            created = True
            logger.info(f"Created new user: {user.username}")
        except IntegrityError as e:
            logger.error(f"Failed to create user with username {username}: {e}")
            raise
        
        # Link external account by external userId.
        try:
            ext_account, _ = ExternalDLAccount.objects.get_or_create(
                external_user_id=external_user_id,
                defaults={
                    'user': user,
                    'external_login': external_login,
                    'external_first_name': external_first_name,
                    'external_last_name': external_last_name,
                }
            )
        except IntegrityError as e:
            logger.error(f"Failed to create external account for user {user.username}: {e}")
            raise
    
    if user:
        user_updates = {}
        if external_first_name and user.first_name != external_first_name:
            user_updates["first_name"] = external_first_name
        if external_last_name and user.last_name != external_last_name:
            user_updates["last_name"] = external_last_name
        if user_updates:
            for key, value in user_updates.items():
                setattr(user, key, value)
            user.save(update_fields=list(user_updates.keys()))

    # Ensure user is in prompt_developer group
    try:
        from django.contrib.auth.models import Group
        group, _ = Group.objects.get_or_create(name=PROMPT_DEVELOPER_GROUP)
        user.groups.add(group)
    except Exception as e:
        logger.error(f"Failed to add user {user.username} to prompt_developer group: {e}")
        raise
    
    return user, created
