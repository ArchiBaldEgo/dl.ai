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
            updated = run_model_health_check(force=force)
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
