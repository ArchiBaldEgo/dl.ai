from django.test import SimpleTestCase, RequestFactory
from django.http import HttpResponse
from django.db import ProgrammingError
from unittest.mock import patch
from types import SimpleNamespace
from ai.views import chat_view, decide_task_view, find_error_view


class ChatViewTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    @patch("ai.views.AIAppSettings.get_solo", side_effect=ProgrammingError)
    def test_chat_view_does_not_fail_when_ai_settings_table_missing(self, _mock_get_solo):
        request = self.factory.get("/ai/chat/", {"uid": "12345"})
        request.user = SimpleNamespace(is_authenticated=True)
        request.session = {}
        with patch("ai.views.render", return_value=HttpResponse("ok")):
            response = chat_view(request)
        self.assertEqual(response.status_code, 200)

    @patch("ai.views.AIAppSettings.get_solo", return_value=SimpleNamespace(is_enabled=False))
    def test_chat_view_returns_404_when_ai_app_disabled(self, _mock_get_solo):
        request = self.factory.get("/ai/chat/", {"uid": "12345"})
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


class DecideTaskViewTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_decide_task_view_requires_uid(self):
        request = self.factory.get("/ai/solve-problem/")
        request.user = SimpleNamespace(is_authenticated=False)
        request.session = {}
        response = decide_task_view(request)
        self.assertEqual(response.status_code, 403)

    @patch("ai.views.AIAppSettings.get_solo", side_effect=ProgrammingError)
    def test_decide_task_view_accessible_with_valid_uid(self, _mock_get_solo):
        request = self.factory.get("/ai/solve-problem/", {"uid": "186638"})
        request.user = SimpleNamespace(is_authenticated=True)
        request.session = {}
        with patch("ai.views.render", return_value=HttpResponse("ok")):
            response = decide_task_view(request)
        self.assertEqual(response.status_code, 200)

    @patch("ai.views.AIAppSettings.get_solo", return_value=SimpleNamespace(is_enabled=False))
    def test_decide_task_view_returns_404_when_disabled(self, _mock_get_solo):
        request = self.factory.get("/ai/solve-problem/", {"uid": "186638"})
        request.user = SimpleNamespace(is_authenticated=True)
        request.session = {}
        response = decide_task_view(request)
        self.assertEqual(response.status_code, 404)


class FindErrorViewTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_find_error_view_requires_uid(self):
        request = self.factory.get("/ai/find-error/")
        request.user = SimpleNamespace(is_authenticated=False)
        request.session = {}
        response = find_error_view(request)
        self.assertEqual(response.status_code, 403)

    @patch("ai.views.AIAppSettings.get_solo", side_effect=ProgrammingError)
    def test_find_error_view_accessible_with_valid_uid(self, _mock_get_solo):
        request = self.factory.get("/ai/find-error/", {"uid": "186638"})
        request.user = SimpleNamespace(is_authenticated=True)
        request.session = {}
        with patch("ai.views.render", return_value=HttpResponse("ok")):
            response = find_error_view(request)
        self.assertEqual(response.status_code, 200)

    @patch("ai.views.AIAppSettings.get_solo", return_value=SimpleNamespace(is_enabled=False))
    def test_find_error_view_returns_404_when_disabled(self, _mock_get_solo):
        request = self.factory.get("/ai/find-error/", {"uid": "186638"})
        request.user = SimpleNamespace(is_authenticated=True)
        request.session = {}
        response = find_error_view(request)
        self.assertEqual(response.status_code, 404)
