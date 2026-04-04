from django.test import TestCase
from django.contrib.auth.models import User
from django.db import ProgrammingError
from unittest.mock import patch


class ChatViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="testpass123")

    @patch("ai.views.AIAppSettings.get_solo", side_effect=ProgrammingError)
    def test_chat_view_does_not_fail_when_ai_settings_table_missing(self, _mock_get_solo):
        self.client.force_login(self.user)
        response = self.client.get("/ai/chat/")
        self.assertEqual(response.status_code, 200)
