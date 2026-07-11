from django.contrib.admin.sites import AdminSite
from django.contrib.auth import SESSION_KEY, get_user_model
from django.contrib.auth.models import AnonymousUser
from django.contrib.auth.models import Group
from django.contrib.sessions.middleware import SessionMiddleware
from django.test import SimpleTestCase, RequestFactory, TestCase, override_settings
from pathlib import Path
import json

from django.http import HttpResponse
from django.db import ProgrammingError
from django.utils import timezone
from asgiref.sync import sync_to_async
from unittest.mock import AsyncMock, MagicMock, patch
from types import SimpleNamespace

from ai.admin import PromptAdmin, PromptForm
from ai.middleware import ExternalAuthMiddleware
from ai.i18n import get_localized_name, get_ui_language_suffix
from ai.models import AIRequestLog, ExternalDLAccount, ProgrammingLanguage, Prompt, SharedPrompt, Topic
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

        with patch("ai.middleware.fetch_external_user_info") as fetch_user_info:
            response = self.middleware(request)

        self.assertEqual(response.status_code, 200)
        fetch_user_info.assert_not_called()
        self.assertEqual(request.user_info, {"userId": "42"})


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
        self.assertEqual(log.status, AIRequestLog.STATUS_SUCCESS)
        self.assertEqual(log.mode, AIRequestLog.MODE_CHAT)
        self.assertEqual(log.get_mode_display(), "Чат")
        self.assertEqual(log.programming_language_name, "Python")
        self.assertEqual(log.topic_name, "Loops")
        self.assertEqual(log.prompt_name, "Helper")


class ModelClientRegistryTests(SimpleTestCase):
    def test_registry_contains_expected_models(self):
        from ai.model_clients import registry

        expected_keys = {
            "DeepSeek_R1_Distill_Llama_70B",
            "DeepSeek_V3_1",
            "DeepSeek_V3_1_cb",
            "DeepSeek_V3_2",
            "Llama_4_Maverick_17B_128E_Instruct",
            "Meta_Llama_3_3_70B_Instruct",
            "MiniMax_M2_5",
            "MiniMax_M2_7",
            "Gemma_3_12b_it",
            "Gpt_oss_120b",
            "Web_DeepSeek",
            "Web_DeepSeek_Thinking",
        }
        for key in expected_keys:
            self.assertIsNotNone(registry.get(key), f"Missing registry entry for {key}")
            self.assertTrue(callable(registry.handler(key)))

    def test_registry_includes_backward_compatible_aliases(self):
        from ai.model_clients import registry

        self.assertIsNotNone(registry.get("DeepSeek_R1"))
        self.assertIsNotNone(registry.get("Meta_Llama_3_1_70B_Instruct"))
        self.assertIsNotNone(registry.get("Mixtral_8x22b"))


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


class ConversationHistoryTests(TestCase):
    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.history = ConversationHistory(max_messages=4)

    def test_get_returns_empty_list_for_unknown_user(self):
        self.assertEqual(self.history.get("user-1"), [])

    def test_add_exchange_appends_messages(self):
        self.history.add_exchange("user-1", "hello", "hi")
        self.assertEqual(
            self.history.get("user-1"),
            [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
            ],
        )

    def test_history_caps_at_max_messages(self):
        self.history.add_exchange("user-1", "a", "A")
        self.history.add_exchange("user-1", "b", "B")
        self.history.add_exchange("user-1", "c", "C")
        # max_messages=4, so after three exchanges (6 messages) we keep the last 4:
        # [assistant A, user b, assistant B, user c, assistant C] -> capped to 4
        # -> [user b, assistant B, user c, assistant C]
        history = self.history.get("user-1")
        self.assertEqual(len(history), 4)
        self.assertEqual(history[0]["content"], "b")

    def test_reset_clears_history(self):
        self.history.add_exchange("user-1", "hello", "hi")
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
        self.assertEqual(message, "hello\n\nПрепромпт: Think step by step.")
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

        for key in ("DeepSeek_R1_Distill_Llama_70B", "Web_DeepSeek_Thinking", "DeepSeek_R1"):
            caps = registry.capabilities(key)
            self.assertTrue(caps["reasoning"], f"{key} should be reasoning")
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

        sample = "program a; begin writeln(1); end."

        async def fake_handler(messages, conv_id):
            return ("program a; begin writeln(1); end.", 12)

        ordered_models = [{"key": "FakeModel", "title": "FakeModel", "handler": fake_handler}]
        tasks_qs = Task.objects.filter(pk__in=[self.t1.id, self.t2.id]).select_related(
            "topic", "programming_language"
        )
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
        try:
            with patch("ai.dl_api_client.fetch_task_solution",
                       lambda sid, tid, ext: {"content": sample}):
                arm_runner._run_batch_job_worker(
                    run_id, tasks_qs, ordered_models, self.user.id, "DLSID-1",
                    ui_language="Русский",
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

        # DB-fallback snapshot rebuilds the batch report from persisted rows.
        snapshot = arm_runner.get_arm_run_snapshot(run_id)
        self.assertEqual(snapshot["run_type"], "batch")
        self.assertEqual(snapshot["report"]["per_model"][0]["solved"], 2)
        self.assertEqual(snapshot["report"]["per_model"][0]["total"], 2)


class SambanovaLoggerTests(SimpleTestCase):
    """sambanova.py must define a module-level logger — its absence made every
    SambaNova model FAIL health-check with `name 'logger' is not defined`."""

    def test_module_defines_logger(self):
        import logging as _logging
        from ai.model_clients import sambanova
        self.assertTrue(hasattr(sambanova, "logger"))
        self.assertIsInstance(sambanova.logger, _logging.Logger)
        # The functions referenced by the registry must import cleanly.
        self.assertTrue(callable(sambanova.ask_DeepSeek_V3_2_async))
        self.assertTrue(callable(sambanova.ask_Gpt_oss_120b_async))


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


class TokenUsageTests(TestCase):
    """Daily global token-usage banner (Codeforces-style, single unit)."""

    def setUp(self):
        from ai.token_usage import invalidate_daily_tokens_cache
        from django.core.cache import cache
        cache.clear()
        invalidate_daily_tokens_cache()
        self.factory = RequestFactory()

    def _make_log(self, tokens, sent_at):
        user = get_user_model().objects.create_user(username=f"u{tokens}{sent_at}")
        return AIRequestLog.objects.create(
            user=user,
            sent_at=sent_at,
            source=AIRequestLog.SOURCE_WEBSOCKET,
            mode=AIRequestLog.MODE_CHAT,
            tokens=tokens,
        )

    def test_format_millions_single_unit(self):
        from ai.token_usage import _format_millions
        # 159814 -> 0.16 (millions, 3-decimal rounding, trailing zeros stripped)
        self.assertEqual(_format_millions(1_900_000), "1.9")
        self.assertEqual(_format_millions(0), "0")
        self.assertEqual(_format_millions(None), "0")
        # Used value: rounding to 3 decimals, in the same unit as the limit.
        self.assertEqual(_format_millions(159_814), "0.16")

    def test_daily_tokens_sums_only_today_msk(self):
        from datetime import timedelta
        from ai.token_usage import _msk_day_start, get_daily_tokens_used
        today_start = _msk_day_start()
        self._make_log(1_000, today_start + timedelta(hours=1))
        self._make_log(2_500, today_start + timedelta(hours=5))
        # Yesterday (before the MSK day boundary) must be excluded.
        self._make_log(999_999, today_start - timedelta(hours=2))
        used = get_daily_tokens_used()
        self.assertEqual(used, 3_500)

    @override_settings(AI_DAILY_TOKEN_LIMIT=1_900_000)
    def test_payload_includes_used_and_limit_in_millions(self):
        from datetime import timedelta
        from ai.token_usage import _msk_day_start, get_daily_token_usage
        today_start = _msk_day_start()
        self._make_log(159_814, today_start + timedelta(hours=1))
        payload = get_daily_token_usage()
        self.assertEqual(payload["limit"], 1_900_000)
        self.assertEqual(payload["limit_display"], "1.9")
        self.assertEqual(payload["used"], 159_814)
        # Both sides render in the same unit (millions).
        self.assertEqual(payload["used_display"], "0.16")

    @override_settings(AI_DAILY_TOKEN_LIMIT=0)
    def test_payload_hides_limit_when_disabled(self):
        from ai.token_usage import get_daily_token_usage
        payload = get_daily_token_usage()
        self.assertEqual(payload["limit"], 0)
        self.assertEqual(payload["limit_display"], "")
