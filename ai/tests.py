from django.contrib.admin.sites import AdminSite
from django.contrib.auth import SESSION_KEY, get_user_model
from django.contrib.auth.models import AnonymousUser
from django.contrib.auth.models import Group
from django.contrib.sessions.middleware import SessionMiddleware
from django.test import SimpleTestCase, RequestFactory, TestCase, override_settings
import json

from django.http import HttpResponse
from django.db import ProgrammingError
from django.utils import timezone
from unittest.mock import patch
from types import SimpleNamespace

from ai.admin import PromptAdmin, PromptForm
from ai.middleware import ExternalAuthMiddleware
from ai.i18n import get_localized_name, get_ui_language_suffix
from ai.models import AIRequestLog, ExternalDLAccount, ProgrammingLanguage, Prompt, SharedPrompt, Topic
from ai.views import chat_view, get_problem_data, get_prompts, set_password_view


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
class ExternalAuthMiddlewareTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.middleware = ExternalAuthMiddleware(lambda req: HttpResponse("ok"))

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

    def test_authenticated_user_skips_external_auth(self):
        request = self.factory.get("/ai/chat/")
        self._add_session(request)
        request.user = SimpleNamespace(is_authenticated=True, pk=1)

        with patch("ai.middleware.fetch_external_user_info") as fetch_user_info:
            response = self.middleware(request)

        self.assertEqual(response.status_code, 200)
        fetch_user_info.assert_not_called()

    def test_cached_user_info_skips_external_call(self):
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

    def test_has_permission_accepts_matching_prompt_developer(self):
        from ai.admin.site import ai_admin_site
        from django.contrib.auth.models import Group
        from ai.constants import PROMPT_DEVELOPER_GROUP
        group, _ = Group.objects.get_or_create(name=PROMPT_DEVELOPER_GROUP)
        user = self.user_model.objects.create_user(
            username="12345",
            password="initial-pass",
        )
        user.groups.add(group)
        request = self._request(user_id="12345")
        request.user = user
        self.assertTrue(ai_admin_site.has_permission(request))

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
