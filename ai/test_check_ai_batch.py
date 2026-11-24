from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.template.loader import render_to_string
from django.test import RequestFactory, TestCase
from django.urls import reverse

from .admin.batch_check import (
    _result_tree, _submit_batch, admin_check_ai_batch_detail_view, admin_check_ai_protocol_view,
)
from .admin.check_ai import admin_check_ai_view
from .models import AICheckBatch, AICheckBatchFile
from .services.check_ai_queue import process_next_file
from .services.pangram_client import PangramError


class CheckAiBatchTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="checker")
        self.requests = RequestFactory()

    def post_batch(self, programs, *, name="", submission_id="", skipped_files=0):
        request = self.requests.post("/ai/admin/check-ai/batch/", {
            "name": name,
            "submission_id": submission_id,
            "skipped_files": skipped_files,
            "paths": [path for path, _ in programs],
            "files": [SimpleUploadedFile(path.rsplit("/", 1)[-1], content) for path, content in programs],
        })
        request.user = self.user
        return _submit_batch(request)

    def test_filters_paths_and_records_invalid_program(self):
        response = self.post_batch([
            ("Group/a.py", b"x = 1\n"),
            ("Group/empty.pas", b""),
            ("Group/readme.txt", b"Hello"),
            ("Group/.git/tool.py", b"pass"),
            ("Group/../tool.py", b"pass"),
        ], skipped_files=2)
        self.assertEqual(response.status_code, 201)
        batch = AICheckBatch.objects.get()
        self.assertEqual(batch.skipped_count, 5)
        self.assertEqual(batch.display_name, f"Пакет №{batch.pk}")
        queued, invalid = batch.files.order_by("id")
        self.assertEqual(queued.source_text, "x = 1\n")
        self.assertEqual(queued.status, AICheckBatchFile.Status.QUEUED)
        self.assertEqual(invalid.error, "Загруженный файл пуст.")
        self.assertIsNotNone(invalid.checked_at)

    def test_retry_does_not_add_a_second_batch_or_file(self):
        token = uuid4()
        programs = [("Group/a.py", b"x = 1\n")]
        first = self.post_batch(programs, submission_id=token)
        retried = self.post_batch(programs, submission_id=token)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(retried.content, first.content)
        self.assertEqual(AICheckBatchFile.objects.count(), 1)

        self.user = get_user_model().objects.create_user(username="checker2")
        other_user = self.post_batch(programs, submission_id=token)
        self.assertEqual(other_user.status_code, 201)
        self.assertNotEqual(other_user.content, first.content)
        self.assertEqual(AICheckBatchFile.objects.count(), 2)

    @patch("ai.services.check_ai_queue.check_text")
    def test_sequential_queue_preserves_results_and_clears_source(self, check):
        self.post_batch([
            ("Group/a.py", b"first"),
            ("Group/values/b.py", b"second"),
            ("Group/values/c.py", b"third"),
        ], name="  Ноябрьская олимпиада  ")
        batch = AICheckBatch.objects.get()
        self.assertEqual(batch.display_name, "Ноябрьская олимпиада")
        check.side_effect = [
            {"ai_percent": 20, "ai_assisted_percent": 0, "human_percent": 80},
            PangramError(502, "Временная ошибка Pangram."),
            {"ai_percent": 80, "ai_assisted_percent": 0, "human_percent": 20},
        ]
        for _ in range(3):
            self.assertTrue(process_next_file())
        self.assertFalse(process_next_file())
        self.assertEqual([call.args[0] for call in check.call_args_list], ["first", "second", "third"])
        items = list(AICheckBatchFile.objects.order_by("id"))
        self.assertTrue(all(item.checked_at and not item.source_text for item in items))
        self.assertEqual(items[1].error, "Временная ошибка Pangram.")
        tree = _result_tree(items)
        folder = tree["folders"][0]
        self.assertEqual(folder["average"], 50)
        self.assertEqual(folder["folders"][0]["average"], 80)
        html = render_to_string("admin/ai/_check_ai_batch_results.html", {"batch": batch, "tree": tree})
        self.assertIn("Ноябрьская олимпиада</h2>", html)
        self.assertIn('data-path="Group/values/"', html)
        self.assertIn("средний ИИ 50.0%", html)
        self.assertIn("Временная ошибка Pangram.", html)

    @patch("ai.services.check_ai_queue.check_text")
    def test_interrupted_request_is_recorded_without_duplicate_submission(self, check):
        batch = AICheckBatch.objects.create(created_by=self.user, checker_name="checker")
        item = batch.files.create(
            relative_path="a.py", source_text="a = 1", status=AICheckBatchFile.Status.RUNNING,
        )
        self.assertFalse(process_next_file())
        check.assert_not_called()
        item.refresh_from_db()
        self.assertEqual(item.status, AICheckBatchFile.Status.ERROR)
        self.assertEqual(item.source_text, "")

    @patch("ai.admin.batch_check.can_access_check_ai", return_value=True)
    def test_detail_and_protocol_do_not_show_other_users_results(self, _can_access):
        batch = AICheckBatch.objects.create(created_by=self.user, checker_name="checker", name="Первая проверка")
        started = datetime(2026, 11, 24, 21, 30, tzinfo=UTC)
        AICheckBatch.objects.filter(pk=batch.pk).update(created_at=started)
        batch.files.create(
            relative_path="Group/main.py",
            status=AICheckBatchFile.Status.ERROR,
            checked_at=started,
            error="Ошибка проверки.",
        )
        for index, score in enumerate([0.0, 80.0]):
            batch.files.create(
                relative_path=f"Group/{index}.py",
                status=AICheckBatchFile.Status.DONE,
                checked_at=started,
                result={"ai_percent": score},
            )
        stranger = get_user_model().objects.create_user(username="other")
        request = self.requests.get(reverse("ai_check_ai_batch_detail", args=[batch.pk]), {"fragment": "programs"})
        request.user = stranger
        self.assertEqual(admin_check_ai_batch_detail_view(request, batch.pk).status_code, 404)
        request.user = self.user
        fragment = admin_check_ai_batch_detail_view(request, batch.pk).render()
        self.assertContains(fragment, "Group/main.py")
        self.assertContains(fragment, "Ошибка проверки.")

        request = self.requests.get(reverse("ai_check_ai_protocol"))
        request.session = {}
        request.user = stranger
        response = admin_check_ai_protocol_view(request)
        self.assertEqual(len(response.context_data["page_obj"]), 0)
        request.user = self.user
        response = admin_check_ai_protocol_view(request)
        self.assertEqual(len(response.context_data["page_obj"]), 1)
        self.assertContains(response.render(), ">Первая проверка</a>", count=1)
        self.assertContains(response, "Средний ИИ 40.0% · файлов 3")
        self.assertContains(response, "· ошибок 1")
        self.assertNotContains(response, "Group/main.py")
        self.assertContains(response, "<td>25.11.2026 00:30</td>")

        # Протокол включает и пакеты, которые уже выпали из списка последних десяти.
        newer = AICheckBatch.objects.bulk_create(
            [AICheckBatch(created_by=self.user, checker_name="checker") for _ in range(10)]
        )
        AICheckBatch.objects.filter(pk__in=[item.pk for item in newer]).update(
            created_at=started + timedelta(minutes=1)
        )
        AICheckBatchFile.objects.bulk_create(
            [AICheckBatchFile(batch=item, relative_path="a.py") for item in newer]
        )
        response = admin_check_ai_protocol_view(request)
        self.assertEqual(len(response.context_data["page_obj"]), 11)
        self.assertEqual(response.context_data["page_obj"][-1].pk, batch.pk)
        self.assertContains(response.render(), ">Первая проверка</a>", count=1)
        self.assertContains(response, "Обработано 0 из 1", count=10)

    @patch("ai.admin.batch_check.can_access_check_ai", return_value=True)
    @patch("ai.admin.check_ai.can_access_check_ai", return_value=True)
    @patch("ai.admin.check_ai.check_text")
    def test_single_checks_are_named_and_saved_in_protocol(self, check_text, _single_access, _protocol_access):
        check_text.side_effect = [
            {"ai_percent": 24.0, "ai_assisted_percent": 1.0, "human_percent": 75.0},
            PangramError(502, "Временная ошибка Pangram."),
        ]
        single_url = reverse("ai_check_ai") + "?format=json"
        request = self.requests.post(single_url, {"text": "x = 1", "name": "  Одиночный пример  "})
        request.user = self.user
        self.assertEqual(admin_check_ai_view(request).status_code, 200)
        request = self.requests.post(single_url, {"file": SimpleUploadedFile("example.py", b"x = 2")})
        request.user = self.user
        self.assertEqual(admin_check_ai_view(request).status_code, 502)

        checks = list(AICheckBatch.objects.order_by("pk"))
        self.assertEqual([item.kind for item in checks], [AICheckBatch.Kind.SINGLE] * 2)
        self.assertEqual(checks[0].display_name, "Одиночный пример")
        self.assertEqual(checks[1].files.get().relative_path, "example.py")
        self.assertFalse(AICheckBatchFile.objects.exclude(source_text="").exists())
        request = self.requests.get(reverse("ai_check_ai_protocol"))
        request.session = {}
        request.user = self.user
        response = admin_check_ai_protocol_view(request).render()
        self.assertContains(response, "Одиночный пример")
        self.assertContains(response, "ИИ 24.0%")
        self.assertContains(response, "Временная ошибка Pangram.")
        self.assertNotContains(response, "data-programs-url")
