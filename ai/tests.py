# Тестовый файл: мок-атрибуты на request (``request.user_info = …``,
# ``request.user = SimpleNamespace(…)``) — штатная Django-идиома (атрибуты
# выставляет middleware/тест, тайп-чекер про них не знает). Глушим
# соответствующие проверки pyright/Pylance на весь файл.
# pyright: basic, reportAttributeAccessIssue=false, reportAssignmentIssue=false
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import SESSION_KEY, get_user_model
from django.contrib.auth.models import AnonymousUser
from django.contrib.auth.models import Group
from django.contrib.sessions.middleware import SessionMiddleware
from django.test import SimpleTestCase, RequestFactory, TestCase, override_settings
from pathlib import Path
import json
import time

from django.http import HttpResponse
from django.db import ProgrammingError
from django.utils import timezone
from asgiref.sync import sync_to_async
from unittest.mock import AsyncMock, MagicMock, patch
from types import SimpleNamespace

from ai.admin import PromptAdmin, PromptForm
from ai.middleware import ExternalAuthMiddleware
from ai.i18n import get_localized_name, get_ui_language_suffix
from ai.models import AIRequestLog, ExternalDLAccount, ProgrammingLanguage, Prompt, SharedPrompt, Topic, UpdateLog
from ai.services import (
    ConversationHistory,
    LogWriter,
    MessageComposer,
    ModelCaller,
    PromptResolver,
    get_user_identity_for_log,
)
from ai.throttling import RateLimiter, get_request_user_id, rate_limited
from ai.views import chat_view, get_problem_data, get_prompts, set_password_view
from ai.dl_api_client import _decode_response_json


class ChatViewTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def _chat_request(self, user=None):
        request = self.factory.get("/ai/chat/")
        request.user = user if user is not None else SimpleNamespace(
            is_authenticated=True, is_active=True, username="chat-user",
        )
        request.session = {}
        request.user_info = {"userId": "chat-user"}
        request.COOKIES = {"userId": "chat-user"}
        return request

    @patch("ai.views.AIAppSettings.get_solo", side_effect=ProgrammingError)
    @patch("ai.views.get_available_model_options", return_value=[])
    def test_chat_view_does_not_fail_when_ai_settings_table_missing(self, _mock_models, _mock_get_solo):
        request = self._chat_request()
        with patch("ai.views.render", return_value=HttpResponse("ok")):
            response = chat_view(request)
        self.assertEqual(response.status_code, 200)

    @patch("ai.views.AIAppSettings.get_solo", return_value=SimpleNamespace(is_enabled=False))
    @patch("ai.views.get_available_model_options", return_value=[])
    def test_chat_view_returns_404_when_ai_app_disabled(self, _mock_models, _mock_get_solo):
        request = self._chat_request()
        response = chat_view(request)
        self.assertEqual(response.status_code, 404)

    def test_chat_view_requires_auth_or_uid(self):
        request = self.factory.get("/ai/chat/")
        request.user = SimpleNamespace(is_authenticated=False)
        request.session = {}
        response = chat_view(request)
        self.assertEqual(response.status_code, 403)

    def test_chat_view_requires_matching_user_info(self):
        # No user_info at all → 403.
        request = self.factory.get("/ai/chat/")
        request.user = SimpleNamespace(is_authenticated=True, is_active=True, username="alice")
        request.session = {}
        request.user_info = None
        request.COOKIES = {}
        with patch("ai.views.AIAppSettings.get_solo", return_value=SimpleNamespace(is_enabled=True)), \
             patch("ai.views.get_available_model_options", return_value=[]):
            response = chat_view(request)
        self.assertEqual(response.status_code, 403)

    def test_chat_view_rejects_session_mismatch(self):
        # Authenticated Django session but no DLSID / DLID / uid at all
        # on the request → 403.
        request = self.factory.get("/ai/chat/")
        request.user = SimpleNamespace(is_authenticated=True, is_active=True, username="alice")
        request.session = {}
        request.user_info = None
        request.COOKIES = {}
        with patch("ai.views.AIAppSettings.get_solo", return_value=SimpleNamespace(is_enabled=True)), \
             patch("ai.views.get_available_model_options", return_value=[]):
            response = chat_view(request)
        self.assertEqual(response.status_code, 403)


@override_settings(SESSION_ENGINE="django.contrib.sessions.backends.signed_cookies")
class ExternalAuthMiddlewareTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.middleware = ExternalAuthMiddleware(lambda req: HttpResponse("ok"))
        self.user_model = get_user_model()

    def _add_session(self, request):
        SessionMiddleware(lambda req: None).process_request(request)

    def test_test_panel_login_no_longer_skips_external_auth(self):
        request = self.factory.get("/ai/test-panel/login/")
        self._add_session(request)

        response = self.middleware(request)

        # Middleware now treats /ai/test-panel/login/ as a regular path:
        # no DLSID, no user_info → 302 redirect to dl.gsu.by.
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "https://dl.gsu.by")

    def test_authenticated_user_without_dlsid_is_redirected(self):
        # A live Django session alone is NOT enough — the user must
        # also have a DLSID. Otherwise a stale session cookie from a
        # superuser would grant access under someone else's identity.
        request = self.factory.get("/ai/chat/")
        self._add_session(request)
        request.user = SimpleNamespace(is_authenticated=True, pk=1)

        response = self.middleware(request)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "https://dl.gsu.by")

    def test_session_is_rebound_when_dlsid_belongs_to_different_user(self):
        # Stale Django session belongs to pk=1 (e.g. a former superuser),
        # but the current DLSID authenticates user 42. Middleware must
        # rebind the session so the local user matches the external one.
        from django.contrib.auth.models import User
        from ai.constants import PROMPT_DEVELOPER_GROUP
        from ai.models import ExternalDLAccount

        stale = User.objects.create_user(
            username="stale-superuser",
            password="x",
            is_superuser=True,
            is_staff=True,
        )
        request = self.factory.get("/ai/chat/")
        self._add_session(request)
        request.user = stale
        request.COOKIES["DLSID"] = "session-123"
        # Seed the prompt_developer group and a fresh user so the real
        # provisioning path (not a mock) can run end-to-end.
        Group.objects.get_or_create(name=PROMPT_DEVELOPER_GROUP)
        fresh = User.objects.create_user(username="alice", password="x")
        ExternalDLAccount.objects.create(user=fresh, external_user_id="42")

        with patch(
            "ai.middleware.fetch_external_user_info",
            return_value={"userId": 42, "login": "alice", "firstName": "Alice"},
        ):
            response = self.middleware(request)

        self.assertEqual(response.status_code, 200)
        # After middleware the request must be bound to the fresh user,
        # not the stale superuser.
        self.assertEqual(request.user.pk, fresh.pk)
        self.assertEqual(request.user.username, "alice")

    def test_cached_user_info_skips_external_call(self):
        # Even for admin paths the middleware now provisions a local
        # user, so seed the prompt_developer group and the user.
        from ai.constants import PROMPT_DEVELOPER_GROUP
        from ai.models import ExternalDLAccount
        Group.objects.get_or_create(name=PROMPT_DEVELOPER_GROUP)
        u = self.user_model.objects.create_user(username="user_42", password="x")
        ExternalDLAccount.objects.create(user=u, external_user_id="42")

        request = self.factory.get("/ai/admin/")
        self._add_session(request)
        request.COOKIES["DLSID"] = "session-123"
        request.session["external_session_id"] = "session-123"
        request.session["external_user_info"] = {"userId": "42"}
        # Свежий кэш (< TTL) — ревалидация DLSID не требуется.
        request.session["external_user_info_fetched_at"] = time.time()

        with patch("ai.middleware.fetch_external_user_info") as fetch_user_info:
            response = self.middleware(request)

        self.assertEqual(response.status_code, 200)
        fetch_user_info.assert_not_called()
        self.assertEqual(request.user_info, {"userId": "42"})

    def test_stale_cached_user_info_revalidates_dlsid(self):
        # Кэш старше TTL → middleware заново проверяет DLSID через внешний API.
        from ai.constants import PROMPT_DEVELOPER_GROUP
        from ai.models import ExternalDLAccount
        Group.objects.get_or_create(name=PROMPT_DEVELOPER_GROUP)
        u = self.user_model.objects.create_user(username="user_42b", password="x")
        ExternalDLAccount.objects.create(user=u, external_user_id="42")

        request = self.factory.get("/ai/admin/")
        self._add_session(request)
        request.COOKIES["DLSID"] = "session-123"
        request.session["external_session_id"] = "session-123"
        request.session["external_user_info"] = {"userId": "42"}
        # Протухший кэш: fetched_at далеко в прошлом → (now - fetched_at) >= TTL.
        request.session["external_user_info_fetched_at"] = time.time() - 3600

        with patch(
            "ai.middleware.fetch_external_user_info",
            return_value={"userId": "42", "login": "alice"},
        ) as fetch_user_info:
            response = self.middleware(request)

        self.assertEqual(response.status_code, 200)
        fetch_user_info.assert_called_once()
        # Кэш обновлён свежим user_info и свежим timestamp.
        self.assertEqual(request.session["external_user_info"], {"userId": "42", "login": "alice"})

    def test_unavailable_external_api_falls_back_to_stale_cache(self):
        # dl.gsu.by недоступен, но есть свежий кэш → graceful degradation,
        # доступ сохраняется (не 503).
        from ai.constants import PROMPT_DEVELOPER_GROUP
        from ai.models import ExternalDLAccount
        from ai.external_auth import ExternalAuthUnavailable
        Group.objects.get_or_create(name=PROMPT_DEVELOPER_GROUP)
        u = self.user_model.objects.create_user(username="user_42c", password="x")
        ExternalDLAccount.objects.create(user=u, external_user_id="42")

        request = self.factory.get("/ai/admin/")
        self._add_session(request)
        request.COOKIES["DLSID"] = "session-123"
        request.session["external_session_id"] = "session-123"
        request.session["external_user_info"] = {"userId": "42"}
        # Протухший кэш провоцирует ревалидацию, которая упадёт с Unavailable.
        request.session["external_user_info_fetched_at"] = time.time() - 3600

        with patch(
            "ai.middleware.fetch_external_user_info",
            side_effect=ExternalAuthUnavailable("dl.gsu.by down"),
        ):
            response = self.middleware(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(request.user_info, {"userId": "42"})

    def test_unavailable_external_api_returns_503_without_cache(self):
        # dl.gsu.by недоступен И кэша нет → 503 (некому предоставить доступ).
        from ai.external_auth import ExternalAuthUnavailable

        request = self.factory.get("/ai/chat/")
        self._add_session(request)
        request.COOKIES["DLSID"] = "session-123"

        with patch(
            "ai.middleware.fetch_external_user_info",
            side_effect=ExternalAuthUnavailable("dl.gsu.by down"),
        ):
            response = self.middleware(request)

        self.assertEqual(response.status_code, 503)


class AdminExternalAuthTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user_model = get_user_model()

    def _request(self, method="get", path="/ai/admin/", data=None, user_id="12345"):
        request = getattr(self.factory, method)(path, data=data or {})
        SessionMiddleware(lambda req: None).process_request(request)
        request.user = AnonymousUser()
        request.user_info = {"userId": user_id}
        return request

    def test_has_permission_rejects_anonymous(self):
        from ai.admin.site import ai_admin_site
        request = self._request()
        self.assertFalse(ai_admin_site.has_permission(request))

    def test_has_permission_rejects_session_user_mismatch(self):
        from ai.admin.site import ai_admin_site
        user = self.user_model.objects.create_user(
            username="other-user",
            password="initial-pass",
        )
        # Request has no user_info, no uid, no DLID cookie — i.e. the
        # DLSID chain is broken. has_permission must refuse, regardless
        # of the local session.
        request = self._request(user_id=None)
        request.user = user
        self.assertFalse(ai_admin_site.has_permission(request))

    def test_has_permission_rejects_stale_session_under_other_dlsid(self):
        # The local session is bound to user 99, but the DLSID chain
        # on the current request authenticates user 12345. This is
        # exactly the cross-account bug: a stale superuser session
        # must NOT grant access on someone else's DLSID.
        from ai.admin.site import ai_admin_site
        from ai.constants import PROMPT_DEVELOPER_GROUP
        from django.contrib.auth.models import Group
        group, _ = Group.objects.get_or_create(name=PROMPT_DEVELOPER_GROUP)
        stale = self.user_model.objects.create_user(
            username="99",
            password="initial-pass",
            is_superuser=True,
            is_staff=True,
        )
        legit = self.user_model.objects.create_user(username="12345", password="initial-pass")
        legit.groups.add(group)
        request = self._request(user_id="12345")
        request.user = stale
        self.assertFalse(ai_admin_site.has_permission(request))

    def test_has_permission_accepts_matching_prompt_developer(self):
        from ai.admin.site import ai_admin_site
        from django.contrib.auth.models import Group
        from ai.constants import PROMPT_DEVELOPER_GROUP
        from ai.models import ExternalDLAccount
        group, _ = Group.objects.get_or_create(name=PROMPT_DEVELOPER_GROUP)
        user = self.user_model.objects.create_user(
            username="12345",
            password="initial-pass",
        )
        user.groups.add(group)
        ExternalDLAccount.objects.create(user=user, external_user_id="12345")
        request = self._request(user_id="12345")
        request.user = user
        self.assertTrue(ai_admin_site.has_permission(request))

    def test_get_admin_user_uses_external_account_not_username(self):
        """A username matching the external id must not win over ExternalDLAccount."""
        from ai.auth_backends import get_admin_user_by_external_id
        from ai.models import ExternalDLAccount

        # A colliding username with a password: the old lookup returned this
        # user and caused an admin <-> set-password redirect loop.
        self.user_model.objects.create_user(username="186638", password="pass-123")
        # The real user mapped by the external API.
        real_user = self.user_model.objects.create_user(username="real-186638")
        ExternalDLAccount.objects.create(user=real_user, external_user_id="186638")

        resolved = get_admin_user_by_external_id("186638")
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.pk, real_user.pk)

    def test_new_external_user_sets_password_once_and_is_created(self):
        request = self._request(
            method="post",
            path="/ai/admin/set-password/",
            data={
                "next": "/ai/admin/",
                "new_password": "strong-pass-123",
                "new_password_confirm": "strong-pass-123",
            },
            user_id="67890",
        )

        response = set_password_view(request)

        user = self.user_model.objects.get(username="67890")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/ai/admin/")
        self.assertTrue(user.check_password("strong-pass-123"))
        self.assertEqual(request.session[SESSION_KEY], str(user.pk))

    def test_admin_external_users_are_assigned_prompt_developer_group(self):
        request = self._request(
            method="post",
            path="/ai/admin/set-password/",
            data={
                "next": "/ai/admin/",
                "new_password": "strong-pass-123",
                "new_password_confirm": "strong-pass-123",
            },
            user_id="24680",
        )

        set_password_view(request)

        group = Group.objects.get(name="prompt_developer")
        user = self.user_model.objects.get(username="24680")
        self.assertTrue(user.groups.filter(pk=group.pk).exists())

    def test_mapped_external_user_without_password_sets_password_on_existing_user(self):
        user = self.user_model.objects.create_user(username="external-login")
        user.set_unusable_password()
        user.save(update_fields=["password"])
        ExternalDLAccount.objects.create(
            user=user,
            external_user_id="13579",
            external_login="external-login",
        )
        request = self._request(
            method="post",
            path="/ai/admin/set-password/",
            data={
                "next": "/ai/admin/ai/prompt/add/",
                "new_password": "strong-pass-123",
                "new_password_confirm": "strong-pass-123",
            },
            user_id="13579",
        )

        response = set_password_view(request)
        user.refresh_from_db()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/ai/admin/ai/prompt/add/")
        self.assertTrue(user.check_password("strong-pass-123"))
        self.assertEqual(self.user_model.objects.filter(username="13579").count(), 0)


class AdminPermissionsTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user_model = get_user_model()
        from django.contrib.auth.models import Group
        from ai.constants import PROMPT_DEVELOPER_GROUP
        self.pd_group, _ = Group.objects.get_or_create(name=PROMPT_DEVELOPER_GROUP)

    def test_prompt_developer_cannot_view_shared_prompt_module(self):
        from ai.admin.models import SharedPromptAdmin
        from django.contrib.admin.sites import AdminSite
        user = self.user_model.objects.create_user(username="alice", password="x")
        user.groups.add(self.pd_group)
        request = self.factory.get("/ai/admin/ai/sharedprompt/")
        request.user = user
        admin = SharedPromptAdmin(SharedPrompt, AdminSite())
        self.assertFalse(admin.has_module_permission(request))

    def test_staff_user_can_view_shared_prompt_module(self):
        from ai.admin.models import SharedPromptAdmin
        from django.contrib.admin.sites import AdminSite
        user = self.user_model.objects.create_user(
            username="bob", password="x", is_staff=True,
        )
        request = self.factory.get("/ai/admin/ai/sharedprompt/")
        request.user = user
        admin = SharedPromptAdmin(SharedPrompt, AdminSite())
        self.assertTrue(admin.has_module_permission(request))

    def test_app_list_hides_staff_only_models_for_prompt_developer(self):
        from ai.admin.site import ai_admin_site
        from ai.admin.permissions import filter_app_list_for_user
        from ai.models import Prompt as PromptModel, SharedPrompt as SharedPromptModel
        from django.contrib.auth import get_user_model as get_user
        User = get_user()
        user = self.user_model.objects.create_user(username="carol", password="x")
        user.groups.add(self.pd_group)
        request = self.factory.get("/ai/admin/")
        request.user = user
        request._ai_admin_registry = ai_admin_site._registry
        app_list = [
            {
                "app_label": "ai",
                "name": "AI",
                "app_url": "/ai/admin/ai/",
                "models": [
                    {"object_name": "Prompt", "name": "Prompt",
                     "admin_url": "/ai/admin/ai/prompt/", "add_url": "",
                     "view_only": False, "_model_cls": PromptModel},
                    {"object_name": "SharedPrompt", "name": "SharedPrompt",
                     "admin_url": "/ai/admin/ai/sharedprompt/", "add_url": "",
                     "view_only": False, "_model_cls": SharedPromptModel},
                ],
            },
            {
                "app_label": "auth",
                "name": "Auth",
                "app_url": "/ai/admin/auth/",
                "models": [
                    {"object_name": "User", "name": "User",
                     "admin_url": "/ai/admin/auth/user/", "add_url": "",
                     "view_only": False, "_model_cls": User},
                ],
            },
        ]
        filtered = filter_app_list_for_user(app_list, request)
        # Non-AI app (auth) without a custom link for this user must be dropped.
        labels = [app["app_label"] for app in filtered]
        self.assertNotIn("auth", labels)
        # The AI app must keep Prompt, drop SharedPrompt.
        ai_app = next(app for app in filtered if app["app_label"] == "ai")
        names = [m["object_name"] for m in ai_app["models"]]
        self.assertIn("Prompt", names)
        self.assertNotIn("SharedPrompt", names)


class PromptAdminAccessTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.prompt_admin = PromptAdmin(Prompt, AdminSite())
        user_model = get_user_model()

        self.staff_user = user_model.objects.create_user(
            username="staff_user",
            password="test-pass",
            is_staff=True,
        )
        self.prompt_developer = user_model.objects.create_user(
            username="prompt_dev",
            password="test-pass",
        )
        self.second_prompt_developer = user_model.objects.create_user(
            username="prompt_dev_second",
            password="test-pass",
        )
        self.other_prompt_developer = user_model.objects.create_user(
            username="prompt_dev_other",
            password="test-pass",
        )

        prompt_developer_group, _ = Group.objects.get_or_create(name="prompt_developer")
        self.prompt_developer.groups.add(prompt_developer_group)
        self.second_prompt_developer.groups.add(prompt_developer_group)
        self.other_prompt_developer.groups.add(prompt_developer_group)

        self.editable_prompt = Prompt.objects.create(
            prompt_name="Editable prompt",
            prompt_text="Editable prompt text",
            owner=self.prompt_developer,
        )
        self.readonly_prompt = Prompt.objects.create(
            prompt_name="Readonly prompt",
            prompt_text="Readonly prompt text",
            owner=self.other_prompt_developer,
        )
        self.legacy_assigned_prompt = Prompt.objects.create(
            prompt_name="Legacy assigned prompt",
            prompt_text="Legacy assigned prompt text",
        )
        self.editable_prompt.editors.add(self.prompt_developer)
        self.legacy_assigned_prompt.editors.add(self.prompt_developer)

    def _build_request(self, user, query_params=None):
        query_params = query_params or {}
        request = self.factory.get("/ai/admin/ai/prompt/", data=query_params)
        request.user = user
        request.user_info = {"userId": user.username}
        request.COOKIES = {"userId": user.username}
        return request

    def test_prompt_developer_can_edit_owned_or_assigned_prompt(self):
        request = self._build_request(self.prompt_developer)

        self.assertTrue(self.prompt_admin.has_change_permission(request, self.editable_prompt))
        self.assertTrue(self.prompt_admin.has_change_permission(request, self.legacy_assigned_prompt))
        self.assertFalse(self.prompt_admin.has_change_permission(request, self.readonly_prompt))
        self.assertTrue(self.prompt_admin.has_view_permission(request, self.readonly_prompt))
        self.assertTrue(self.prompt_admin.has_delete_permission(request, self.editable_prompt))
        self.assertFalse(self.prompt_admin.has_delete_permission(request, self.readonly_prompt))

    def test_prompt_developer_can_add_prompt_and_becomes_owner(self):
        request = self._build_request(self.prompt_developer)
        self.assertTrue(self.prompt_admin.has_add_permission(request))

        new_prompt = Prompt(prompt_name="My prompt", prompt_text="My prompt text")
        self.prompt_admin.save_model(request, new_prompt, form=None, change=False)
        new_prompt.refresh_from_db()

        self.assertEqual(new_prompt.owner_id, self.prompt_developer.id)
        self.assertTrue(new_prompt.editors.filter(pk=self.prompt_developer.pk).exists())

    def test_second_prompt_developer_can_add_and_own_prompt(self):
        request = self._build_request(self.second_prompt_developer)
        self.assertTrue(self.prompt_admin.has_add_permission(request))

        prompt = Prompt(prompt_name="Second prompt", prompt_text="Second prompt text")
        self.prompt_admin.save_model(request, prompt, form=None, change=False)
        prompt.refresh_from_db()

        self.assertEqual(prompt.owner_id, self.second_prompt_developer.id)
        self.assertTrue(prompt.editors.filter(pk=self.second_prompt_developer.pk).exists())

    def test_prompt_developer_fields_are_readonly_for_foreign_prompt(self):
        request = self._build_request(self.prompt_developer)

        readonly_fields = self.prompt_admin.get_readonly_fields(request, self.readonly_prompt)
        editable_fields = self.prompt_admin.get_readonly_fields(request, self.editable_prompt)

        self.assertEqual(editable_fields, ())
        self.assertEqual(
            readonly_fields,
            (
                "programming_language", "topic",
                "prompt_name", "prompt_name_ru", "prompt_name_en", "prompt_name_fr",
                "shared_prompt", "prompt_text_override",
                "prompt_text", "prompt_text_ru", "prompt_text_en", "prompt_text_fr",
            ),
        )

    def test_prompt_developer_queryset_shows_all_prompts(self):
        request = self._build_request(self.prompt_developer)
        prompt_ids = set(self.prompt_admin.get_queryset(request).values_list("id", flat=True))

        self.assertEqual(
            prompt_ids,
            {
                self.editable_prompt.id,
                self.readonly_prompt.id,
                self.legacy_assigned_prompt.id,
            },
        )

    def test_prompt_developer_queryset_can_filter_mine(self):
        request = self._build_request(self.prompt_developer, query_params={"mine": "1"})
        prompt_ids = set(self.prompt_admin.get_queryset(request).values_list("id", flat=True))

        self.assertEqual(prompt_ids, {self.editable_prompt.id, self.legacy_assigned_prompt.id})

    def test_staff_user_sees_only_own_prompts(self):
        request = self._build_request(self.staff_user)

        self.assertTrue(self.prompt_admin.has_add_permission(request))
        self.assertFalse(self.prompt_admin.has_change_permission(request, self.readonly_prompt))
        self.assertTrue(self.prompt_admin.has_view_permission(request, self.readonly_prompt))

    def test_get_prompts_api_returns_only_current_user_prompts(self):
        request = self._build_request(self.prompt_developer)
        response = get_prompts(request)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Editable prompt")
        self.assertContains(response, "Readonly prompt")
        self.assertContains(response, "Legacy assigned prompt")

    def test_superuser_queryset_shows_all_prompts(self):
        superuser = get_user_model().objects.create_superuser(
            username="super_user",
            password="test-pass",
        )
        own_prompt = Prompt.objects.create(
            prompt_name="Superuser prompt",
            prompt_text="Superuser prompt text",
            owner=superuser,
        )
        request = self._build_request(superuser)
        prompt_ids = set(self.prompt_admin.get_queryset(request).values_list("id", flat=True))

        self.assertEqual(
            prompt_ids,
            {
                self.editable_prompt.id,
                self.readonly_prompt.id,
                self.legacy_assigned_prompt.id,
                own_prompt.id,
            },
        )
        self.assertTrue(self.prompt_admin.has_view_permission(request, self.readonly_prompt))
        self.assertTrue(self.prompt_admin.has_change_permission(request, self.readonly_prompt))
        self.assertTrue(self.prompt_admin.has_change_permission(request, own_prompt))
        self.assertTrue(self.prompt_admin.has_delete_permission(request, self.readonly_prompt))
        self.assertTrue(self.prompt_admin.has_delete_permission(request, own_prompt))
        self.assertEqual(self.prompt_admin.get_readonly_fields(request, self.readonly_prompt), ())

    def test_superuser_queryset_can_filter_mine(self):
        superuser = get_user_model().objects.create_superuser(
            username="super_user_mine",
            password="test-pass",
        )
        own_prompt = Prompt.objects.create(
            prompt_name="Superuser prompt mine",
            prompt_text="Superuser prompt text",
            owner=superuser,
        )
        request = self._build_request(superuser, query_params={"mine": "1"})
        # The "mine" filter is intended for prompt developers; superusers bypass
        # it and continue to see all prompts.
        prompt_ids = set(self.prompt_admin.get_queryset(request).values_list("id", flat=True))

        self.assertEqual(
            prompt_ids,
            {
                self.editable_prompt.id,
                self.readonly_prompt.id,
                self.legacy_assigned_prompt.id,
                own_prompt.id,
            },
        )


class PromptFormTests(TestCase):
    def setUp(self):
        self.python_language = ProgrammingLanguage.objects.create(language_name="Python")
        self.c_language = ProgrammingLanguage.objects.create(language_name="C")
        self.python_topic = Topic.objects.create(
            topic_name="Loops",
            programming_language=self.python_language,
        )
        self.c_topic = Topic.objects.create(
            topic_name="Pointers",
            programming_language=self.c_language,
        )

    def test_form_sets_programming_language_from_prompt_topic(self):
        prompt = Prompt.objects.create(
            topic=self.python_topic,
            prompt_name="Prompt",
            prompt_text="Body",
        )

        form = PromptForm(instance=prompt)

        self.assertEqual(form.fields["programming_language"].initial, self.python_language.id)
        self.assertQuerySetEqual(
            form.fields["topic"].queryset,
            [self.python_topic],
            transform=lambda item: item,
        )

    def test_form_filters_topics_by_selected_language(self):
        form = PromptForm(
            data={
                "programming_language": str(self.python_language.id),
                "topic": str(self.python_topic.id),
                "prompt_name": "Prompt",
                "prompt_text": "Body",
            }
        )

        self.assertTrue(form.is_valid())
        self.assertQuerySetEqual(
            form.fields["topic"].queryset,
            [self.python_topic],
            transform=lambda item: item,
        )

    def test_form_validates_topic_belongs_to_selected_language(self):
        form = PromptForm(
            data={
                "programming_language": str(self.python_language.id),
                "topic": str(self.c_topic.id),
                "prompt_name": "Prompt",
                "prompt_text": "Body",
            }
        )

        self.assertFalse(form.is_valid())
        self.assertIn("topic", form.errors)


class LocalizationHelpersTests(TestCase):
    def test_ui_language_suffix_mapping(self):
        self.assertEqual(get_ui_language_suffix("Русский"), "ru")
        self.assertEqual(get_ui_language_suffix("English"), "en")
        self.assertEqual(get_ui_language_suffix("Français"), "fr")
        self.assertEqual(get_ui_language_suffix("Russian"), "ru")

    def test_get_localized_name_falls_back(self):
        topic = Topic(topic_name="Base", topic_name_ru="Рус", topic_name_en="Eng", topic_name_fr="Fra")
        self.assertEqual(get_localized_name(topic, "Русский", "topic_name"), "Рус")
        self.assertEqual(get_localized_name(topic, "English", "topic_name"), "Eng")
        self.assertEqual(get_localized_name(topic, "Français", "topic_name"), "Fra")
        self.assertEqual(get_localized_name(topic, "Unknown", "topic_name"), "Рус")


class PromptEffectiveTextTests(TestCase):
    def setUp(self):
        self.pl = ProgrammingLanguage.objects.create(language_name="Python")

    def test_effective_text_uses_ui_language(self):
        prompt = Prompt(
            prompt_name="P",
            prompt_text="Base {language}",
            prompt_text_ru="Рус {language}",
            prompt_text_en="Eng {language}",
        )
        self.assertEqual(prompt.get_effective_text("Русский", "Python"), "Рус Python")
        self.assertEqual(prompt.get_effective_text("English", "Python"), "Eng Python")

    def test_shared_prompt_text_uses_ui_language(self):
        shared = SharedPrompt(prompt_name="S", prompt_text="Base {language}", prompt_text_ru="Рус {language}")
        prompt = Prompt(prompt_name="P", shared_prompt=shared)
        self.assertEqual(prompt.get_effective_text("Русский", "C++"), "Рус C++")
        self.assertEqual(prompt.get_effective_text("English", "C++"), "Base C++")


class ProblemDataApiUiLanguageTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user = get_user_model().objects.create_user(username="api_user", password="test-pass")

    def _request(self, ui_language=""):
        params = {}
        if ui_language:
            params["ui_language"] = ui_language
        request = self.factory.get("/ai/api/problem-data/", data=params)
        request.user = self.user
        request.user_info = {"userId": self.user.username}
        request.COOKIES = {"userId": self.user.username}
        return request

    def test_problem_data_localizes_topic_and_prompt_names(self):
        pl = ProgrammingLanguage.objects.create(language_name="Python")
        topic = Topic.objects.create(
            topic_name="Base topic",
            topic_name_ru="Русская тема",
            topic_name_en="English topic",
            programming_language=pl,
        )
        Prompt.objects.create(
            topic=topic,
            prompt_name="Base prompt",
            prompt_name_ru="Русский промпт",
            prompt_name_en="English prompt",
            prompt_text="text",
        )

        response_en = get_problem_data(self._request("English"))
        data_en = json.loads(response_en.content)
        self.assertEqual(data_en["topics"][0]["name"], "English topic")
        self.assertEqual(data_en["prompts"][0]["name"], "English prompt")

        response_ru = get_problem_data(self._request("Русский"))
        data_ru = json.loads(response_ru.content)
        self.assertEqual(data_ru["topics"][0]["name"], "Русская тема")
        self.assertEqual(data_ru["prompts"][0]["name"], "Русский промпт")

    def test_prompt_name_falls_back_to_russian_when_en_missing(self):
        """When an admin has not filled prompt_name_en, the English UI must
        fall back to the Russian/base name (not disappear). This is the data
        situation behind 'preprompt names don't translate to English' — the
        localization pipeline is correct; the English names simply need to be
        entered in the admin (prompt_name_en field)."""
        pl = ProgrammingLanguage.objects.create(language_name="Python")
        topic = Topic.objects.create(
            topic_name="Base topic",
            topic_name_en="English topic",
            programming_language=pl,
        )
        Prompt.objects.create(
            topic=topic,
            prompt_name="Русский оригинал",
            prompt_name_en="",  # no English translation entered
            prompt_text="text",
        )

        data_en = json.loads(get_problem_data(self._request("English")).content)
        # English name missing -> falls back to the base (Russian) name.
        self.assertEqual(data_en["prompts"][0]["name"], "Русский оригинал")


class AIRequestLogModelTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="log_user",
            password="test-pass",
            first_name="Log",
            last_name="User",
        )

    def test_create_log_from_websocket(self):
        log = AIRequestLog.objects.create(
            user=self.user,
            username=self.user.username,
            external_user_id="42",
            user_full_name=self.user.get_full_name(),
            client_id="client-1",
            source=AIRequestLog.SOURCE_WEBSOCKET,
            mode=AIRequestLog.MODE_CHAT,
            sent_at=timezone.now(),
            model_names=["DeepSeek-R1"],
            message="hello",
            programming_language_id=1,
            programming_language_name="Python",
            topic_id=2,
            topic_name="Loops",
            prompt_id=3,
            prompt_name="Helper",
        )
        log.refresh_from_db()
        self.assertEqual(log.user_full_name, "Log User")
        self.assertEqual(log.model_names, ["DeepSeek-R1"])
        # Миграция 0029: дефолт статуса — error. Запись лога создаётся в момент
        # отправки запроса (LogWriter.create) без status и считается ошибкой,
        # пока update_success не закроет её успехом при непустом ответе модели.
        # Здесь create() без update_* — статус остаётся дефолтным (error).
        self.assertEqual(log.status, AIRequestLog.STATUS_ERROR)
        self.assertEqual(log.mode, AIRequestLog.MODE_CHAT)
        self.assertEqual(log.get_mode_display(), "Чат")
        self.assertEqual(log.programming_language_name, "Python")
        self.assertEqual(log.topic_name, "Loops")
        self.assertEqual(log.prompt_name, "Helper")


class ModelClientRegistryTests(SimpleTestCase):
    def test_registry_contains_expected_models(self):
        from ai.model_clients import registry

        # Default (always-on) providers: Web DeepSeek pool.
        for key in ("Web_DeepSeek", "Web_DeepSeek_Thinking"):
            self.assertIsNotNone(registry.get(key), f"Missing registry entry for {key}")
            self.assertTrue(callable(registry.handler(key)))

    def test_sambanova_models_complete(self):
        # SambaNova is gated behind AI_ENABLE_SAMBANOVA (off by default at import
        # time), so the global registry won't contain it — verify the builder
        # function returns all 10 declared models via a local registry instance.
        from ai.model_clients.registry import _sambanova_models, ModelRegistry

        samba = ModelRegistry(_sambanova_models())
        expected = {
            "DeepSeek_R1_Distill_Llama_70B", "DeepSeek_V3_1", "DeepSeek_V3_1_cb",
            "DeepSeek_V3_2", "Llama_4_Maverick_17B_128E_Instruct",
            "Meta_Llama_3_3_70B_Instruct", "MiniMax_M2_5", "MiniMax_M2_7",
            "Gemma_3_12b_it", "Gpt_oss_120b",
        }
        self.assertEqual(set(samba.keys()), expected)
        for key in expected:
            self.assertIsNotNone(samba.get(key), f"Missing sambanova entry for {key}")
            self.assertTrue(callable(samba.handler(key)))

    def test_model_caller_resolves_backward_compatible_aliases(self):
        # Legacy aliases live in ModelCaller._resolve_legacy_alias, NOT in the
        # registry (see CLAUDE.md: «legacy aliases resolved in ModelCaller»).
        from ai.services.model_caller import _resolve_legacy_alias

        self.assertEqual(_resolve_legacy_alias("DeepSeek_R1"), "DeepSeek_R1_Distill_Llama_70B")
        self.assertEqual(_resolve_legacy_alias("Meta_Llama_3_1_70B_Instruct"), "Meta_Llama_3_3_70B_Instruct")
        self.assertEqual(_resolve_legacy_alias("Mixtral_8x22b"), "Llama_4_Maverick_17B_128E_Instruct")


class DLApiClientEncodingTests(SimpleTestCase):
    """Unit tests for DL API response decoding and mojibake repair."""

    def _decode(self, text: str, encoding: str = "utf-8") -> dict:
        class FakeResponse:
            content = text.encode(encoding)

        return _decode_response_json(FakeResponse())

    def test_repairs_cp866_bytes_presented_as_cp1251(self):
        garbled = (
            "XXVII Љ®¬­¤­л© зҐ¬ЇЁ®­­в иЄ®«м­­ЁЄ®ў "
            "‘­Єв-ЏҐўҐаЎгаЈ Ї® Їа®Ја¬¬Ёа®ў­Ё®"
        )
        payload = json.dumps({"statement": garbled}, ensure_ascii=False)
        result = self._decode(payload)
        repaired = result["statement"]

        self.assertNotEqual(repaired, garbled)
        cyrillic = sum(1 for c in repaired if "Ѐ" <= c <= "ӿ")
        self.assertGreater(cyrillic, 30)
        self.assertIn("XXVII", repaired)
        self.assertTrue(repaired.startswith("XXVII "))

    def test_does_not_corrupt_valid_utf8_cyrillic(self):
        normal = (
            "XXVII Командный чемпионат школьников "
            "Санкт-Петербург по программированию"
        )
        payload = json.dumps({"statement": normal}, ensure_ascii=False)
        result = self._decode(payload)
        self.assertEqual(result["statement"], normal)

    def test_leaves_ascii_text_unchanged(self):
        payload = json.dumps({"statement": "Hello, world!"}, ensure_ascii=False)
        result = self._decode(payload)
        self.assertEqual(result["statement"], "Hello, world!")

    def test_preserves_utf8_nbsp_spaces_in_english_statement(self):
        """English statements that use non-breaking spaces (U+00A0) for spacing
        must not be turned into the '┬а' mojibake.

        nbsp is UTF-8 bytes [0xC2, 0xA0]; the cp1251 candidate decodes those as
        Cyrillic В + nbsp and used to win scoring (В gets the +2 Cyrillic
        bonus), after which the CP866 repair converted В+nbsp into '┬а'.
        """
        statement = "International\xa0Olympiad\xa0in\xa0Informatics"
        payload = json.dumps({"statement": statement}, ensure_ascii=False)
        result = self._decode(payload)
        self.assertNotIn("┬а", result["statement"])
        self.assertNotIn("В\xa0", result["statement"])
        self.assertIn("International", result["statement"])
        self.assertIn("Olympiad", result["statement"])

    def test_repairs_mixed_cp866_payload_with_replacement_chars(self):
        """A payload with U+FFFD mixed with cp1251 codepoints must not crash.

        Some PDF-derived responses contain Unicode replacement characters
        alongside the CP866-via-cp1251 mojibake. The repair must drop the
        replacement chars and still recover readable Cyrillic.
        """
        # "Привет мир!" as CP866 bytes interpreted as cp1251 codepoints,
        # plus a trailing U+FFFD.
        garbled = "ЏаЁўҐв ¬Ёа!" + chr(0xFFFD)
        payload = json.dumps({"statement": garbled}, ensure_ascii=False)
        result = self._decode(payload)
        # The result should contain Cyrillic and no replacement characters.
        self.assertNotIn(chr(0xFFFD), result["statement"])
        self.assertIn("Привет", result["statement"])
        self.assertIn("мир", result["statement"])

    def test_repairs_real_curl_captured_payload(self):
        """The captured /restapi/get-task-info payload decodes correctly."""
        curl_path = Path(__file__).resolve().parent.parent / "curl.txt"
        if not curl_path.exists():
            self.skipTest("curl.txt fixture not found")

        raw = curl_path.read_bytes()
        # The first line of the file is the JSON response.
        json_bytes = raw.split(b"\n")[0]

        class FakeResponse:
            content = json_bytes

        result = _decode_response_json(FakeResponse())
        self.assertEqual(result["taskId"], 221905)
        self.assertEqual(result["name"], "Прибытие короля")
        self.assertIn(
            "XXVII Командный чемпионат школьников Санкт-Петербурга",
            result["statement"],
        )
        # The statement is long and should contain plenty of Cyrillic.
        cyrillic = sum(1 for c in result["statement"] if "Ѐ" <= c <= "ӿ")
        self.assertGreater(cyrillic, 1000)

    def test_non_json_response_error_includes_status_and_snippet(self):
        """When DL returns a 2xx with a non-JSON body (HTML error page, empty
        body, login redirect, …), the error must carry the HTTP status and a
        body snippet so the operator can diagnose it — not a bare «некорректный
        JSON»."""
        from ai.dl_api_client import DLServerError, _decode_response_json

        class FakeResponse:
            status_code = 200
            content = b"<!DOCTYPE html><html><body>413 Request Entity Too Large</body></html>"

        with self.assertRaises(DLServerError) as cm:
            _decode_response_json(FakeResponse())
        msg = str(cm.exception)
        self.assertIn("код 200", msg)
        self.assertIn("413 Request Entity Too Large", msg)

    def test_non_json_empty_response_error_notes_empty_body(self):
        from ai.dl_api_client import DLServerError, _decode_response_json

        class FakeResponse:
            status_code = 200
            content = b""

        with self.assertRaises(DLServerError) as cm:
            _decode_response_json(FakeResponse())
        self.assertIn("пустой ответ", str(cm.exception))

    def test_raise_for_status_handles_generic_4xx_with_body(self):
        """Unmapped 4xx (400/405/413/422) must raise a typed error with the body
        snippet instead of falling through to JSON parsing of an HTML page."""
        from ai.dl_api_client import _raise_for_status, DLServerError

        class FakeResponse:
            status_code = 413
            content = b"<html>nginx 413 Too Large</html>"

        with self.assertRaises(DLServerError) as cm:
            _raise_for_status(FakeResponse())
        msg = str(cm.exception)
        self.assertIn("413", msg)
        self.assertIn("nginx 413 Too Large", msg)

    def test_raise_for_status_still_maps_known_statuses(self):
        from ai.dl_api_client import (
            _raise_for_status,
            DLUnauthorizedError,
            DLForbiddenError,
            DLTaskNotFoundError,
            DLServerError,
        )

        class R:
            def __init__(self, code, body=b""):
                self.status_code = code
                self.content = body

        self.assertRaises(DLUnauthorizedError, _raise_for_status, R(401))
        self.assertRaises(DLForbiddenError, _raise_for_status, R(403))
        self.assertRaises(DLTaskNotFoundError, _raise_for_status, R(404))
        self.assertRaises(DLServerError, _raise_for_status, R(500))
        # 200 — no raise.
        _raise_for_status(R(200))


class ConversationHistoryTests(TestCase):
    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.history = ConversationHistory(max_messages=4)

    def test_get_returns_empty_list_for_unknown_user(self):
        self.assertEqual(self.history.get("user-1"), [])

    def test_add_exchange_appends_messages(self):
        self.history.append("user-1", {"role": "user", "content": "hello"})
        self.history.append("user-1", {"role": "assistant", "content": "hi"})
        self.assertEqual(
            self.history.get("user-1"),
            [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
            ],
        )

    def test_history_caps_at_max_messages(self):
        for user_msg, assistant_msg in (("a", "A"), ("b", "B"), ("c", "C")):
            self.history.append("user-1", {"role": "user", "content": user_msg})
            self.history.append("user-1", {"role": "assistant", "content": assistant_msg})
        # max_messages=4, so after three exchanges (6 messages) we keep the last 4:
        # [assistant A, user b, assistant B, user c, assistant C] -> capped to 4
        # -> [user b, assistant B, user c, assistant C]
        history = self.history.get("user-1")
        self.assertEqual(len(history), 4)
        self.assertEqual(history[0]["content"], "b")

    def test_reset_clears_history(self):
        self.history.append("user-1", {"role": "user", "content": "hello"})
        self.history.append("user-1", {"role": "assistant", "content": "hi"})
        self.history.reset("user-1")
        self.assertEqual(self.history.get("user-1"), [])


class MessageComposerTests(TestCase):
    def setUp(self):
        self.composer = MessageComposer()

    async def test_chat_mode_appends_prompt_when_provided(self):
        data = {
            "type": "1",
            "message": "hello",
            "preprompt": "shared_999",
            "language": "English",
        }
        with patch.object(
            self.composer.resolver,
            "resolve_text",
            new=AsyncMock(return_value="Think step by step."),
        ):
            message, mode = await self.composer.compose(data)
        self.assertEqual(message, "hello\n\nRespond only in English.\n\nPreprompt: Think step by step.")
        self.assertEqual(mode, AIRequestLog.MODE_CHAT)

    async def test_solve_mode_uses_default_message_when_no_shared_prompt(self):
        data = {
            "type": "2",
            "message": "sum numbers",
            "language": "English",
            "programming_language_name": "Python",
            "topic_name": "Loops",
        }
        with patch.object(
            self.composer.resolver,
            "resolve_text",
            new=AsyncMock(return_value=None),
        ):
            with patch("ai.services.message_composer.get_default_shared_prompt", new=AsyncMock(return_value=None)):
                message, mode = await self.composer.compose(data)
        self.assertIn("Python", message)
        self.assertIn("Loops", message)
        self.assertIn("sum numbers", message)
        self.assertEqual(mode, AIRequestLog.MODE_SOLVE)


class ModelCallerTests(SimpleTestCase):
    async def test_returns_error_for_unknown_model(self):
        result = await ModelCaller().call("hi", "client", "Unknown_Model")
        self.assertTrue(result.is_error)
        self.assertIn("не найдена", result.response_text)

    async def test_returns_success_for_known_model(self):
        registry_mock = MagicMock()
        registry_mock.get.return_value = True
        registry_mock.handler.return_value = AsyncMock(return_value=("answer", 42))
        registry_mock.title.return_value = "Test Model"

        result = await ModelCaller(registry_mock).call("hi", "client", "Known")
        self.assertFalse(result.is_error)
        self.assertEqual(result.response_text, "answer")
        self.assertEqual(result.tokens, 42)
        self.assertEqual(result.model_title, "Test Model")

    async def test_propagates_is_error_from_client_3tuple(self):
        # Clients signal a 200-OK-but-error body (rate limit, «все боты
        # заняты», parse failure, …) via a 3rd tuple element. ModelCaller must
        # propagate it so the consumer routes to update_error — replacing the
        # old text-marker heuristic that mis-flagged legit answers discussing
        # «ошибка». A 2-tuple (no 3rd element) stays is_error=False (back-compat).
        registry_mock = MagicMock()
        registry_mock.get.return_value = True
        registry_mock.handler.return_value = AsyncMock(
            return_value=("Все боты заняты", 0, True)
        )
        registry_mock.title.return_value = "Web DeepSeek"

        result = await ModelCaller(registry_mock).call("hi", "client", "Known")
        self.assertTrue(result.is_error)
        self.assertEqual(result.response_text, "Все боты заняты")
        self.assertEqual(result.tokens, 0)


class RateLimiterTests(SimpleTestCase):
    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.limit = 2
        self.limiter = RateLimiter(ws_limit=self.limit, http_limit=self.limit, window_seconds=60)

    def test_allows_requests_under_limit(self):
        self.assertTrue(self.limiter.is_allowed_ws("user-1"))
        self.assertTrue(self.limiter.is_allowed_ws("user-1"))

    def test_blocks_requests_over_limit(self):
        self.limiter.is_allowed_ws("user-1")
        self.limiter.is_allowed_ws("user-1")
        self.assertFalse(self.limiter.is_allowed_ws("user-1"))

    def test_limits_are_isolated_by_user(self):
        self.limiter.is_allowed_ws("user-1")
        self.limiter.is_allowed_ws("user-1")
        self.assertTrue(self.limiter.is_allowed_ws("user-2"))

    def test_rate_limited_decorator_returns_429_when_over_limit(self):
        request = RequestFactory().get("/ai/api/prompts/")
        request.user = SimpleNamespace(is_authenticated=True, pk=1)
        request.user_info = {"userId": "1"}
        request.headers = {"Accept": "application/json"}

        custom_limiter = RateLimiter(ws_limit=2, http_limit=2, window_seconds=60)

        @rate_limited
        def sample_view(request):
            return HttpResponse("ok")

        with patch("ai.throttling.rate_limiter", custom_limiter):
            sample_view(request)
            sample_view(request)
            response = sample_view(request)
        self.assertEqual(response.status_code, 429)

    def test_poll_counter_is_separate_from_http_counter(self):
        from django.core.cache import cache
        from ai.throttling import RateLimiter

        cache.clear()
        # http_limit=2, poll_limit=5 — poll requests must not consume the
        # action budget and get their own (higher) bound.
        limiter = RateLimiter(ws_limit=2, http_limit=2, window_seconds=60, poll_limit=5)
        # Saturate the poll counter beyond the http limit.
        for _ in range(3):
            self.assertTrue(limiter.is_allowed_poll("user-1"))
        # The http (action) counter is untouched: still 2 actions allowed.
        self.assertTrue(limiter.is_allowed_http("user-1"))
        self.assertTrue(limiter.is_allowed_http("user-1"))
        self.assertFalse(limiter.is_allowed_http("user-1"))

    def test_poll_request_path_detected(self):
        from ai.throttling import _is_poll_request

        def mk(method, path):
            req = RequestFactory().get(path) if method == "GET" else RequestFactory().post(path)
            req.method = method
            return req

        self.assertTrue(_is_poll_request(mk("GET", "/ai/admin/arm/models/state/")))
        self.assertTrue(_is_poll_request(mk("GET", "/ai/admin/arm/find-error/status/")))
        # Non-poll paths and non-GET methods are not poll requests.
        self.assertFalse(_is_poll_request(mk("GET", "/ai/api/problem-data/")))
        self.assertFalse(_is_poll_request(mk("POST", "/ai/admin/arm/find-error/status/")))


class RateLimitMiddlewarePollTests(SimpleTestCase):
    """The middleware must route read-only polling endpoints through the
    separate poll counter so background polling never 429s real actions."""

    def setUp(self):
        from django.core.cache import cache
        cache.clear()

    def _request(self, path, method="GET"):
        request = RequestFactory().get(path) if method == "GET" else RequestFactory().post(path)
        request.method = method
        request.user = SimpleNamespace(is_authenticated=True, pk=1)
        request.user_info = {"userId": "1"}
        request.COOKIES = {"userId": "1"}
        request.headers = {"Accept": "application/json"}
        return request

    def test_poll_requests_do_not_consume_http_action_budget(self):
        from ai.throttling import RateLimitMiddleware, RateLimiter

        custom_limiter = RateLimiter(ws_limit=2, http_limit=2, window_seconds=60, poll_limit=10)
        calls = {"n": 0}

        def get_response(request):
            calls["n"] += 1
            return HttpResponse("ok")

        middleware = RateLimitMiddleware(get_response)
        middleware.enabled = True
        with patch("ai.throttling.rate_limiter", custom_limiter):
            # 5 poll requests — all pass (poll_limit=10) and don't touch http.
            for _ in range(5):
                self.assertEqual(middleware(self._request("/ai/admin/arm/models/state/")).status_code, 200)
            # Action budget (http_limit=2) is still fully available.
            self.assertEqual(middleware(self._request("/ai/api/problem-data/")).status_code, 200)
            self.assertEqual(middleware(self._request("/ai/api/problem-data/")).status_code, 200)
            # 3rd action now 429s — proves polls did not consume it.
            self.assertEqual(middleware(self._request("/ai/api/problem-data/")).status_code, 429)
        self.assertEqual(calls["n"], 7)


class UserIdentityForLogTests(TestCase):
    def test_extracts_external_id_from_user_with_account(self):
        user = get_user_model().objects.create_user(
            username="local",
            password="test-pass",
            first_name="First",
            last_name="Last",
        )
        ExternalDLAccount.objects.create(user=user, external_user_id="ext-42", external_login="local")
        identity = get_user_identity_for_log(user, None)
        self.assertEqual(identity["external_user_id"], "ext-42")
        self.assertEqual(identity["user_full_name"], "First Last")

    def test_extracts_from_external_info_string(self):
        identity = get_user_identity_for_log("ext-42", {"firstName": "Alice", "lastName": "Smith"})
        self.assertEqual(identity["external_user_id"], "ext-42")
        self.assertEqual(identity["user_full_name"], "Alice Smith")


class ModelCapabilitiesTests(SimpleTestCase):
    """ТЗ B: registry.capabilities shape and per-model annotations."""

    def test_reasoning_models_are_marked_reasoning(self):
        from ai.model_clients import registry
        from ai.model_clients.registry import _sambanova_models, ModelRegistry

        # Web_DeepSeek_Thinking is in the default (always-on) registry.
        self.assertTrue(registry.capabilities("Web_DeepSeek_Thinking")["reasoning"])
        # SambaNova reasoning model — via a local registry (gated globally).
        samba = ModelRegistry(_sambanova_models())
        caps = samba.capabilities("DeepSeek_R1_Distill_Llama_70B")
        self.assertTrue(caps["reasoning"], "DeepSeek_R1_Distill_Llama_70B should be reasoning")
        self.assertTrue(caps["text"])
        self.assertFalse(caps["vision"])

    def test_plain_text_models_are_not_reasoning(self):
        from ai.model_clients import registry

        caps = registry.capabilities("DeepSeek_V3_1")
        self.assertFalse(caps["reasoning"])
        self.assertTrue(caps["text"])
        self.assertFalse(caps["vision"])

    def test_unknown_key_gets_conservative_default(self):
        from ai.model_clients import registry

        caps = registry.capabilities("does_not_exist")
        self.assertEqual(caps, {"text": True, "vision": False, "reasoning": False})

    def test_every_entry_exposes_three_boolean_capabilities(self):
        from ai.model_clients import registry

        for key in registry.keys():
            caps = registry.capabilities(key)
            self.assertEqual(set(caps.keys()), {"text", "vision", "reasoning"})
            for value in caps.values():
                self.assertIsInstance(value, bool)


class ArmReportTests(SimpleTestCase):
    """ТЗ D: _build_summary / _build_report aggregation and ordering."""

    def _result(self, key, status, duration, tokens=0):
        return {
            "model_key": key,
            "model_title": key,
            "status": status,
            "duration": duration,
            "tokens": tokens,
        }

    def test_summary_aggregates_and_sorts_by_percent_then_duration(self):
        from ai.arm_runner import _build_summary

        results = [
            self._result("A", "ok", 3.0, tokens=10),
            self._result("B", "error", 1.0, tokens=5),
            self._result("A", "ok", 5.0, tokens=20),
        ]
        summary = _build_summary(results)
        # A: 2/2 solved = 100%, avg (3+5)/2 = 4.0, tokens 30
        # B: 0/1 solved = 0%, avg 1.0, tokens 5
        self.assertEqual(summary[0]["model_key"], "A")
        self.assertEqual(summary[0]["solved"], 2)
        self.assertEqual(summary[0]["total"], 2)
        self.assertEqual(summary[0]["percent_solved"], 100.0)
        self.assertEqual(summary[0]["avg_duration"], 4.0)
        self.assertEqual(summary[0]["tokens"], 30)
        self.assertEqual(summary[1]["model_key"], "B")
        self.assertEqual(summary[1]["percent_solved"], 0.0)

    def test_summary_tiebreak_orders_by_fastest_average_duration(self):
        from ai.arm_runner import _build_summary

        results = [
            self._result("Slow", "ok", 5.0),
            self._result("Fast", "ok", 2.0),
        ]
        summary = _build_summary(results)
        # Both 100% solved; faster average wins.
        self.assertEqual(summary[0]["model_key"], "Fast")
        self.assertEqual(summary[1]["model_key"], "Slow")

    def test_build_report_none_for_empty_results(self):
        from ai.arm_runner import _build_report

        self.assertIsNone(_build_report([]))

    def test_build_report_includes_summary_and_counts(self):
        from ai.arm_runner import _build_report

        results = [
            self._result("A", "ok", 2.0, tokens=10),
            self._result("B", "error", 4.0, tokens=7),
        ]
        report = _build_report(results)
        self.assertEqual(report["models_total"], 2)
        self.assertEqual(report["success_count"], 1)
        self.assertEqual(report["error_count"], 1)
        self.assertEqual(report["tokens_total"], 17)
        self.assertEqual(report["fastest_model"], "A")
        self.assertIn("summary", report)
        self.assertEqual(len(report["summary"]), 2)


class AutorecoveryTests(TestCase):
    """ТЗ E: _maybe_autorecover_web_deepseek gating, annotation, and never-raise."""

    def _down_row(self, window_date, key="Web_DeepSeek"):
        from ai.models import AIModelAvailability
        return AIModelAvailability.objects.create(
            model_key=key,
            model_title="Web DeepSeek",
            is_available=False,
            window_date=window_date,
            last_message="down",
        )

    @override_settings(AI_WEB_DEEPSEEK_AUTORECOVERY=False)
    def test_disabled_flag_skips_restart_even_when_down(self):
        from ai.model_health import _maybe_autorecover_web_deepseek, get_health_window_date

        window_date = get_health_window_date()
        row = self._down_row(window_date)
        with patch("ai.model_health.restart_bot_pool") as mock_restart:
            _maybe_autorecover_web_deepseek({}, window_date)
            mock_restart.assert_not_called()
        row.refresh_from_db()
        self.assertEqual(row.last_message, "down")

    def test_restart_failure_annotates_pool_unavailable(self):
        from ai.model_health import _maybe_autorecover_web_deepseek, get_health_window_date

        window_date = get_health_window_date()
        row = self._down_row(window_date)
        with patch("ai.model_health.restart_bot_pool", return_value=False):
            _maybe_autorecover_web_deepseek({}, window_date)
        row.refresh_from_db()
        self.assertIn("Автоподъём не удался", row.last_message)

    def test_restart_success_annotates_ok(self):
        from ai.model_health import _maybe_autorecover_web_deepseek, get_health_window_date

        window_date = get_health_window_date()
        row = self._down_row(window_date)
        with patch("ai.model_health.restart_bot_pool", return_value=True), \
                patch("ai.model_health._check_one_model",
                      return_value={"is_available": True, "last_message": "2",
                                    "last_http_code": 200, "response_time_ms": 1}), \
                patch("ai.model_health.time.sleep"):
            _maybe_autorecover_web_deepseek({}, window_date)
        row.refresh_from_db()
        self.assertIn("[автоподъём: ок]", row.last_message)

    def test_no_restart_when_web_deepseek_is_up(self):
        from ai.models import AIModelAvailability
        from ai.model_health import _maybe_autorecover_web_deepseek, get_health_window_date

        window_date = get_health_window_date()
        for key in ("Web_DeepSeek", "Web_DeepSeek_Thinking"):
            AIModelAvailability.objects.create(
                model_key=key, model_title=key, is_available=True, window_date=window_date,
            )
        with patch("ai.model_health.restart_bot_pool") as mock_restart:
            _maybe_autorecover_web_deepseek({}, window_date)
            mock_restart.assert_not_called()


class ModelHealthGuardTests(TestCase):
    """Serialization guard for run_model_health_check (multi-worker prod safety).

    The cold-boot race: N Daphne workers booting at once all see no
    AIModelHealthRun row for the window. get_or_create on the unique
    window_date lets only one process create; the rest must observe the
    winner's RUNNING/COMPLETED status and bail out without sweeping.
    """

    def _window(self):
        from ai.model_health import get_health_window_date
        return get_health_window_date()

    def test_bails_out_when_a_recent_running_run_exists(self):
        from ai.model_health import run_model_health_check
        from ai.models import AIModelHealthRun

        AIModelHealthRun.objects.create(
            window_date=self._window(),
            status=AIModelHealthRun.STATUS_RUNNING,
            started_at=timezone.now(),
            finished_at=None,
            error_message="",
        )
        # If the guard works, get_runtime_model_handlers is never called
        # (the function returns inside the atomic block before the sweep).
        with patch("ai.model_health.get_runtime_model_handlers") as mock_handlers:
            result = run_model_health_check(force=False)
            mock_handlers.assert_not_called()
        self.assertFalse(result)

    def test_bails_out_when_a_completed_run_exists(self):
        from ai.model_health import run_model_health_check
        from ai.models import AIModelHealthRun

        AIModelHealthRun.objects.create(
            window_date=self._window(),
            status=AIModelHealthRun.STATUS_COMPLETED,
            started_at=timezone.now(),
            finished_at=timezone.now(),
            error_message="",
        )
        with patch("ai.model_health.get_runtime_model_handlers") as mock_handlers:
            result = run_model_health_check(force=False)
            mock_handlers.assert_not_called()
        self.assertFalse(result)

    def test_cold_start_creates_run_and_completes_without_real_api(self):
        from ai.model_health import get_health_window_date, run_model_health_check
        from ai.models import AIModelHealthRun

        # No existing row -> this process becomes the creator and sweeps.
        # Empty handlers => _check_one_model persists "Handler not found" rows
        # without making any real network call.
        with patch("ai.model_health.get_runtime_model_handlers", return_value={}), \
                patch("ai.model_health._maybe_autorecover_web_deepseek"):
            result = run_model_health_check(force=False)
        self.assertTrue(result)
        run = AIModelHealthRun.objects.get(window_date=get_health_window_date())
        self.assertEqual(run.status, AIModelHealthRun.STATUS_COMPLETED)

    def test_force_does_not_double_run_a_recent_running_run(self):
        """force=True may re-run COMPLETED/stale runs, but must NOT bypass an
        actively-running (<45min) run. This is the cross-process TOCTOU guard:
        two concurrent --force / admin-refresh invocations only do a racy
        read-only pre-check, so the row lock inside run_model_health_check is
        the real serialization point — this guard must hold even for force.
        """
        from ai.model_health import run_model_health_check
        from ai.models import AIModelHealthRun

        AIModelHealthRun.objects.create(
            window_date=self._window(),
            status=AIModelHealthRun.STATUS_RUNNING,
            started_at=timezone.now(),
            finished_at=None,
            error_message="",
        )
        with patch("ai.model_health.get_runtime_model_handlers") as mock_handlers:
            result = run_model_health_check(force=True)
            mock_handlers.assert_not_called()
        self.assertFalse(result)

    def test_force_does_run_a_stale_running_run(self):
        """A RUNNING run started >45min ago is treated as stuck and force=True
        IS allowed to re-run it (that is what --force is for)."""
        from datetime import timedelta
        from ai.model_health import run_model_health_check
        from ai.models import AIModelHealthRun

        AIModelHealthRun.objects.create(
            window_date=self._window(),
            status=AIModelHealthRun.STATUS_RUNNING,
            started_at=timezone.now() - timedelta(minutes=60),
            finished_at=None,
            error_message="",
        )
        with patch("ai.model_health.get_runtime_model_handlers", return_value={}), \
                patch("ai.model_health._maybe_autorecover_web_deepseek"):
            result = run_model_health_check(force=True)
        self.assertTrue(result)

    def test_force_runs_a_completed_run(self):
        from ai.model_health import run_model_health_check
        from ai.models import AIModelHealthRun

        AIModelHealthRun.objects.create(
            window_date=self._window(),
            status=AIModelHealthRun.STATUS_COMPLETED,
            started_at=timezone.now(),
            finished_at=timezone.now(),
            error_message="",
        )
        with patch("ai.model_health.get_runtime_model_handlers", return_value={}), \
                patch("ai.model_health._maybe_autorecover_web_deepseek"):
            result = run_model_health_check(force=True)
        self.assertTrue(result)

    def test_autorecovery_never_raises_when_bot_pool_unreachable(self):
        """When restart_bot_pool() returns False, _maybe_autorecover tries to
        annotate the down models. A transient DB error there must not escape
        (docstring: 'Never raises'), so the surrounding health run is not
        flipped to FAILED."""
        from ai.model_health import _maybe_autorecover_web_deepseek
        from ai.models import AIModelAvailability

        window = self._window()
        AIModelAvailability.objects.create(
            window_date=window,
            model_key="Web_DeepSeek",
            model_title="t",
            is_available=False,
            last_message="down",
        )
        with patch("ai.model_health.restart_bot_pool", return_value=False), \
                patch("ai.model_health._save_availability", side_effect=Exception("DB down")):
            # Must not raise.
            _maybe_autorecover_web_deepseek({}, window)


class HealthClassifierTests(SimpleTestCase):
    """Robust healthcheck classifier: a correct answer wins unless the reply is
    a definite API/client error. Loose stems (недоступ/подключени/ошибка) no
    longer flip a healthy '2' to down."""

    def _healthy(self, text):
        from ai.model_health import _is_healthy_response
        return _is_healthy_response(text)

    def test_plain_digit_is_healthy(self):
        self.assertTrue(self._healthy("2"))
        self.assertTrue(self._healthy(" 2 "))

    def test_correct_answer_with_loose_marker_word_still_healthy(self):
        # Regression: previously "недоступ" stem marked this down even though
        # the model answered correctly.
        self.assertTrue(self._healthy("Никаких ошибок нет, ответ: 2"))
        self.assertTrue(self._healthy("Подключение установлено. 2"))

    def test_word_form_with_punctuation_is_healthy(self):
        self.assertTrue(self._healthy("два."))
        self.assertTrue(self._healthy("two."))
        self.assertTrue(self._healthy("Two"))

    def test_wrong_digit_is_unhealthy(self):
        self.assertFalse(self._healthy("12"))
        self.assertFalse(self._healthy("3"))

    def test_empty_is_unhealthy(self):
        self.assertFalse(self._healthy(""))
        self.assertFalse(self._healthy(None))

    def test_definite_api_error_is_unhealthy_even_with_digit_nearby(self):
        self.assertFalse(self._healthy("Ошибка API (код 402): закончились кредиты"))
        # "2 минуты" must not rescue a rate-limit reply.
        self.assertFalse(self._healthy("rate limit, подождите 2 минуты"))
        self.assertFalse(self._healthy("Бот не авторизован"))
        self.assertFalse(self._healthy("Таймаут при подключении к серверу. Попробуйте позже."))


class HealthCheckTransientTests(SimpleTestCase):
    """Retry-decision helpers (no DB needed): transient detection + invoke."""

    def test_timeout_is_transient(self):
        from ai.model_health import _looks_transient
        self.assertTrue(_looks_transient("Таймаут при подключении к серверу. Попробуйте позже."))
        self.assertTrue(_looks_transient("Бот инициализируется слишком долго."))
        self.assertTrue(_looks_transient(""))

    def test_definite_api_error_is_not_transient(self):
        from ai.model_health import _looks_transient
        self.assertFalse(_looks_transient("Ошибка API (код 402): закончились кредиты"))
        self.assertFalse(_looks_transient("Бот не авторизован"))

    def test_exception_is_transient(self):
        from ai.model_health import _looks_transient
        self.assertTrue(_looks_transient("", exc=Exception("boom")))

    def test_invoke_returns_text_and_no_exc_on_success(self):
        from datetime import date
        from ai.model_health import _invoke_healthcheck

        async def handler(prompt, conv_id):
            return ("2", 1)

        text, elapsed, exc = _invoke_healthcheck(handler, date(2026, 1, 1), "K")
        self.assertEqual(text, "2")
        self.assertIsNone(exc)
        self.assertGreaterEqual(elapsed, 0)

    def test_invoke_returns_exc_on_failure(self):
        from datetime import date
        from ai.model_health import _invoke_healthcheck

        async def handler(prompt, conv_id):
            raise RuntimeError("network down")

        text, elapsed, exc = _invoke_healthcheck(handler, date(2026, 1, 1), "K")
        self.assertEqual(text, "")
        self.assertIsInstance(exc, RuntimeError)


class HealthCheckRetryTests(TestCase):
    """One retry on transient failure so a cold-start timeout on a now-working
    model is not persisted as down. Definite API errors are not retried."""

    def _window(self):
        from ai.model_health import get_health_window_date
        return get_health_window_date()

    def test_retries_once_on_transient_timeout_then_marks_up(self):
        from ai.model_health import _check_one_model

        state = {"n": 0}

        async def handler(prompt, conv_id):
            state["n"] += 1
            if state["n"] == 1:
                return ("Таймаут при подключении к серверу. Попробуйте позже.", "0")
            return ("2", 1)

        handler_info = {"handler": handler, "title": "Test"}
        with patch("ai.model_health.time.sleep"):
            result = _check_one_model("TestKey", "Test", handler_info, self._window())
        self.assertTrue(result["is_available"])
        self.assertEqual(result["last_http_code"], 200)
        self.assertEqual(state["n"], 2)

    def test_does_not_retry_on_definite_api_error(self):
        from ai.model_health import _check_one_model

        state = {"n": 0}

        async def handler(prompt, conv_id):
            state["n"] += 1
            return ("Ошибка API (код 402): закончились кредиты", "0")

        handler_info = {"handler": handler, "title": "Test"}
        with patch("ai.model_health.time.sleep"):
            result = _check_one_model("TestKey", "Test", handler_info, self._window())
        self.assertFalse(result["is_available"])
        self.assertEqual(result["last_http_code"], 402)
        self.assertEqual(state["n"], 1)


class ChatViewSelfHealTests(SimpleTestCase):
    """When no model is available for the current window, the chat page kicks a
    non-blocking forced sweep so a freshly-fixed key/balance recovers without
    waiting for 04:00 MSK or a manual --force."""

    def setUp(self):
        self.factory = RequestFactory()

    def _chat_request(self):
        request = self.factory.get("/ai/chat/")
        request.user = SimpleNamespace(is_authenticated=True, is_active=True, username="u")
        request.session = {}
        request.user_info = {"userId": "u"}
        request.COOKIES = {"userId": "u"}
        return request

    def test_empty_models_triggers_async_refresh(self):
        request = self._chat_request()
        with patch("ai.views.AIAppSettings.get_solo", return_value=SimpleNamespace(is_enabled=True)), \
             patch("ai.views.get_available_model_options", return_value=[]), \
             patch("ai.views.trigger_model_health_refresh_async") as mock_trigger, \
             patch("ai.views.render", return_value=HttpResponse("ok")):
            response = chat_view(request)
        self.assertEqual(response.status_code, 200)
        mock_trigger.assert_called_once()

    def test_populated_models_does_not_trigger_refresh(self):
        request = self._chat_request()
        models = [{"key": "DeepSeek_V3_1", "title": "DeepSeek-V3.1", "capabilities": {}}]
        with patch("ai.views.AIAppSettings.get_solo", return_value=SimpleNamespace(is_enabled=True)), \
             patch("ai.views.get_available_model_options", return_value=models), \
             patch("ai.views.trigger_model_health_refresh_async") as mock_trigger, \
             patch("ai.views.render", return_value=HttpResponse("ok")):
            response = chat_view(request)
        self.assertEqual(response.status_code, 200)
        mock_trigger.assert_not_called()


class TranslatePromptsCommandTests(TestCase):
    """translate_prompts: fill only empty _en/_fr fields, preserve placeholders,
    and --dry-run must not write."""

    def _echo_handler(self):
        async def handler(prompt, conv_id):
            # Echo the protected payload back so placeholder restore is exercised.
            marker = "\n\nТекст:\n"
            idx = prompt.find(marker)
            payload = prompt[idx + len(marker):] if idx >= 0 else prompt
            return (payload, 0)
        return handler

    def _run(self, *args):
        from io import StringIO
        from django.core.management import call_command
        out = StringIO()
        call_command("translate_prompts", *args, stdout=out)
        return out.getvalue()

    def setUp(self):
        from ai.models import SharedPrompt, Prompt, Topic
        self.shared = SharedPrompt.objects.create(
            prompt_name="Решить задачу",
            prompt_text="Реши задачу на {language} по теме {тема}. Код: {code}",
            prompt_name_en="",
            prompt_text_en="already-en",
            prompt_name_fr="",
            prompt_text_fr="",
        )

    def test_fills_only_empty_fields_and_preserves_placeholders(self):
        with patch("ai.management.commands.translate_prompts.registry.handler",
                   return_value=self._echo_handler()):
            self._run()

        self.shared.refresh_from_db()
        # Empty name fields were translated (echoed back).
        self.assertEqual(self.shared.prompt_name_en, "Решить задачу")
        self.assertEqual(self.shared.prompt_name_fr, "Решить задачу")
        # Non-empty _en text was NOT overwritten.
        self.assertEqual(self.shared.prompt_text_en, "already-en")
        # Empty _fr text was filled, with placeholders restored verbatim.
        self.assertIn("{language}", self.shared.prompt_text_fr)
        self.assertIn("{тема}", self.shared.prompt_text_fr)
        self.assertIn("{code}", self.shared.prompt_text_fr)
        self.assertNotIn("@@PH", self.shared.prompt_text_fr)

    def test_dry_run_does_not_write(self):
        with patch("ai.management.commands.translate_prompts.registry.handler",
                   return_value=self._echo_handler()):
            self._run("--dry-run")
        self.shared.refresh_from_db()
        self.assertEqual(self.shared.prompt_name_en, "")
        self.assertEqual(self.shared.prompt_name_fr, "")

    def test_unsupported_language_raises(self):
        from django.core.management import CommandError
        with patch("ai.management.commands.translate_prompts.registry.handler",
                   return_value=self._echo_handler()):
            with self.assertRaises(CommandError):
                self._run("--languages", "de")


class BatchGradingTests(SimpleTestCase):
    """Batch-solve ARM grading: normalize_solution / grade_solution."""

    def test_identical_after_whitespace_and_case_normalization(self):
        from ai.arm_runner import grade_solution
        self.assertEqual(
            grade_solution("Program A;\nBegin\n  Writeln(1);\nEnd.", "program a; begin writeln(1); end."),
            "solved",
        )

    def test_pascal_brace_and_paren_comments_stripped(self):
        from ai.arm_runner import grade_solution
        sample = "{ this is a comment } program a; begin writeln(1); end."
        model = "(* another *) program a; begin writeln(1); end."
        self.assertEqual(grade_solution(model, sample), "solved")

    def test_c_line_and_block_comments_stripped(self):
        from ai.arm_runner import grade_solution
        sample = "#include <stdio.h>\nint main(){return 0;}"
        model = "// leading comment\nint main(){ /* x */ return 0; }"
        self.assertEqual(grade_solution(model, sample), "solved")

    def test_different_solution_is_failed(self):
        from ai.arm_runner import grade_solution
        # Substantially different solutions fall below the similarity threshold.
        # (A single-token diff like return 42 vs return 0 is intentionally NOT
        #  enough to fail — grading is approximate, see CLAUDE.md.)
        self.assertEqual(
            grade_solution("print('hello world')", "int main(){return 0;}"),
            "failed",
        )

    def test_empty_sample_is_skipped(self):
        from ai.arm_runner import grade_solution
        self.assertEqual(grade_solution("anything", ""), "skipped")

    def test_empty_model_is_failed(self):
        from ai.arm_runner import grade_solution
        self.assertEqual(grade_solution("", "int main(){return 0;}"), "failed")


class HealthCheckOutputTests(SimpleTestCase):
    """`check_models_health --force` live per-model console line formatting."""

    def _capture(self, detail):
        from io import StringIO
        from ai.management.commands.check_models_health import Command

        cmd = Command()
        out = StringIO()
        cmd.stdout = out
        cmd._print_model_detail(detail)
        return out.getvalue()

    def test_healthy_model_prints_200_and_response(self):
        line = self._capture({
            "title": "DeepSeek V3", "is_available": True,
            "last_http_code": 200, "response_time_ms": 850,
            "last_message": "2",
        })
        self.assertIn("DeepSeek V3", line)
        self.assertIn("HTTP 200", line)
        self.assertIn("OK", line)
        self.assertIn("850ms", line)
        self.assertIn("| 2", line)

    def test_down_model_prints_error_code_and_message(self):
        line = self._capture({
            "title": "GigaChat", "is_available": False,
            "last_http_code": 402, "response_time_ms": 120,
            "last_message": "Ошибка API (код 402): закончились кредиты",
        })
        self.assertIn("HTTP 402", line)
        self.assertIn("FAIL", line)
        self.assertIn("закончились кредиты", line)

    def test_missing_code_shows_dash(self):
        line = self._capture({
            "title": "Bot", "is_available": False,
            "last_http_code": None, "response_time_ms": None,
            "last_message": "Health check exception: timeout",
        })
        self.assertIn("HTTP —", line)
        self.assertIn("| — |", line)


class BatchReportTests(SimpleTestCase):
    """Batch-solve ARM report: per-model / per-topic aggregation + ordering."""

    def _item(self, model, topic, verdict, duration, tokens=0):
        return {
            "model_key": model, "model_title": model,
            "topic_name": topic, "verdict": verdict,
            "duration": duration, "tokens": tokens,
        }

    def test_per_model_and_per_topic_with_skipped_excluded(self):
        from ai.arm_runner import _build_batch_report

        results = [
            self._item("A", "Линейные", "solved", 2.0, 10),
            self._item("A", "Циклы", "failed", 4.0, 10),
            self._item("A", "Линейные", "skipped", 1.0, 2),
            self._item("B", "Линейные", "solved", 5.0, 20),
            self._item("B", "Циклы", "solved", 3.0, 20),
        ]
        report = _build_batch_report(results)
        # Top-level counts: 3 solved, 1 failed, 1 skipped, 5 total.
        self.assertEqual(report["total_pairs"], 5)
        self.assertEqual(report["solved"], 3)
        self.assertEqual(report["failed"], 1)
        self.assertEqual(report["skipped"], 1)

        # Per-model: A has 1 solved / 2 non-skipped = 50%, B 2/2 = 100%.
        per_model = {row["label"]: row for row in report["per_model"]}
        self.assertEqual(per_model["A"]["solved"], 1)
        self.assertEqual(per_model["A"]["total"], 2)
        self.assertEqual(per_model["A"]["percent_solved"], 50.0)
        self.assertEqual(per_model["B"]["percent_solved"], 100.0)
        # Sorted by % desc -> B first.
        self.assertEqual(report["per_model"][0]["label"], "B")

        # Per-topic: skipped excluded. Линейные: A solved + B solved + A skipped
        # -> 2 solved / 2 total = 100%. Циклы: A failed + B solved -> 1/2 = 50%.
        per_topic = {row["label"]: row for row in report["per_topic"]}
        self.assertEqual(per_topic["Линейные"]["percent_solved"], 100.0)
        self.assertEqual(per_topic["Циклы"]["percent_solved"], 50.0)


class TaskModelTests(TestCase):
    """Task model basics (DL task reference for batch-solve ARM)."""

    def test_node_id_unique_and_str(self):
        from django.db import transaction
        from ai.models import Task
        Task.objects.create(node_id=12345, name="Сумма двух чисел")
        # Wrap the expected unique violation in a savepoint so the TestCase's
        # outer transaction is not left broken for subsequent tests.
        with transaction.atomic():
            with self.assertRaises(Exception):
                Task.objects.create(node_id=12345)
        t = Task.objects.get(node_id=12345)
        self.assertEqual(str(t), "Сумма двух чисел")
        t2 = Task.objects.create(node_id=99)
        self.assertEqual(str(t2), "DL #99")

    def test_active_default_true(self):
        from ai.models import Task
        t = Task.objects.create(node_id=555)
        self.assertTrue(t.active)


class BatchRunnerIntegrationTests(TestCase):
    """End-to-end batch solve with mocked handlers + DL sample fetch.

    Calls ``_run_batch_job_worker`` directly (synchronously, same thread) rather
    than going through the daemon thread in ``start_batch_solve_run``: the real
    thread runs on a separate DB connection which a TestCase's per-test
    transaction would hide. The worker function is the unit that owns the
    handler calls, grading, persistence and report — exercising it directly is a
    faithful, deterministic test of that logic.
    """

    def setUp(self):
        from ai.models import Task
        self.user = get_user_model().objects.create_user(username="batcher", password="x")
        self.lang = ProgrammingLanguage.objects.create(language_name="Pascal")
        self.topic = Topic.objects.create(topic_name="Линейные", programming_language=self.lang)
        self.t1 = Task.objects.create(
            node_id=1001, task_id=2001, name="A", statement="Сложите a и b",
            topic=self.topic, programming_language=self.lang, file_extension=".pas",
        )
        self.t2 = Task.objects.create(
            node_id=1002, task_id=2002, name="B", statement="Выведите n",
            topic=self.topic, programming_language=self.lang, file_extension=".pas",
        )

    def test_batch_run_records_solved_verdicts(self):
        import time as _t
        from ai import arm_runner
        from ai.models import AIModelTestResult, AIModelTestRun, Task

        async def fake_handler(messages, conv_id):
            return ("program a; begin writeln(1); end.", 12)

        ordered_models = [{"key": "FakeModel", "title": "FakeModel", "handler": fake_handler}]
        node_ids = [self.t1.node_id, self.t2.node_id]
        run_id = "test-batch-run-1"

        # Pre-seed the in-memory job so the worker can record live progress and
        # build the final report from it (mirrors start_batch_solve_run).
        now_ts = _t.time()
        arm_runner._jobs[run_id] = {
            "run_id": run_id, "run_type": "batch", "status": "running",
            "error_message": "", "total_models": 1,
            "total_pairs": 2, "completed_pairs": 0, "completed_models": 0,
            "current_model_key": "FakeModel", "current_model_title": "FakeModel",
            "current_task_node_id": "", "current_task_name": "",
            "results": [], "report": None,
            "created_at_ts": now_ts, "updated_at_ts": now_ts,
        }
        # Вердикт теперь строго по DL: мокаем _test_solution_on_dl успехом.
        dl_ok = lambda sid, node_id, code, ext, **kw: {
            "verdict": "solved", "comment": "Все тесты успешно пройдены",
            "submit_error": "", "queue_id": 1, "code_sent": code,
        }
        try:
            with patch("ai.arm_runner._test_solution_on_dl", dl_ok):
                arm_runner._run_batch_job_worker(
                    run_id, node_ids, ordered_models, self.user.id, "DLSID-1",
                    ui_language="Русский", dl_test=True,
                )
        finally:
            arm_runner._jobs.pop(run_id, None)

        run = AIModelTestRun.objects.get(run_id=run_id)
        self.assertEqual(run.status, AIModelTestRun.STATUS_COMPLETED)
        self.assertEqual(run.run_type, AIModelTestRun.RUN_TYPE_BATCH)

        results = list(AIModelTestResult.objects.filter(run=run))
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r.verdict == "solved" for r in results))
        self.assertTrue(all(r.task_id in (self.t1.id, self.t2.id) for r in results))
        # Код и DL-коммент сохраняются в новых полях.
        self.assertTrue(all(r.code == "program a; begin writeln(1); end." for r in results))
        self.assertTrue(all(r.dl_comment == "Все тесты успешно пройдены" for r in results))
        self.assertTrue(all(r.dl_queue_id == 1 for r in results))
        self.assertTrue(all(r.file_extension_snapshot == ".pas" for r in results))

        # DB-fallback snapshot rebuilds the batch report from persisted rows.
        snapshot = arm_runner.get_arm_run_snapshot(run_id)
        self.assertEqual(snapshot["run_type"], "batch")
        self.assertEqual(snapshot["report"]["per_model"][0]["solved"], 2)
        self.assertEqual(snapshot["report"]["per_model"][0]["total"], 2)


class TestSolutionOnDlTests(SimpleTestCase):
    """``_test_solution_on_dl``: на 400 от send-solution пробует taskId вместо
    nodeId (гипотеза пользователя: «с твоим nodeId — доступ запрещён») и при
    успехе падает в поллинг. Старая подсказка «язык выбран неверно» удалена."""

    def test_400_falls_back_to_task_id_and_polls(self):
        from ai import arm_runner
        from ai.dl_api_client import DLServerError

        calls = []

        def fake_send(session_id, node_id, code, file_extension, course_id=0):
            calls.append(node_id)
            if node_id == 2606747:
                raise DLServerError(
                    "send-solution(nodeId=2606747, fileExtension='.mpc', "
                    "codeLen=111, codeHead='...'): DL API вернул ошибку "
                    "(код 400): Bad Request"
                )
            # taskId принят
            return {"queueId": 42, "message": "ok"}

        def fake_poll(session_id, queue_id):
            return {"isFinished": True, "comment": "Все тесты успешно пройдены"}

        with patch("ai.dl_api_client.send_solution_to_dl", fake_send), \
                patch("ai.dl_api_client.get_solution_result_from_dl", fake_poll):
            res = arm_runner._test_solution_on_dl(
                "SID", 2606747, "code...", ".mpc",
                max_polls=1, poll_interval=0, task_id=2001,
            )
        # Сначала nodeId, затем taskId.
        self.assertEqual(calls, [2606747, 2001])
        self.assertEqual(res["queue_id"], 42)
        self.assertEqual(res["submit_error"], "")
        self.assertEqual(res["verdict"], "solved")
        self.assertEqual(res["comment"], "Все тесты успешно пройдены")

    def test_400_both_ids_rejected_reports_honest_error(self):
        from ai import arm_runner
        from ai.dl_api_client import DLServerError

        def fake_send(session_id, node_id, code, file_extension, course_id=0):
            raise DLServerError(
                f"send-solution(nodeId={node_id}, fileExtension='.mpc', "
                f"codeLen=111, codeHead='...'): DL API вернул ошибку "
                f"(код 400): Bad Request"
            )

        with patch("ai.dl_api_client.send_solution_to_dl", fake_send):
            res = arm_runner._test_solution_on_dl(
                "SID", 2606747, "code...", ".mpc", task_id=2001,
            )
        self.assertEqual(res["queue_id"], 0)
        self.assertIn("код 400", res["submit_error"])
        # Оба идентификатора видны оператору для проверки вручную.
        self.assertIn("nodeId=2606747", res["submit_error"])
        self.assertIn("taskId=2001", res["submit_error"])
        # Старая неверная подсказка о языке удалена.
        self.assertNotIn("язык выбран неверно", res["submit_error"])

    def test_400_without_task_id_skips_fallback(self):
        from ai import arm_runner
        from ai.dl_api_client import DLServerError

        calls = []

        def fake_send(session_id, node_id, code, file_extension, course_id=0):
            calls.append(node_id)
            raise DLServerError(
                f"send-solution(nodeId={node_id}, ...): DL API вернул ошибку "
                f"(код 400): Bad Request"
            )

        with patch("ai.dl_api_client.send_solution_to_dl", fake_send):
            res = arm_runner._test_solution_on_dl(
                "SID", 2606747, "code...", ".cpp", task_id=0,
            )
        # Без taskId fallback не делается — ровно один вызов (по nodeId).
        self.assertEqual(calls, [2606747])
        self.assertEqual(res["queue_id"], 0)
        self.assertIn("код 400", res["submit_error"])


class SambanovaLoggerTests(SimpleTestCase):
    """sambanova.py must define a module-level logger — its absence made every
    SambaNova model FAIL health-check with `name 'logger' is not defined`."""

    def test_module_defines_logger(self):
        import logging as _logging
        from ai.model_clients import sambanova
        self.assertTrue(hasattr(sambanova, "logger"))
        self.assertIsInstance(sambanova.logger, _logging.Logger)
        # The functions referenced by the registry must import cleanly.
        # getattr, т.к. ask_* генерируются динамически (make_table_handlers)
        # и статический анализатор их не видит.
        self.assertTrue(callable(getattr(sambanova, "ask_DeepSeek_V3_2_async")))
        self.assertTrue(callable(getattr(sambanova, "ask_Gpt_oss_120b_async")))


class TaskRegistryTests(TestCase):
    """Auto-registration of DL tasks solved via the chat page."""

    def setUp(self):
        self.lang = ProgrammingLanguage.objects.create(language_name="Pascal")
        self.other_lang = ProgrammingLanguage.objects.create(language_name="Python")
        self.topic = Topic.objects.create(topic_name="Линейные", programming_language=self.lang)

    def test_apply_dl_task_info_sets_truthy_fields_only(self):
        from ai.models import Task
        from ai.services import apply_dl_task_info
        t = Task.objects.create(node_id=1, name="old", statement="old stmt", task_id=11)
        apply_dl_task_info(t, {"taskId": 22, "name": "Новое название", "statement": "Новое условие"})
        self.assertEqual(t.task_id, 22)
        self.assertEqual(t.name, "Новое название")
        self.assertEqual(t.statement, "Новое условие")
        # Empty values must not clobber existing fields.
        apply_dl_task_info(t, {"taskId": None, "name": "", "statement": ""})
        self.assertEqual(t.task_id, 22)
        self.assertEqual(t.name, "Новое название")
        self.assertEqual(t.statement, "Новое условие")

    def test_ensure_task_creates_inactive_and_fills_from_dl(self):
        from ai.models import Task
        from ai.services import ensure_task
        dl_data = {"taskId": 777, "name": "Сумма", "statement": "Даны a и b, верните a+b"}
        with patch("ai.services.task_registry.fetch_task_info", return_value=dl_data):
            task = ensure_task(
                42, programming_language_id=self.lang.id, topic_id=self.topic.id, session_id="DLSID-1"
            )
        self.assertIsNotNone(task)
        self.assertEqual(task.node_id, 42)
        self.assertFalse(task.active)  # auto-registered tasks are inactive until operator readies them
        self.assertEqual(task.programming_language_id, self.lang.id)
        self.assertEqual(task.topic_id, self.topic.id)
        self.assertEqual(task.task_id, 777)
        self.assertEqual(task.name, "Сумма")
        self.assertEqual(task.statement, "Даны a и b, верните a+b")
        self.assertEqual(Task.objects.filter(node_id=42).count(), 1)

    def test_ensure_task_without_session_creates_without_dl(self):
        from ai.services import ensure_task
        with patch("ai.services.task_registry.fetch_task_info") as mocked:
            task = ensure_task(7, programming_language_id=self.lang.id, topic_id=self.topic.id, session_id=None)
        self.assertIsNotNone(task)
        self.assertFalse(task.active)
        self.assertEqual(task.name, "")
        self.assertEqual(task.statement, "")
        self.assertIsNone(task.task_id)
        mocked.assert_not_called()  # no DL fetch without a session

    def test_ensure_task_existing_updates_assignments_no_dl(self):
        from ai.models import Task
        from ai.services import ensure_task
        Task.objects.create(
            node_id=9, name="exists", statement="stmt", task_id=5,
            programming_language=self.lang, topic=self.topic, file_extension=".pas", active=True,
        )
        with patch("ai.services.task_registry.fetch_task_info") as mocked:
            task = ensure_task(
                9, programming_language_id=self.other_lang.id, topic_id=None, session_id="DLSID"
            )
        self.assertEqual(task.node_id, 9)
        # Local assignments refreshed to the latest solve request; active unchanged.
        self.assertEqual(task.programming_language_id, self.other_lang.id)
        self.assertTrue(task.active)
        # No DL fetch for an already-existing task.
        mocked.assert_not_called()

    def test_ensure_task_swallows_dl_errors(self):
        from ai.services import ensure_task
        from ai.dl_api_client import DLApiError
        with patch("ai.services.task_registry.fetch_task_info", side_effect=DLApiError("boom")):
            task = ensure_task(100, programming_language_id=self.lang.id, topic_id=None, session_id="DLSID")
        # Task still created; DL fields just left blank. No exception propagated.
        self.assertIsNotNone(task)
        self.assertEqual(task.node_id, 100)
        self.assertEqual(task.name, "")

    def test_ensure_task_never_raises(self):
        from ai.services import ensure_task
        with patch("ai.models.Task.objects.get_or_create", side_effect=RuntimeError("db down")):
            result = ensure_task(200, session_id="DLSID")
        self.assertIsNone(result)  # registration must never break the chat


class PromptGradingTests(SimpleTestCase):
    """Prompt-regression comparators in ai/grading.py::compare_response."""

    def test_ratio_identical_is_match(self):
        from ai.grading import compare_response
        verdict, hint, missing = compare_response(
            "program a; begin writeln(1); end.",
            "program a; begin writeln(1); end.",
            comparator="ratio",
        )
        self.assertEqual(verdict, "match")
        self.assertEqual(missing, [])

    def test_ratio_different_is_mismatch(self):
        from ai.grading import compare_response
        verdict, hint, missing = compare_response(
            "print('hello world')", "int main(){return 0;}", comparator="ratio",
        )
        self.assertEqual(verdict, "mismatch")
        self.assertIn("ratio", hint)

    def test_ratio_threshold_respected(self):
        from ai.grading import compare_response
        # Very close text: ratio is high but below 0.999.
        verdict, _hint, _missing = compare_response(
            "program a; begin writeln(1); end.",
            "program a; begin writeln(2); end.",
            comparator="ratio",
            threshold=0.99,
        )
        self.assertEqual(verdict, "mismatch")

    def test_contains_all_match(self):
        from ai.grading import compare_response
        expected = "В строке 5 нет точки с запятой\nпеременная x не объявлена"
        actual = "В коде в строке 5 нет точки с запятой. Также переменная x не объявлена."
        verdict, hint, missing = compare_response(actual, expected, comparator="contains_all")
        self.assertEqual(verdict, "match")
        self.assertEqual(missing, [])

    def test_contains_all_mismatch_lists_missing(self):
        from ai.grading import compare_response
        expected = "ошибка деления на ноль\nнет точки с запятой"
        actual = "В коде ошибка деления на ноль."
        verdict, hint, missing = compare_response(actual, expected, comparator="contains_all")
        self.assertEqual(verdict, "mismatch")
        self.assertIn("нет точки с запятой", missing)
        self.assertIn("отсутствуют", hint)

    def test_exact_match_and_mismatch(self):
        from ai.grading import compare_response
        self.assertEqual(
            compare_response("Program A;\nBegin\n  Writeln(1);\nEnd.",
                             "program a; begin writeln(1); end.", comparator="exact")[0],
            "match",
        )
        self.assertEqual(
            compare_response("return 0", "return 1", comparator="exact")[0],
            "mismatch",
        )

    def test_set_match_and_mismatch(self):
        from ai.grading import compare_response
        self.assertEqual(
            compare_response("a\nb\nc", "c\nb\na", comparator="set")[0], "match",
        )
        verdict, hint, missing = compare_response("a\nb", "a\nb\nc", comparator="set")
        self.assertEqual(verdict, "mismatch")
        self.assertIn("c", missing)

    def test_empty_expected_is_skipped(self):
        from ai.grading import compare_response
        self.assertEqual(compare_response("anything", "", comparator="ratio")[0], "skipped")

    def test_empty_actual_is_mismatch(self):
        from ai.grading import compare_response
        verdict, hint, _missing = compare_response("", "int main(){return 0;}", comparator="ratio")
        self.assertEqual(verdict, "mismatch")
        self.assertIn("пустой", hint)


class PromptRegressionRunnerTests(TestCase):
    """ai/prompt_test_runner.py: per-case verdicts + DB fallback snapshot."""

    def setUp(self):
        from ai.models import PromptTestCase
        self.user = get_user_model().objects.create_user(username="prompt-tester", password="x")
        self.lang = ProgrammingLanguage.objects.create(language_name="Pascal")
        self.topic = Topic.objects.create(topic_name="Линейные", programming_language=self.lang)
        self.case_match = PromptTestCase.objects.create(
            name="Solve match", mode="solve",
            input_text="Сложите a и b",
            expected_text="program a; begin writeln(1); end.",
            comparator="ratio",
            programming_language=self.lang, topic=self.topic,
        )
        self.case_mismatch = PromptTestCase.objects.create(
            name="Solve mismatch", mode="solve",
            input_text="Выведите n",
            expected_text="int main(){return 0;}",
            comparator="ratio",
            programming_language=self.lang, topic=self.topic,
        )

    def _run_worker(self, run_id, cases, handler_response):
        import time as _t
        from ai import prompt_test_runner

        async def fake_handler(messages, conv_id):
            return handler_response, 12

        model = {"key": "FakeModel", "title": "FakeModel", "handler": fake_handler}
        now_ts = _t.time()
        prompt_test_runner._jobs[run_id] = {
            "run_id": run_id, "status": "running", "error_message": "",
            "total_cases": len(cases), "completed_cases": 0, "current_case_name": cases[0].name if cases else "",
            "results": [], "report": None,
            "created_at_ts": now_ts, "updated_at_ts": now_ts,
        }
        try:
            prompt_test_runner._run_job_worker(
                run_id, cases, model, self.user.id,
                prompt_id=None, ui_language="Русский",
            )
        finally:
            prompt_test_runner._jobs.pop(run_id, None)

    def test_run_records_match_and_mismatch_verdicts(self):
        from ai.models import PromptTestResult, PromptTestRun
        run_id = "test-prompt-run-1"
        # Handler returns the golden solution: matches case_match, mismatches case_mismatch.
        self._run_worker(run_id, [self.case_match, self.case_mismatch],
                         "program a; begin writeln(1); end.")

        run = PromptTestRun.objects.get(run_id=run_id)
        self.assertEqual(run.status, PromptTestRun.STATUS_COMPLETED)

        results = {r.test_case_id: r for r in PromptTestResult.objects.filter(run=run)}
        self.assertEqual(results[self.case_match.id].verdict, PromptTestResult.VERDICT_MATCH)
        self.assertEqual(results[self.case_mismatch.id].verdict, PromptTestResult.VERDICT_MISMATCH)

        # DB-fallback snapshot rebuilds the report from persisted rows.
        from ai import prompt_test_runner
        snapshot = prompt_test_runner.get_prompt_test_run_snapshot(run_id)
        self.assertEqual(snapshot["status"], "completed")
        self.assertEqual(snapshot["report"]["total"], 2)
        self.assertEqual(snapshot["report"]["matched"], 1)
        self.assertEqual(snapshot["report"]["mismatched"], 1)
        self.assertEqual(len(snapshot["report"]["mismatches"]), 1)
        self.assertEqual(snapshot["report"]["mismatches"][0]["case_name"], "Solve mismatch")

    def test_skipped_when_expected_empty(self):
        from ai.models import PromptTestCase, PromptTestResult, PromptTestRun
        case = PromptTestCase.objects.create(
            name="No oracle", mode="solve", input_text="stmt", expected_text="",
            comparator="ratio", programming_language=self.lang, topic=self.topic,
        )
        run_id = "test-prompt-run-2"
        self._run_worker(run_id, [case], "program a; begin writeln(1); end.")
        run = PromptTestRun.objects.get(run_id=run_id)
        result = PromptTestResult.objects.get(run=run, test_case=case)
        self.assertEqual(result.verdict, PromptTestResult.VERDICT_SKIPPED)


# ===================================================================
# Tests for model sorting (top-2 user models + alphabetical rest)
# ===================================================================

class ModelSortingTests(SimpleTestCase):
    """Tests for _get_user_top_model_keys and model sorting in _render_ai_page."""

    def setUp(self):
        self.factory = RequestFactory()

    def _make_user(self, pk=1):
        return SimpleNamespace(
            is_authenticated=True,
            is_active=True,
            username="sort-user",
            pk=pk,
        )

    def test_top_model_keys_returns_empty_for_anonymous(self):
        """No user pk → empty list, no exception."""
        from ai.views import _get_user_top_model_keys
        request = self.factory.get("/ai/chat/")
        request.user = SimpleNamespace(is_authenticated=False, pk=None)
        result = _get_user_top_model_keys(request, limit=2)
        self.assertEqual(result, [])

    def test_top_model_keys_returns_empty_for_string_user(self):
        """String user (no pk) → empty list."""
        from ai.views import _get_user_top_model_keys
        request = self.factory.get("/ai/chat/")
        request.user = "some-session-string"
        result = _get_user_top_model_keys(request, limit=2)
        self.assertEqual(result, [])

    def test_top_model_keys_returns_empty_when_no_logs(self):
        """User with no AIRequestLog entries → empty list."""
        from ai.views import _get_user_top_model_keys
        request = self.factory.get("/ai/chat/")
        request.user = self._make_user(pk=999)
        result = _get_user_top_model_keys(request, limit=2)
        self.assertEqual(result, [])

    def test_user_top_models_come_first(self):
        """User's top-2 models first, then the rest strictly alphabetical (no web priority)."""
        from ai.views import _render_ai_page
        models_data = [
            {"key": "DeepSeek_V3_1", "title": "DeepSeek V3.1", "capabilities": {}},
            {"key": "Groq_Llama_3_3_70B", "title": "Groq Llama 3.3 70B", "capabilities": {}},
            {"key": "Web_DeepSeek", "title": "Web DeepSeek", "capabilities": {}},
            {"key": "Gemma_3_12b_it", "title": "Gemma 3 12b IT", "capabilities": {}},
            {"key": "MiniMax_M2_5", "title": "MiniMax M2.5", "capabilities": {}},
        ]
        request = self.factory.get("/ai/chat/")
        request.user = self._make_user(pk=1)
        request.session = {}
        request.user_info = {"userId": "sort-user"}
        request.COOKIES = {"userId": "sort-user"}
        with patch("ai.views._has_page_access", return_value=True),              patch("ai.views.AIAppSettings.get_solo", return_value=SimpleNamespace(is_enabled=True)),              patch("ai.views.get_available_model_options", return_value=models_data),              patch("ai.views._get_user_top_model_keys", return_value=["Groq_Llama_3_3_70B", "DeepSeek_V3_1"]),              patch("ai.views.render", return_value=HttpResponse("ok")) as mock_render:
            _render_ai_page(request, "ai/chat.html")
        context = mock_render.call_args[0][2]
        keys = [m["key"] for m in context["available_models"]]
        self.assertEqual(keys[0], "Groq_Llama_3_3_70B")
        self.assertEqual(keys[1], "DeepSeek_V3_1")
        self.assertEqual(keys[2], "Gemma_3_12b_it")
        self.assertEqual(keys[3], "MiniMax_M2_5")
        self.assertEqual(keys[4], "Web_DeepSeek")

    def test_no_user_top_models_strictly_alphabetical(self):
        """Without user top models: everything strictly alphabetical by title."""
        from ai.views import _render_ai_page
        models_data = [
            {"key": "DeepSeek_V3_1", "title": "DeepSeek V3.1", "capabilities": {}},
            {"key": "Web_DeepSeek", "title": "Web DeepSeek", "capabilities": {}},
            {"key": "Web_DeepSeek_Thinking", "title": "Web DeepSeek Thinking", "capabilities": {}},
            {"key": "Gemma_3_12b_it", "title": "Gemma 3 12b IT", "capabilities": {}},
            {"key": "MiniMax_M2_5", "title": "MiniMax M2.5", "capabilities": {}},
        ]
        request = self.factory.get("/ai/chat/")
        request.user = self._make_user(pk=1)
        request.session = {}
        request.user_info = {"userId": "sort-user"}
        request.COOKIES = {"userId": "sort-user"}
        with patch("ai.views._has_page_access", return_value=True),              patch("ai.views.AIAppSettings.get_solo", return_value=SimpleNamespace(is_enabled=True)),              patch("ai.views.get_available_model_options", return_value=models_data),              patch("ai.views._get_user_top_model_keys", return_value=[]),              patch("ai.views.render", return_value=HttpResponse("ok")) as mock_render:
            _render_ai_page(request, "ai/chat.html")
        context = mock_render.call_args[0][2]
        keys = [m["key"] for m in context["available_models"]]
        self.assertEqual(keys[0], "DeepSeek_V3_1")
        self.assertEqual(keys[1], "Gemma_3_12b_it")
        self.assertEqual(keys[2], "MiniMax_M2_5")
        self.assertEqual(keys[3], "Web_DeepSeek")
        self.assertEqual(keys[4], "Web_DeepSeek_Thinking")

    def test_user_top_first_no_duplicates(self):
        """User's top models first, the rest strictly alphabetical, no duplicate keys."""
        from ai.views import _render_ai_page
        models_data = [
            {"key": "Web_DeepSeek", "title": "Web DeepSeek", "capabilities": {}},
            {"key": "Web_DeepSeek_Thinking", "title": "Web DeepSeek Thinking", "capabilities": {}},
            {"key": "Gemma_3_12b_it", "title": "Gemma 3 12b IT", "capabilities": {}},
            {"key": "MiniMax_M2_5", "title": "MiniMax M2.5", "capabilities": {}},
        ]
        request = self.factory.get("/ai/chat/")
        request.user = self._make_user(pk=1)
        request.session = {}
        request.user_info = {"userId": "sort-user"}
        request.COOKIES = {"userId": "sort-user"}
        with patch("ai.views._has_page_access", return_value=True),              patch("ai.views.AIAppSettings.get_solo", return_value=SimpleNamespace(is_enabled=True)),              patch("ai.views.get_available_model_options", return_value=models_data),              patch("ai.views._get_user_top_model_keys", return_value=["Web_DeepSeek", "Gemma_3_12b_it"]),              patch("ai.views.render", return_value=HttpResponse("ok")) as mock_render:
            _render_ai_page(request, "ai/chat.html")
        context = mock_render.call_args[0][2]
        keys = [m["key"] for m in context["available_models"]]
        self.assertEqual(keys[0], "Web_DeepSeek")
        self.assertEqual(keys[1], "Gemma_3_12b_it")
        self.assertEqual(keys[2], "MiniMax_M2_5")
        self.assertEqual(keys[3], "Web_DeepSeek_Thinking")
        self.assertEqual(len(keys), len(set(keys)))


# ===================================================================
# Tests for problem-data API (languages, topics, prompts)
# ===================================================================

class ProblemDataApiTests(TestCase):
    """Verify that get_problem_data returns correct data for selection."""

    def setUp(self):
        self.factory = RequestFactory()
        self.user = get_user_model().objects.create_user(username="pd_user", password="test-pass")

    def _request(self, ui_language=""):
        params = {}
        if ui_language:
            params["ui_language"] = ui_language
        request = self.factory.get("/ai/api/problem-data/", data=params)
        request.user = self.user
        request.user_info = {"userId": self.user.username}
        request.COOKIES = {"userId": self.user.username}
        return request

    def test_problem_data_returns_languages(self):
        ProgrammingLanguage.objects.create(language_name="Python")
        ProgrammingLanguage.objects.create(language_name="C++")
        response = get_problem_data(self._request("Russian"))
        data = json.loads(response.content)
        lang_names = [l["language_name"] for l in data["languages"]]
        self.assertIn("Python", lang_names)
        self.assertIn("C++", lang_names)
        self.assertGreaterEqual(len(data["languages"]), 2)

    def test_problem_data_returns_topics_filtered_by_language(self):
        pl1 = ProgrammingLanguage.objects.create(language_name="Python")
        pl2 = ProgrammingLanguage.objects.create(language_name="C++")
        Topic.objects.create(topic_name="Loops", programming_language=pl1)
        Topic.objects.create(topic_name="Arrays", programming_language=pl1)
        Topic.objects.create(topic_name="Pointers", programming_language=pl2)
        response = get_problem_data(self._request("Russian"))
        data = json.loads(response.content)
        self.assertEqual(len(data["topics"]), 3)
        # Check topics are linked to correct languages
        py_topics = [t for t in data["topics"] if t["programming_language"] == pl1.id]
        self.assertEqual(len(py_topics), 2)
        cpp_topics = [t for t in data["topics"] if t["programming_language"] == pl2.id]
        self.assertEqual(len(cpp_topics), 1)

    def test_problem_data_returns_prompts(self):
        pl = ProgrammingLanguage.objects.create(language_name="Python")
        topic = Topic.objects.create(topic_name="Loops", programming_language=pl)
        Prompt.objects.create(topic=topic, prompt_name="Loop helper", prompt_text="Help with loops")
        response = get_problem_data(self._request("Russian"))
        data = json.loads(response.content)
        self.assertGreaterEqual(len(data["prompts"]), 1)
        self.assertEqual(data["prompts"][0]["prompt_name"], "Loop helper")

    def test_problem_data_requires_auth(self):
        request = self.factory.get("/ai/api/problem-data/")
        request.user = AnonymousUser()
        request.user_info = None
        request.COOKIES = {}
        response = get_problem_data(request)
        self.assertEqual(response.status_code, 403)

    def test_problem_data_returns_shared_prompts(self):
        pl = ProgrammingLanguage.objects.create(language_name="Python")
        sp = SharedPrompt.objects.create(prompt_name="Common prep", prompt_text="Common text")
        sp.programming_languages.add(pl)
        response = get_problem_data(self._request("Russian"))
        data = json.loads(response.content)
        self.assertGreaterEqual(len(data["shared_prompts"]), 1)
        self.assertEqual(data["shared_prompts"][0]["prompt_name"], "Common prep")

    def test_problem_data_response_has_all_keys(self):
        response = get_problem_data(self._request("Russian"))
        data = json.loads(response.content)
        self.assertIn("languages", data)
        self.assertIn("topics", data)
        self.assertIn("prompts", data)
        self.assertIn("shared_prompts", data)


# ===================================================================
# Tests for _get_user_top_model_keys with real AIRequestLog data
# ===================================================================

class UserTopModelKeysTests(TestCase):
    """Test _get_user_top_model_keys with real AIRequestLog records."""

    def setUp(self):
        from django.core.cache import cache
        # LocMem-кеш не откатывается между тестами с TestCase, а несколько тестов
        # ниже используют одного user_id=42 с разным набором логов — без очистки
        # кеш _get_user_top_model_keys возвращал бы результат предыдущего теста.
        cache.clear()
        self.factory = RequestFactory()
        self.user = get_user_model().objects.create_user(
            username="freq_user", password="test-pass", pk=42,
        )
        # Считаем всю историю: сбрасываем дату-отсечку фаворитов, чтобы
        # созданные ниже логи (sent_at ≈ now) не отфильтровывались epoch-фильтром
        # в _get_user_top_model_keys (по умолчанию favorites_epoch = now).
        from ai.models import AIAppSettings
        settings = AIAppSettings.get_solo()
        settings.favorites_epoch = None
        settings.save(update_fields=["favorites_epoch"])

    def _make_request(self, user=None):
        request = self.factory.get("/ai/chat/")
        request.user = user or self.user
        request.session = {}
        return request

    def _make_log(self, model_names, status="success"):
        AIRequestLog.objects.create(
            user=self.user,
            username=self.user.username,
            external_user_id="42",
            user_full_name="Freq User",
            client_id="client-1",
            source=AIRequestLog.SOURCE_WEBSOCKET,
            mode=AIRequestLog.MODE_CHAT,
            sent_at=timezone.now(),
            model_names=model_names,
            message="test",
            status=status,
        )

    def test_returns_most_frequent_models(self):
        """The most frequently used models should be returned in order."""
        from ai.views import _get_user_top_model_keys
        from ai.model_clients import registry

        # Get actual model titles from registry
        all_keys = list(registry.keys())
        if len(all_keys) < 3:
            self.skipTest("Need at least 3 models in registry")
        key_a = all_keys[0]
        key_b = all_keys[1]
        key_c = all_keys[2]
        title_a = registry.title(key_a)
        title_b = registry.title(key_b)
        title_c = registry.title(key_c)

        # Create logs: key_a used 3 times, key_b used 2 times, key_c used 1 time
        for _ in range(3):
            self._make_log([title_a])
        for _ in range(2):
            self._make_log([title_b])
        for _ in range(1):
            self._make_log([title_c])

        result = _get_user_top_model_keys(self._make_request(), limit=2)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0], key_a)
        self.assertEqual(result[1], key_b)

    def test_error_logs_are_excluded(self):
        """Only success logs should be counted."""
        from ai.views import _get_user_top_model_keys
        from ai.model_clients import registry

        all_keys = list(registry.keys())
        if len(all_keys) < 2:
            self.skipTest("Need at least 2 models in registry")
        key_a = all_keys[0]
        title_a = registry.title(key_a)

        # Error log should NOT be counted
        self._make_log([title_a], status="error")
        # Success log should be counted
        self._make_log([title_a], status="success")

        result = _get_user_top_model_keys(self._make_request(), limit=2)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0], key_a)

    def test_limit_respected(self):
        """Should respect the limit parameter."""
        from ai.views import _get_user_top_model_keys
        from ai.model_clients import registry

        all_keys = list(registry.keys())
        if len(all_keys) < 5:
            self.skipTest("Need at least 5 models in registry")
        for i in range(5):
            key = all_keys[i]
            self._make_log([registry.title(key)])

        result = _get_user_top_model_keys(self._make_request(), limit=3)
        self.assertEqual(len(result), 3)

    def test_other_users_logs_excluded(self):
        """Logs from other users should NOT affect this user's top models."""
        from ai.views import _get_user_top_model_keys
        from ai.model_clients import registry

        all_keys = list(registry.keys())
        if len(all_keys) < 2:
            self.skipTest("Need at least 2 models in registry")
        key_a = all_keys[0]
        key_b = all_keys[1]
        title_a = registry.title(key_a)
        title_b = registry.title(key_b)

        # Other user's logs
        other_user = get_user_model().objects.create_user(
            username="other_freq_user", password="test-pass", pk=99,
        )
        for _ in range(5):
            AIRequestLog.objects.create(
                user=other_user,
                username=other_user.username,
                external_user_id="99",
                user_full_name="Other User",
                client_id="client-2",
                source=AIRequestLog.SOURCE_WEBSOCKET,
                mode=AIRequestLog.MODE_CHAT,
                sent_at=timezone.now(),
                model_names=[title_b],
                message="test",
                status="success",
            )
        # This user's logs
        self._make_log([title_a])

        result = _get_user_top_model_keys(self._make_request(), limit=2)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0], key_a)

    def test_result_is_cached(self):
        """Результат кешируется per-user (TTL 120 c): повторный вызов без
        инвалидации возвращает закешированный список; после cache.clear() —
        пересчитывается по актуальным логам.
        """
        from ai.views import _get_user_top_model_keys
        from ai.model_clients import registry
        from django.core.cache import cache

        all_keys = list(registry.keys())
        if len(all_keys) < 2:
            self.skipTest("Need at least 2 models in registry")
        key_a = all_keys[0]
        key_b = all_keys[1]
        title_a = registry.title(key_a)
        title_b = registry.title(key_b)

        # Сначала key_a — единственный, он и попадёт в топ.
        for _ in range(2):
            self._make_log([title_a])
        result1 = _get_user_top_model_keys(self._make_request(), limit=2)
        self.assertEqual(result1, [key_a])

        # Добавляем много логов с key_b, но кеш ещё валиден (TTL 120 c) —
        # повторный вызов должен вернуть закешированный [key_a], не key_b.
        for _ in range(5):
            self._make_log([title_b])
        result2 = _get_user_top_model_keys(self._make_request(), limit=2)
        self.assertEqual(result2, [key_a])

        # После очистки кеша — пересчёт: теперь key_b встречается чаще.
        cache.clear()
        result3 = _get_user_top_model_keys(self._make_request(), limit=2)
        self.assertEqual(result3[0], key_b)


# ===================================================================
# Tests for admin Updates section (UpdateLog)
# ===================================================================

class UpdateLogAdminTests(TestCase):
    """Tests for the admin updates view: access control, filtering, search."""

    def setUp(self):
        self.factory = RequestFactory()
        # Очищаем данные от миграции 0026, чтобы тесты были изолированы
        UpdateLog.objects.all().delete()
        self.superuser = get_user_model().objects.create_superuser(
            username="upd_admin", password="***", email="admin@test.com",
        )
        self.normal_user = get_user_model().objects.create_user(
            username="upd_normal", password="***",
        )
        # Add to prompt_developer group (non-superuser staff-like)
        from ai.constants import PROMPT_DEVELOPER_GROUP
        group, _ = Group.objects.get_or_create(name=PROMPT_DEVELOPER_GROUP)
        self.normal_user.groups.add(group)

        # Create some test data
        self.u1 = UpdateLog.objects.create(
            commit_date="2026-07-27", description="Нововведение: test feature",
            author="whmi1", commit_hash="abc123",
        )
        self.u2 = UpdateLog.objects.create(
            commit_date="2026-07-11", description="Исправление: bug fix",
            author="Archi", commit_hash="def456",
        )
        self.u3 = UpdateLog.objects.create(
            commit_date="2026-06-22", description="fix build v2",
            author="ArchiBaldEgo", commit_hash="ghi789",
        )

    def _make_request(self, user=None, params=None):
        request = self.factory.get("/ai/admin/updates/", data=params or {})
        request.user = user or self.superuser
        request.user_info = {"userId": getattr(user, "username", "test")}
        request.COOKIES = {"userId": getattr(user, "username", "test")}
        request.session = {"admin_fresh_auth": True}
        return request

    def _call_view(self, request):
        """Call admin_updates_view bypassing admin_view decorator checks."""
        from ai.admin.updates import admin_updates_view
        # The view is wrapped by admin_view which checks DLSID auth.
        # We need to bypass that for testing — call the inner function directly.
        import ai.admin.site as site_module
        original_admin_view = site_module.ai_admin_site.admin_view

        # Temporarily replace admin_view with a passthrough
        def passthrough_admin_view(view, cacheable=False):
            return view
        site_module.ai_admin_site.admin_view = passthrough_admin_view

        # Re-import to get the unwrapped view
        import importlib
        import ai.admin.updates as updates_module
        importlib.reload(updates_module)
        response = updates_module.admin_updates_view(request)

        # Restore
        site_module.ai_admin_site.admin_view = original_admin_view
        return response

    def test_superuser_can_access(self):
        """Superuser should see the updates page."""
        from ai.admin.updates import admin_updates_view
        response = self._call_view(self._make_request(self.superuser))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Обновления")

    def test_non_superuser_forbidden(self):
        """Non-superuser should get 403."""
        from ai.admin.updates import admin_updates_view
        response = self._call_view(self._make_request(self.normal_user))
        self.assertEqual(response.status_code, 403)

    def test_filter_by_author(self):
        """Filtering by author should return only that author's commits."""
        request = self._make_request(self.superuser, {"author": "Archi"})
        response = self._call_view(request)
        self.assertEqual(response.status_code, 200)
        # The filtered results should contain Archi's commit
        self.assertContains(response, "Archi")
        # Total count should be 1 (only Archi's commit)
        self.assertContains(response, "записей: 1")

    def test_search_by_description(self):
        """Text search should match description."""
        from ai.admin.updates import admin_updates_view
        request = self._make_request(self.superuser, {"q": "test feature"})
        response = self._call_view(request)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "test feature")
        self.assertNotContains(response, "bug fix")

    def test_filter_by_date_range(self):
        """Date range filter should narrow results."""
        request = self._make_request(self.superuser, {
            "date_from": "2026-07-01", "date_to": "2026-07-31",
        })
        response = self._call_view(request)
        self.assertEqual(response.status_code, 200)
        # Should include whmi1 (2026-07-27) and Archi (2026-07-11)
        self.assertContains(response, "whmi1")
        self.assertContains(response, "Archi")
        # Total count should be 2 (both in July)
        self.assertContains(response, "записей: 2")

    def test_pagination_works(self):
        """Page parameter should not crash."""
        from ai.admin.updates import admin_updates_view
        request = self._make_request(self.superuser, {"page": "1"})
        response = self._call_view(request)
        self.assertEqual(response.status_code, 200)

    def test_combined_filter(self):
        """Author + date + search combined should work."""
        from ai.admin.updates import admin_updates_view
        request = self._make_request(self.superuser, {
            "author": "Archi", "date_from": "2026-07-01", "q": "bug",
        })
        response = self._call_view(request)
        self.assertEqual(response.status_code, 200)

    def test_empty_results(self):
        """Search with no matches should show 'no records' message."""
        from ai.admin.updates import admin_updates_view
        request = self._make_request(self.superuser, {"q": "nonexistent_xyz"})
        response = self._call_view(request)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Нет записей")


class UpdateLogModelTests(TestCase):
    """Tests for the UpdateLog model itself."""

    def setUp(self):
        UpdateLog.objects.all().delete()

    def test_ordering_newest_first(self):
        """UpdateLog should be ordered by commit_date descending."""
        UpdateLog.objects.create(
            commit_date="2026-06-01", description="old", author="A", commit_hash="old1",
        )
        UpdateLog.objects.create(
            commit_date="2026-07-28", description="new", author="B", commit_hash="new1",
        )
        first = UpdateLog.objects.first()
        self.assertEqual(first.commit_hash, "new1")

    def test_str_representation(self):
        """__str__ should contain date, author, and truncated description."""
        entry = UpdateLog.objects.create(
            commit_date="2026-07-28", description="A" * 100, author="whmi1", commit_hash="h1",
        )
        s = str(entry)
        self.assertIn("2026-07-28", s)
        self.assertIn("whmi1", s)
        # Description truncated to 80 chars in __str__
        self.assertIn("A" * 80, s)
        self.assertNotIn("A" * 81, s)


class LastUpdateDateCacheTests(TestCase):
    """Tests for _get_last_update_date with caching."""

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        UpdateLog.objects.all().delete()

    def test_returns_none_when_empty(self):
        """No UpdateLog records → None."""
        from ai.views import _get_last_update_date
        result = _get_last_update_date()
        self.assertIsNone(result)

    def test_returns_date_when_populated(self):
        """Most recent commit_date should be returned."""
        from ai.views import _get_last_update_date
        UpdateLog.objects.create(
            commit_date="2026-07-28", description="test", author="A", commit_hash="h1",
        )
        UpdateLog.objects.create(
            commit_date="2026-06-01", description="old", author="B", commit_hash="h2",
        )
        result = _get_last_update_date()
        self.assertEqual(str(result), "2026-07-28")

    def test_caches_result(self):
        """Second call should use cache (no DB hit).
        With post_save/post_delete signals, cache is invalidated on change,
        so we test caching without deleting records (signals clear cache).
        """
        from ai.views import _get_last_update_date
        from django.core.cache import cache
        cache.clear()
        UpdateLog.objects.create(
            commit_date="2026-07-28", description="test", author="A", commit_hash="h1",
        )
        # First call populates cache (post_save signal clears it, then this sets it)
        result1 = _get_last_update_date()
        self.assertEqual(str(result1), "2026-07-28")
        # Second call should return cached value without DB hit
        result2 = _get_last_update_date()
        self.assertEqual(str(result2), "2026-07-28")
        # Clean up
        cache.clear()


class AvailableModelOptionsCacheTests(TestCase):
    """Tests for get_available_model_options caching + AIModelAvailability signal.

    Непустой результат кешируется на 30 c; пустой — нет (чтобы self-heal в
    _render_ai_page продолжал срабатывать). Инвалидация — сигналом
    post_save/post_delete на AIModelAvailability.
    """

    def setUp(self):
        from django.core.cache import cache
        from ai.models import AIModelAvailability
        cache.clear()
        AIModelAvailability.objects.all().delete()

    def _make_row(self, key, window_date):
        from ai.models import AIModelAvailability
        return AIModelAvailability.objects.create(
            model_key=key, model_title=key, is_available=True,
            window_date=window_date, last_message="ok",
        )

    def test_caches_nonempty_result(self):
        from ai.model_health import (
            get_available_model_options, get_health_window_date, MODEL_CATALOG_KEYS,
        )
        from django.core.cache import cache
        if not MODEL_CATALOG_KEYS:
            self.skipTest("No models in registry")
        window_date = get_health_window_date()
        key = MODEL_CATALOG_KEYS[0]
        self._make_row(key, window_date)
        cache.clear()  # create triggered the invalidation signal; be explicit

        result1 = get_available_model_options()
        self.assertEqual([m["key"] for m in result1], [key])

        # Second call served from cache → no DB queries.
        with self.assertNumQueries(0):
            result2 = get_available_model_options()
        self.assertEqual(result2, result1)

    def test_empty_result_not_cached(self):
        """Empty window → [] is NOT cached, so self-heal in _render_ai_page still fires."""
        from ai.model_health import get_available_model_options
        from ai.constants import AI_CACHE_KEY_PREFIX
        from django.core.cache import cache
        cache.clear()
        result = get_available_model_options()
        self.assertEqual(result, [])
        self.assertIsNone(cache.get(f"{AI_CACHE_KEY_PREFIX}:available_models"))

    def test_signal_invalidates_on_new_row(self):
        from ai.model_health import (
            get_available_model_options, get_health_window_date, MODEL_CATALOG_KEYS,
        )
        from django.core.cache import cache
        if len(MODEL_CATALOG_KEYS) < 2:
            self.skipTest("Need at least 2 models in registry")
        window_date = get_health_window_date()
        key_a, key_b = MODEL_CATALOG_KEYS[0], MODEL_CATALOG_KEYS[1]
        self._make_row(key_a, window_date)
        cache.clear()
        result1 = get_available_model_options()
        self.assertEqual([m["key"] for m in result1], [key_a])

        # Adding a second available model triggers post_save → cache invalidated.
        self._make_row(key_b, window_date)
        result2 = get_available_model_options()
        self.assertEqual({m["key"] for m in result2}, {key_a, key_b})


# ===================================================================
# Tests for Ollama provider (cloud models, plain chat, no tools)
# ===================================================================

class OllamaRegistryTests(SimpleTestCase):
    """Ollama-модели зарегистрированы в registry с правильными capabilities."""

    OLLAMA_KEYS = [
        "Ollama_Glm_5_2_Cloud",
        "Ollama_Glm_5_3_Flash_Cloud",
        "Ollama_Gemma_4_Cloud",
        "Ollama_Qwen_3_5_Cloud",
        "Ollama_Nemotron_3_Super_Cloud",
        "Ollama_Kimi_K2_7_Code_Cloud",
        "Ollama_Kimi_K2_6_Cloud",
        "Ollama_DeepSeek_V4_Flash_Cloud",
    ]

    # glm-5.3-flash:cloud стримит отдельное поле thinking → reasoning-модель.
    REASONING_KEYS = {"Ollama_Glm_5_3_Flash_Cloud"}

    def _expected_caps(self, key):
        return {"text": True, "vision": False, "reasoning": key in self.REASONING_KEYS}

    def test_registry_contains_ollama_models(self):
        from ai.model_clients import registry, ollama
        for key in self.OLLAMA_KEYS:
            self.assertIsNotNone(registry.get(key), f"Missing registry entry for {key}")
            self.assertTrue(callable(registry.handler(key)), f"No handler for {key}")
            self.assertEqual(
                registry.capabilities(key),
                self._expected_caps(key),
                f"Wrong capabilities for {key}",
            )
            self.assertTrue(
                callable(getattr(ollama, f"ask_{key}_async", None)),
                f"Missing ollama.ask_{key}_async",
            )

    def test_ollama_table_and_registry_in_sync(self):
        """Каждая модель OLLAMA_MODELS есть в registry, и наоборот — без рассинхрона."""
        from ai.model_clients import registry, ollama
        table_keys = set(ollama.OLLAMA_MODELS)
        for key in self.OLLAMA_KEYS:
            self.assertIn(key, table_keys, f"{key} missing from OLLAMA_MODELS table")
        for key in table_keys:
            self.assertIsNotNone(
                registry.get(key),
                f"OLLAMA_MODELS entry {key} is not registered in the model registry",
            )

    def test_ollama_models_table_matches_registry(self):
        from ai.model_clients import registry, ollama
        for key in self.OLLAMA_KEYS:
            self.assertIn(key, ollama.OLLAMA_MODELS)
            self.assertEqual(registry.handler(key), getattr(ollama, f"ask_{key}_async"))

    def test_module_defines_logger(self):
        import logging as _logging
        from ai.model_clients import ollama
        self.assertTrue(hasattr(ollama, "logger"))
        self.assertIsInstance(ollama.logger, _logging.Logger)


class OllamaHandlerTests(SimpleTestCase):
    """Handler вызывает ollama.Client.chat и возвращает (content, tokens, is_error)."""

    async def test_handler_returns_content_and_tokens(self):
        from ai.model_clients import ollama
        with patch("ai.model_clients.ollama.OLLAMA_API_KEY", "test-key"), \
             patch("ai.model_clients.ollama.OLLAMA_HOST", "https://api.ollama.com"), \
             patch("ai.model_clients.ollama.Client") as mock_client_cls:
            mock_client = mock_client_cls.return_value
            mock_resp = MagicMock()
            mock_resp.message.content = "2"
            mock_resp.eval_count = 5
            mock_client.chat.return_value = mock_resp

            # Хендлеры генерируются динамически (make_table_handlers) — getattr,
            # чтобы линтер не ругался на отсутствующий атрибут.
            glm_5_2 = getattr(ollama, "ask_Ollama_Glm_5_2_Cloud_async")
            result = await glm_5_2("1+1=?", "client")
            self.assertEqual(result, ("2", 5, False))
            mock_client.chat.assert_called_once()
            # Без tools= (обычный чат).
            _, kwargs = mock_client.chat.call_args
            self.assertNotIn("tools", kwargs)

    async def test_glm_5_3_flash_uses_cloud_model_id(self):
        """GLM 5.3 Flash ходит через ollama.chat с model='glm-5.3-flash:cloud'."""
        from ai.model_clients import ollama
        with patch("ai.model_clients.ollama.OLLAMA_API_KEY", "test-key"), \
             patch("ai.model_clients.ollama.OLLAMA_HOST", "https://api.ollama.com"), \
             patch("ai.model_clients.ollama.Client") as mock_client_cls:
            mock_client = mock_client_cls.return_value
            mock_resp = MagicMock()
            mock_resp.message.content = "ok"
            mock_resp.eval_count = 1
            mock_client.chat.return_value = mock_resp

            glm_5_3_flash = getattr(ollama, "ask_Ollama_Glm_5_3_Flash_Cloud_async")
            result = await glm_5_3_flash("hi", "client")
            self.assertEqual(result, ("ok", 1, False))
            _, kwargs = mock_client.chat.call_args
            self.assertEqual(kwargs.get("model"), "glm-5.3-flash:cloud")

    async def test_cloud_guard_without_api_key(self):
        """Cloud host + пустой OLLAMA_API_KEY → guard-сообщение, без вызова Client."""
        from ai.model_clients import ollama
        with patch("ai.model_clients.ollama.OLLAMA_API_KEY", ""), \
             patch("ai.model_clients.ollama.OLLAMA_HOST", "https://api.ollama.com"), \
             patch("ai.model_clients.ollama.Client") as mock_client_cls:
            qwen_3_5 = getattr(ollama, "ask_Ollama_Qwen_3_5_Cloud_async")
            result = await qwen_3_5("hi", "client")
            self.assertIn("Ollama API ключ не настроен", result[0])
            mock_client_cls.assert_not_called()


class SendSolutionCourseIdTests(SimpleTestCase):
    """``send_solution_to_dl`` должна передавать обязательное по контракту REST
    API поле ``courseId`` в JSON-теле. Без него dl.gsu.by отдаёт 400 Bad Request
    (ручная отправка через веб-форму работает, т.к. курс уже в URL страницы)."""

    def test_body_includes_course_id_field(self):
        from ai import dl_api_client

        captured = {}

        class FakeResponse:
            status_code = 200
            content = b'{"queueId": 42, "message": "ok"}'

        def fake_dl_request(method, path, **kwargs):
            captured["method"] = method
            captured["path"] = path
            captured["json"] = kwargs.get("json")
            return FakeResponse()

        with patch("ai.dl_api_client._dl_request", fake_dl_request):
            data = dl_api_client.send_solution_to_dl(
                "SID-1", 2606747, "program a;", ".mpc", course_id=1450,
            )

        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["path"], "/restapi/send-solution")
        body = captured["json"]
        self.assertIsNotNone(body)
        self.assertEqual(body["sessionId"], "SID-1")
        self.assertEqual(body["nodeId"], 2606747)
        self.assertEqual(body["code"], "program a;")
        self.assertEqual(body["fileExtension"], ".mpc")
        # Ключевое требование контракта: courseId присутствует в теле.
        self.assertEqual(body["courseId"], 1450)
        self.assertEqual(data, {"queueId": 42, "message": "ok"})

    def test_default_course_id_is_zero(self):
        from ai import dl_api_client

        captured = {}

        class FakeResponse:
            status_code = 200
            content = b'{"queueId": 1, "message": "ok"}'

        def fake_dl_request(method, path, **kwargs):
            captured["json"] = kwargs.get("json")
            return FakeResponse()

        with patch("ai.dl_api_client._dl_request", fake_dl_request):
            dl_api_client.send_solution_to_dl("SID", 100, "code", ".cpp")

        # Обратная совместимость: без явного course_id поле всё равно есть (=0).
        self.assertEqual(captured["json"]["courseId"], 0)

    def test_payload_context_includes_course_id(self):
        from ai.dl_api_client import _send_solution_payload_context

        ctx = _send_solution_payload_context(2606747, "code...", ".mpc", 1450)
        self.assertIn("nodeId=2606747", ctx)
        self.assertIn("courseId=1450", ctx)
        self.assertIn("fileExtension='.mpc'", ctx)


class EmptyResponseDetectionTests(SimpleTestCase):
    """Пустой ответ модели должен детектиться на уровне провайдера и
    возвращаться как 3-кортеж ``is_error=True`` — осмысленное сообщение вместо
    «успеха» с пустым текстом или плейсхолдером, обходящим нижестоящие гарды."""

    def test_extract_choice_content_returns_empty_not_placeholder(self):
        from ai.model_clients.exceptions import extract_choice_content

        self.assertEqual(extract_choice_content({"choices": [{"message": {"content": ""}}]}), "")
        self.assertEqual(extract_choice_content({"choices": [{"message": {}}]}), "")
        self.assertEqual(extract_choice_content({}), "")
        # Плейсхолдер «Пустой ответ от модели.» удалён — он обходил empty-гарды.
        for obj in (
            {"choices": [{"message": {"content": ""}}]},
            {"choices": [{"message": {}}]},
            {},
        ):
            self.assertNotIn("Пустой ответ", extract_choice_content(obj))

    def test_sambanova_empty_returns_error_3tuple(self):
        from ai.model_clients import sambanova

        class FakeResponse:
            status_code = 200
            text = '{"choices": [{"message": {"content": ""}}], "usage": {"completion_tokens": 0}}'

        with patch("ai.model_clients.sambanova.requests") as mock_req:
            mock_req.post.return_value = FakeResponse()
            mock_req.exceptions = __import__("requests").exceptions
            result = sambanova._ask_sambanova_model_async("hi", 0, "Model", max_tokens=8)
            content, tokens, is_error = async_to_sync_runner(result)
        self.assertTrue(is_error)
        self.assertEqual(tokens, 0)
        self.assertIn("пустой ответ", content.lower())

    def test_sambanova_nonempty_returns_2tuple_success(self):
        from ai.model_clients import sambanova

        class FakeResponse:
            status_code = 200
            text = ('{"choices": [{"message": {"content": "42"}}], '
                    '"usage": {"completion_tokens": 3}}')

        with patch("ai.model_clients.sambanova.requests") as mock_req:
            mock_req.post.return_value = FakeResponse()
            mock_req.exceptions = __import__("requests").exceptions
            result = sambanova._ask_sambanova_model_async("hi", 0, "Model", max_tokens=8)
            content, tokens = async_to_sync_runner(result)
        self.assertEqual(content, "42")
        self.assertEqual(tokens, 3)

    def test_groq_empty_returns_error_3tuple(self):
        from ai.model_clients import groq

        class FakeResponse:
            status_code = 200
            text = '{"choices": [{"message": {"content": ""}}], "usage": {"total_tokens": 0}}'
            headers = {}

            def json(self):
                import json as _json
                return _json.loads(self.text)

        async def fake_post(url, **kw):
            return FakeResponse()

        with patch("ai.model_clients.groq.GROQ_TOKEN", "tok"), \
                patch("ai.model_clients.groq.httpx.AsyncClient") as mock_cls:
            mock_client = MagicMock()
            mock_client.post = fake_post
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_cls.return_value = mock_client
            result = groq._ask_groq("groq-model", "hi", 0, model_key="Groq_Test")
            content, tokens, is_error = async_to_sync_runner(result)
        self.assertTrue(is_error)
        self.assertEqual(tokens, 0)
        self.assertIn("пустой ответ", content.lower())
        self.assertIn("groq-model", content)


class BatchSolveModeAndCancelModelTests(SimpleTestCase):
    """Модельные константы/поля для режима «Пакетное решение» (F4) и
    мгновенного прерывания (F2): режим лога, статус прогона, поле course_id."""

    def test_ai_request_log_has_batch_solve_mode(self):
        from ai.models import AIRequestLog
        self.assertEqual(AIRequestLog.MODE_BATCH_SOLVE, "batch_solve")
        choices = dict(AIRequestLog.MODE_CHOICES)
        self.assertIn("batch_solve", choices)
        self.assertEqual(choices["batch_solve"], "Пакетное решение")

    def test_ai_model_test_run_has_cancelled_status(self):
        from ai.models import AIModelTestRun
        self.assertEqual(AIModelTestRun.STATUS_CANCELLED, "cancelled")
        choices = dict(AIModelTestRun.STATUS_CHOICES)
        self.assertIn("cancelled", choices)

    def test_ai_model_test_run_has_course_id_field(self):
        from ai.models import AIModelTestRun
        field = AIModelTestRun._meta.get_field("course_id")
        self.assertTrue(field.null)
        self.assertTrue(field.blank)


class BatchLogSnapshotTests(TestCase):
    """Детал batch-лога в Журнале запросов (F4): ``_is_batch_solve_log``
    распознаёт batch-прогоны (новый mode=batch_solve и старый mode=solve+
    sentinel), ``_build_batch_log_snapshot`` собирает снапшот мини-таблицы
    (run_id, course_id, file_extension, node_ids, results, report) из БД."""

    def setUp(self):
        from ai.models import AIModelTestRun, AIModelTestResult, Task
        self.user = get_user_model().objects.create_user(username="snap", password="x")
        self.lang = ProgrammingLanguage.objects.create(language_name="Pascal")
        self.topic = Topic.objects.create(topic_name="Линейные", programming_language=self.lang)
        self.t1 = Task.objects.create(
            node_id=5001, task_id=6001, name="A", statement="x",
            topic=self.topic, programming_language=self.lang, file_extension=".pas",
        )
        self.t2 = Task.objects.create(
            node_id=5002, task_id=6002, name="B", statement="y",
            topic=self.topic, programming_language=self.lang, file_extension=".pas",
        )
        self.run_id = "a" * 32  # 32-hex sentinel-совместимый
        self.test_run = AIModelTestRun.objects.create(
            run_id=self.run_id,
            run_type=AIModelTestRun.RUN_TYPE_BATCH,
            status=AIModelTestRun.STATUS_COMPLETED,
            course_id=1450,
        )
        for task, verdict in ((self.t1, "solved"), (self.t2, "failed")):
            AIModelTestResult.objects.create(
                run=self.test_run, task=task,
                model_key="FakeModel", model_title="FakeModel",
                status="ok", verdict=verdict,
                duration_seconds=1.0, tokens=10,
                short_response="r", raw_response="raw", code="code",
                dl_comment="c", dl_queue_id=1,
                file_extension_snapshot=".pas",
                topic_name_snapshot="Линейные", prog_lang_snapshot="Pascal",
            )

    def _make_log(self, mode, message):
        return AIRequestLog.objects.create(
            user=self.user, source="arm", mode=mode, message=message,
            status=AIRequestLog.STATUS_SUCCESS, sent_at=timezone.now(),
        )

    def test_is_batch_solve_log_recognizes_new_and_legacy(self):
        from ai.admin.logs import _is_batch_solve_log
        new_log = self._make_log("batch_solve", f"Batch solve run {self.run_id}")
        legacy_log = self._make_log("solve", f"Batch solve run {self.run_id}")
        plain_log = self._make_log("chat", "hello")
        self.assertTrue(_is_batch_solve_log(new_log))
        self.assertTrue(_is_batch_solve_log(legacy_log))
        self.assertFalse(_is_batch_solve_log(plain_log))

    def test_build_batch_log_snapshot_fields(self):
        from ai.admin.logs import _build_batch_log_snapshot
        log = self._make_log("batch_solve", f"Batch solve run {self.run_id}")
        snap = _build_batch_log_snapshot(log)
        self.assertIsNotNone(snap)
        self.assertEqual(snap["run_id"], self.run_id)
        self.assertEqual(snap["course_id"], 1450)
        self.assertEqual(snap["file_extension"], ".pas")
        self.assertEqual(sorted(snap["node_ids"]), [5001, 5002])
        self.assertEqual(len(snap["results"]), 2)
        # report считается из результатов: 1 решено, 1 не решено.
        self.assertEqual(snap["report"]["solved"], 1)
        self.assertEqual(snap["report"]["failed"], 1)

    def test_snapshot_none_when_run_missing(self):
        from ai.admin.logs import _build_batch_log_snapshot
        log = self._make_log("batch_solve", f"Batch solve run {'b' * 32}")
        self.assertIsNone(_build_batch_log_snapshot(log))


class ArmCancelImmediateDbFlipTests(TestCase):
    """F2: ``cancel_arm_run`` сразу переводит AIModelTestRun в STATUS_CANCELLED с
    finished_at, не дожидаясь in-flight вызова — UI может перестать поллить."""

    def test_cancel_flips_db_status_immediately(self):
        from ai import arm_runner
        from ai.models import AIModelTestRun

        run_id = "cancel-flip-1"
        run = AIModelTestRun.objects.create(
            run_id=run_id, run_type=AIModelTestRun.RUN_TYPE_BATCH,
            status=AIModelTestRun.STATUS_RUNNING,
        )
        # Эмулируем живой in-memory job (как делает start_batch_solve_run).
        arm_runner._jobs[run_id] = {
            "run_id": run_id, "status": "running", "cancel_requested": False,
            "cancelled": False, "results": [], "report": None,
        }
        try:
            arm_runner.cancel_arm_run(run_id)
        finally:
            arm_runner._jobs.pop(run_id, None)

        run.refresh_from_db()
        self.assertEqual(run.status, AIModelTestRun.STATUS_CANCELLED)
        self.assertIsNotNone(run.finished_at)
        self.assertEqual(run.error_message, "Прервано пользователем")

    def test_cancel_orphan_run_marks_cancelled(self):
        from ai import arm_runner
        from ai.models import AIModelTestRun

        run_id = "cancel-orphan-1"
        AIModelTestRun.objects.create(
            run_id=run_id, run_type=AIModelTestRun.RUN_TYPE_BATCH,
            status=AIModelTestRun.STATUS_RUNNING,
        )
        # В памяти job нет — orphan-ветка.
        self.assertNotIn(run_id, arm_runner._jobs)
        arm_runner.cancel_arm_run(run_id)
        run = AIModelTestRun.objects.get(run_id=run_id)
        self.assertEqual(run.status, AIModelTestRun.STATUS_CANCELLED)
        self.assertIsNotNone(run.finished_at)


# Хелпер: запустить async-корутину из синхронного test-метода (в sync-методах
# нет работающего event loop, поэтому asyncio.run безопасен).
def async_to_sync_runner(coro):
    import asyncio
    return asyncio.run(coro)
