"""WebSocket authentication service."""

from typing import Any

from asgiref.sync import sync_to_async

from ..external_auth import (
    ExternalAuthMisconfigured,
    ExternalAuthUnauthorized,
    ExternalAuthUnavailable,
    fetch_external_user_info,
    get_external_session_cookie_name,
)


class WebSocketAuthService:
    """Authenticate a WebSocket connection using the same DLSID flow as HTTP.

    The service first trusts a user already placed in ``scope["user"]`` by
    ``AuthMiddlewareStack``. If no authenticated user is present, it falls back to
    the ``DLSID`` cookie and validates it against the external DL API.
    """

    def __init__(self):
        self.session_cookie_name = get_external_session_cookie_name()

    async def authenticate(self, consumer) -> tuple[Any | None, dict | None]:
        """Return (user_or_none, user_info_or_none).

        ``user_or_none`` may be a Django User instance (if already authenticated
        via Channels auth stack) or a string user id extracted from the external
        API payload (for pure external users that have not been auto-provisioned
        yet).
        """
        user = self._get_scope_user(consumer.scope)
        if user is not None:
            if not await self._is_app_enabled():
                return None, None
            return user, consumer.scope.get("user_info")

        raw_session_id = consumer.scope.get("cookies", {}).get(self.session_cookie_name)
        if not raw_session_id:
            return None, None

        session_id = self._decode_session_id(raw_session_id)
        user_info = await self._resolve_user_info(consumer.scope, session_id)
        if not user_info:
            return None, None

        if not await self._is_app_enabled():
            return None, None

        return user_info.get("userId"), user_info

    def _get_scope_user(self, scope: dict) -> Any | None:
        from django.contrib.auth.models import AnonymousUser

        user = scope.get("user")
        if user is None or isinstance(user, AnonymousUser):
            return None
        if not getattr(user, "is_authenticated", False):
            return None
        return user

    def _decode_session_id(self, raw: str) -> str:
        from urllib.parse import unquote

        return unquote(raw)

    def get_session_id(self, scope: dict) -> str | None:
        """Return the decoded DL session id (DLSID) from the WS scope cookies.

        Used by the consumer to forward the caller's session to DL API helpers
        (e.g. task auto-registration) when no Django user session is available.
        """
        raw = scope.get("cookies", {}).get(self.session_cookie_name)
        if not raw:
            return None
        return self._decode_session_id(raw)

    async def _resolve_user_info(self, scope: dict, session_id: str) -> dict | None:
        session = scope.get("session")
        if session is not None:
            cached_session_id = session.get("external_session_id")
            cached_user_info = session.get("external_user_info")
            if cached_session_id == session_id and isinstance(cached_user_info, dict) and cached_user_info:
                return cached_user_info

        try:
            user_info = await sync_to_async(fetch_external_user_info)(session_id)
        except (ExternalAuthMisconfigured, ExternalAuthUnauthorized, ExternalAuthUnavailable):
            return None

        if session is not None and user_info:
            session["external_session_id"] = session_id
            session["external_user_info"] = user_info
            session.modified = True
            await sync_to_async(session.save)()

        return user_info

    @sync_to_async
    def _is_app_enabled(self) -> bool:
        from ..models import AIAppSettings
        return AIAppSettings.get_solo().is_enabled


@sync_to_async
def resolve_external_account(user: Any):
    """Return the ExternalDLAccount for a Django User, if any."""
    from ..models import ExternalDLAccount
    try:
        return user.external_dl_account
    except (ExternalDLAccount.DoesNotExist, AttributeError):
        return None


def get_user_identity_for_log(user: Any, user_info: dict | None, external_account=None) -> dict:
    """Return identity fields for AIRequestLog from a user or external info.

    ``external_account`` is the user's ``ExternalDLAccount`` instance if
    already resolved. Passing it avoids a synchronous DB query when this
    function is called from an async context.
    """
    result = {
        "user": None,
        "username": "",
        "external_user_id": "",
        "user_full_name": "",
    }
    if user is None:
        return result

    if isinstance(user, str):
        result["external_user_id"] = user
        result["username"] = user
        if user_info:
            first = (user_info.get("firstName") or "").strip()
            last = (user_info.get("lastName") or "").strip()
            result["user_full_name"] = f"{first} {last}".strip() or user
        return result

    if getattr(user, "is_authenticated", False):
        result["user"] = user
        result["username"] = getattr(user, "username", "") or ""
        result["user_full_name"] = (user.get_full_name() or "").strip() or result["username"]
        if external_account is not None:
            result["external_user_id"] = external_account.external_user_id
        else:
            try:
                from ..models import ExternalDLAccount
                result["external_user_id"] = user.external_dl_account.external_user_id
            except (ExternalDLAccount.DoesNotExist, AttributeError):
                result["external_user_id"] = result["username"]

    return result
