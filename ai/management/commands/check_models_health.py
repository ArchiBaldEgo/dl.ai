from django.core.management.base import BaseCommand, CommandError

from ai.model_health import get_available_model_options, run_model_health_check


class Command(BaseCommand):
    help = "Run model health checks for current 04:00 MSK window and persist availability"

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Force re-check even if current window was already completed",
        )

    def handle(self, *args, **options):
        try:
            updated = run_model_health_check(force=options["force"])
            available = get_available_model_options()
        except Exception as exc:
            raise CommandError(f"Model health check failed: {exc}") from exc

        if updated:
            self.stdout.write(self.style.SUCCESS("Model health check completed."))
        else:
            self.stdout.write("Model health check is already up-to-date for current window.")

        if available:
            self.stdout.write("Available models:")
            for model in available:
                self.stdout.write(f"- {model['title']} ({model['key']})")
        else:
            self.stdout.write(self.style.WARNING("No models are available in current window."))
