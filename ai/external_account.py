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
                   - userId (int)
                   - login (str)
                   - other fields (courseID, nodeId, taskId, etc.)
    
    Returns:
        (user, created) tuple where created=True if user was newly created
    """
    external_user_id = str(user_info.get('userId'))
    external_login = user_info.get('login', '').strip()
    
    if not external_user_id or not external_login:
        logger.error(f"Invalid user_info: userId={external_user_id}, login={external_login}")
        raise ValueError("userId and login are required")
    
    user = None
    created = False
    
    # 1. Try to find by ExternalDLAccount (if user already linked)
    try:
        ext_account = ExternalDLAccount.objects.select_related('user').get(
            external_user_id=external_user_id
        )
        user = ext_account.user
        
        # Update external_login if changed on DL side
        if ext_account.external_login != external_login:
            new_username = _find_available_username(external_login)
            
            # Migrate username only if it changed
            if user.username != new_username:
                try:
                    user.username = new_username
                    user.save(update_fields=['username'])
                    ext_account.external_login = external_login
                    ext_account.save(update_fields=['external_login'])
                    logger.info(f"Updated user {user.id}: username {user.username}")
                except IntegrityError as e:
                    logger.error(f"Failed to update username for user {user.id}: {e}")
                    raise
    
    except ExternalDLAccount.DoesNotExist:
        # 2. Try to find by username
        try:
            user = User.objects.get(username=external_login)
            logger.info(f"Found existing user by username: {user.username}")
        except User.DoesNotExist:
            # 3. Create new user
            username = _find_available_username(external_login)
            try:
                user = User.objects.create_user(
                    username=username,
                    email='',  # Will be set later if available
                )
                user.set_unusable_password()
                user.save()
                created = True
                logger.info(f"Created new user: {user.username}")
            except IntegrityError as e:
                logger.error(f"Failed to create user with username {username}: {e}")
                raise
        
        # Link external account
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
