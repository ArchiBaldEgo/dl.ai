from django.core.management.base import BaseCommand, CommandError

from ai.model_health import (
    get_model_status_rows,
    is_model_health_refresh_running,
    run_model_health_check,
)


class Command(BaseCommand):
    help = "Run model health checks for current 04:00 MSK window and persist availability"

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Force re-check even if current window was already completed",
        )

    def _print_model_detail(self, detail):
        """Live per-model console output: HTTP code (or 200) + model response."""
        title = detail.get("title") or detail.get("key") or "?"
        code = detail.get("last_http_code")
        code_str = str(code) if code is not None else "—"
        state = "OK" if detail.get("is_available") else "FAIL"
        ms = detail.get("response_time_ms")
        ms_str = f"{ms}ms" if ms is not None else "—"
        message = (detail.get("last_message") or "").replace("\n", " ").strip()
        if len(message) > 200:
            message = message[:200] + "…"
        self.stdout.write(
            f"- {title}: HTTP {code_str} | {state} | {ms_str} | {message}"
        )

    def handle(self, *args, **options):
        force = options["force"]
        # --force bypasses the in-run STATUS_RUNNING guard inside
        # run_model_health_check, so a concurrent run (daily scheduler or admin
        # refresh) would race the auto-recovery restart. Refuse to force while a
        # run is already in progress rather than double-restart the bot pool.
        if force and is_model_health_refresh_running():
            self.stdout.write(self.style.WARNING(
                "A health check is already running; skipping --force to avoid a "
                "concurrent bot-pool restart. Retry shortly."
            ))
            return

        try:
            updated = run_model_health_check(force=force, on_model_checked=self._print_model_detail)
            rows = get_model_status_rows()
        except Exception as exc:
            raise CommandError(f"Model health check failed: {exc}") from exc

        if updated:
            self.stdout.write(self.style.SUCCESS("Model health check completed."))
        else:
            self.stdout.write("Model health check is already up-to-date for current window.")

        if rows:
            self.stdout.write("Model statuses:")
            for row in rows:
                self.stdout.write(f"- {row['title']}: {row['status_label']}")
        else:
            self.stdout.write(self.style.WARNING("No model status data found."))
