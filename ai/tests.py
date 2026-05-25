from django.contrib.admin.sites import AdminSite
from django.contrib.auth import SESSION_KEY, get_user_model
from django.contrib.auth.models import AnonymousUser
from django.contrib.auth.models import Group
from django.contrib.sessions.middleware import SessionMiddleware
from django.test import SimpleTestCase, RequestFactory, TestCase
from django.http import HttpResponse
from django.db import ProgrammingError
from unittest.mock import patch
from types import SimpleNamespace

from ai.admin import PromptAdmin, PromptForm, _external_admin_entry_response
from ai.models import ExternalDLAccount, ProgrammingLanguage, Prompt, Topic
from ai.views import chat_view, get_prompts, set_password_view


class ChatViewTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    @patch("ai.views.AIAppSettings.get_solo", side_effect=ProgrammingError)
    def test_chat_view_does_not_fail_when_ai_settings_table_missing(self, _mock_get_solo):
        request = self.factory.get("/ai/chat/")
        request.user = SimpleNamespace(is_authenticated=True)
        request.session = {}
        with patch("ai.views.render", return_value=HttpResponse("ok")):
            response = chat_view(request)
        self.assertEqual(response.status_code, 200)

    @patch("ai.views.AIAppSettings.get_solo", return_value=SimpleNamespace(is_enabled=False))
    def test_chat_view_returns_404_when_ai_app_disabled(self, _mock_get_solo):
        request = self.factory.get("/ai/chat/")
        request.user = SimpleNamespace(is_authenticated=True)
        request.session = {}
        response = chat_view(request)
        self.assertEqual(response.status_code, 404)

    def test_chat_view_requires_auth_or_uid(self):
        request = self.factory.get("/ai/chat/")
        request.user = SimpleNamespace(is_authenticated=False)
        request.session = {}
        response = chat_view(request)
        self.assertEqual(response.status_code, 403)


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

    def test_existing_external_user_auto_logs_into_admin(self):
        user = self.user_model.objects.create_user(
            username="12345",
            password="initial-pass",
        )
        request = self._request()

        response = _external_admin_entry_response(request)

        self.assertIsNone(response)
        self.assertEqual(request.session[SESSION_KEY], str(user.pk))
        self.assertTrue(request.session["admin_fresh_auth"])

    def test_existing_mapped_external_user_keeps_valid_admin_session(self):
        user = self.user_model.objects.create_user(
            username="external-login",
            password="initial-pass",
        )
        ExternalDLAccount.objects.create(
            user=user,
            external_user_id="12345",
            external_login="external-login",
        )
        request = self._request(path="/ai/admin/ai/prompt/add/")
        request.user = user

        response = _external_admin_entry_response(request)

        self.assertIsNone(response)
        self.assertTrue(request.session["admin_fresh_auth"])

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
        return request

    def test_prompt_developer_can_edit_only_own_prompt(self):
        request = self._build_request(self.prompt_developer)

        self.assertTrue(self.prompt_admin.has_change_permission(request, self.editable_prompt))
        self.assertFalse(self.prompt_admin.has_change_permission(request, self.legacy_assigned_prompt))
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
        self.assertEqual(readonly_fields, ("programming_language", "topic", "prompt_name", "prompt_text"))

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

        self.assertEqual(prompt_ids, {self.editable_prompt.id})

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
        self.assertNotContains(response, "Readonly prompt")
        self.assertNotContains(response, "Legacy assigned prompt")

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
        prompt_ids = set(self.prompt_admin.get_queryset(request).values_list("id", flat=True))

        self.assertEqual(prompt_ids, {own_prompt.id})


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
        self.assertQuerysetEqual(
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
        self.assertQuerysetEqual(
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
