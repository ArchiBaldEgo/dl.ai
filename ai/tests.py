from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import SimpleTestCase, RequestFactory, TestCase
from django.http import HttpResponse
from django.db import ProgrammingError
from unittest.mock import patch
from types import SimpleNamespace

from ai.admin import PromptAdmin
from ai.models import Prompt
from ai.views import chat_view


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
        self.tester_developer = user_model.objects.create_user(
            username="tester_dev",
            password="test-pass",
        )
        self.other_prompt_developer = user_model.objects.create_user(
            username="prompt_dev_other",
            password="test-pass",
        )

        prompt_developer_group, _ = Group.objects.get_or_create(name="prompt_developer")
        tester_group, _ = Group.objects.get_or_create(name="tester")
        self.prompt_developer.groups.add(prompt_developer_group)
        self.tester_developer.groups.add(tester_group)
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

    def test_prompt_developer_can_edit_only_own_or_assigned_prompt(self):
        request = self._build_request(self.prompt_developer)

        self.assertTrue(self.prompt_admin.has_change_permission(request, self.editable_prompt))
        self.assertTrue(self.prompt_admin.has_change_permission(request, self.legacy_assigned_prompt))
        self.assertFalse(self.prompt_admin.has_change_permission(request, self.readonly_prompt))
        self.assertTrue(self.prompt_admin.has_view_permission(request, self.readonly_prompt))

    def test_prompt_developer_can_add_prompt_and_becomes_owner(self):
        request = self._build_request(self.prompt_developer)
        self.assertTrue(self.prompt_admin.has_add_permission(request))

        new_prompt = Prompt(prompt_name="My prompt", prompt_text="My prompt text")
        self.prompt_admin.save_model(request, new_prompt, form=None, change=False)
        new_prompt.refresh_from_db()

        self.assertEqual(new_prompt.owner_id, self.prompt_developer.id)
        self.assertTrue(new_prompt.editors.filter(pk=self.prompt_developer.pk).exists())

    def test_tester_group_has_prompt_developer_rights(self):
        request = self._build_request(self.tester_developer)
        self.assertTrue(self.prompt_admin.has_add_permission(request))

        prompt = Prompt(prompt_name="Tester prompt", prompt_text="Tester prompt text")
        self.prompt_admin.save_model(request, prompt, form=None, change=False)
        prompt.refresh_from_db()

        self.assertEqual(prompt.owner_id, self.tester_developer.id)
        self.assertTrue(prompt.editors.filter(pk=self.tester_developer.pk).exists())

    def test_prompt_developer_fields_are_readonly_for_foreign_prompt(self):
        request = self._build_request(self.prompt_developer)

        readonly_fields = self.prompt_admin.get_readonly_fields(request, self.readonly_prompt)
        editable_fields = self.prompt_admin.get_readonly_fields(request, self.editable_prompt)

        self.assertEqual(editable_fields, ())
        self.assertEqual(readonly_fields, ("topic", "prompt_name", "prompt_text"))

    def test_prompt_developer_mine_filter_shows_only_own_scope(self):
        request = self._build_request(self.prompt_developer, {"mine": "1"})
        prompt_ids = set(self.prompt_admin.get_queryset(request).values_list("id", flat=True))

        self.assertIn(self.editable_prompt.id, prompt_ids)
        self.assertIn(self.legacy_assigned_prompt.id, prompt_ids)
        self.assertNotIn(self.readonly_prompt.id, prompt_ids)

    def test_staff_user_has_full_prompt_permissions(self):
        request = self._build_request(self.staff_user)

        self.assertTrue(self.prompt_admin.has_add_permission(request))
        self.assertTrue(self.prompt_admin.has_change_permission(request, self.readonly_prompt))
        self.assertEqual(self.prompt_admin.get_readonly_fields(request, self.readonly_prompt), ())
