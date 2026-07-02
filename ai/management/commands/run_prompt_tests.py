"""Run prompt regression tests from the CLI.

Runs one model over a set of prompt regression test cases (with a chosen prompt
under test), polls until the run completes, and prints the report to stdout.
Intended for CI / scheduled checks after a prompt edit.

Usage:
    python manage.py run_prompt_tests --model <key> [--prompt <id>]
        [--cases 1,2,3] [--ui-language Русский]
"""

from django.core.management.base import BaseCommand, CommandError

from ai.prompt_test_runner import get_prompt_test_run_snapshot, start_prompt_test_run


class Command(BaseCommand):
    help = "Run prompt regression tests and print the report"

    def add_arguments(self, parser):
        parser.add_argument("--model", required=True, help="Model key (e.g. DeepSeek_V3_1)")
        parser.add_argument("--prompt", default="", help="Prompt id under test (optional)")
        parser.add_argument(
            "--cases",
            default="",
            help="Comma-separated PromptTestCase ids; empty = all active cases",
        )
        parser.add_argument("--ui-language", default="Русский", help="UI language (Русский/English/Français)")

    def handle(self, *args, **options):
        model_key = options["model"]
        prompt_id = (options["prompt"] or "").strip() or None
        ui_language = options["ui_language"] or "Русский"

        case_ids = []
        for raw in (options["cases"] or "").split(","):
            raw = raw.strip()
            if not raw:
                continue
            try:
                case_ids.append(int(raw))
            except ValueError:
                raise CommandError(f"Invalid case id: {raw!r}")

        run_id, error = start_prompt_test_run(
            case_ids, model_key, options.get("user_id") or 0,
            prompt_id=prompt_id, ui_language=ui_language,
        )
        if not run_id:
            raise CommandError(error or "Не удалось запустить прогон")

        self.stdout.write(f"Регрессионный прогон запущен: run_id={run_id}")

        import time
        snapshot = None
        # Poll until terminal. The runner's daemon thread does the real work.
        while True:
            snapshot = get_prompt_test_run_snapshot(run_id)
            status = snapshot.get("status") if snapshot else None
            if status in ("completed", "failed"):
                break
            time.sleep(1.0)

        if not snapshot:
            raise CommandError("Прогон исчез до завершения")

        if snapshot.get("status") == "failed":
            raise CommandError(snapshot.get("error_message") or "Прогон завершился с ошибкой")

        report = snapshot.get("report") or {}
        self.stdout.write(self.style.SUCCESS(
            "Готово: "
            f"всего {report.get('total', 0)}, "
            f"совпадает {report.get('matched', 0)}, "
            f"отклонений {report.get('mismatched', 0)}, "
            f"пропущено {report.get('skipped', 0)}, "
            f"токенов {report.get('tokens_total', 0)}"
        ))

        mismatches = report.get("mismatches") or []
        if not mismatches:
            self.stdout.write("Отклонений от эталона нет.")
            return

        self.stdout.write("")
        self.stdout.write(self.style.WARNING(f"Отклонения ({len(mismatches)}):"))
        for m in mismatches:
            self.stdout.write("")
            self.stdout.write(self.style.NOTICE(f"• {m.get('case_name', '—')} [{m.get('mode', '')}]"))
            if m.get("diff_hint"):
                self.stdout.write(f"  Причина: {m['diff_hint']}")
            self.stdout.write("  Эталон:")
            self._write_indented(m.get("expected", "") or "—")
            self.stdout.write("  Реакция модели:")
            self._write_indented(m.get("actual", "") or "—")

    def _write_indented(self, text):
        for line in str(text).splitlines() or ["—"]:
            self.stdout.write(f"    {line}")