"""
External DL account model and utilities for user provisioning.
"""
from django.contrib.auth import get_user_model
from django.db import IntegrityError
from .models import ExternalDLAccount
import logging

logger = logging.getLogger(__name__)
User = get_user_model()

PROMPT_DEVELOPER_GROUP = "prompt_developer"


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
    """
    external_user_id = str(user_info.get('userId'))
    external_login = (user_info.get('login') or '').strip()
    
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
        if external_login and ext_account.external_login != external_login:
            ext_account.external_login = external_login
            ext_account.save(update_fields=['external_login'])
    
    except ExternalDLAccount.DoesNotExist:
        # 2. Create new user with deterministic username from userId.
        base_username = f"user_{external_user_id}"
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
                }
            )
        except IntegrityError as e:
            logger.error(f"Failed to create external account for user {user.username}: {e}")
            raise
    
    # Ensure user is in prompt_developer group
    try:
        from django.contrib.auth.models import Group
        group, _ = Group.objects.get_or_create(name=PROMPT_DEVELOPER_GROUP)
        user.groups.add(group)
    except Exception as e:
        logger.error(f"Failed to add user {user.username} to prompt_developer group: {e}")
        raise
    
    return user, created
