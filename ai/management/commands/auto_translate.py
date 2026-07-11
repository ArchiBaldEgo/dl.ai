"""Management command: auto-translate empty *_en and *_fr fields.

Usage:
    python manage.py auto_translate                  # translate all empty fields
    python manage.py auto_translate --model Topic     # only one model
    python manage.py auto_translate --overwrite       # overwrite existing translations
    python manage.py auto_translate --lang fr          # only French
"""

import logging

from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Auto-translate empty *_en and *_fr localized fields for Topic, "
        "SharedPrompt and Prompt using an available AI model."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--model",
            type=str,
            default=None,
            help="Only translate this model class (Topic, SharedPrompt, Prompt).",
        )
        parser.add_argument(
            "--lang",
            type=str,
            default=None,
            choices=["en", "fr"],
            help="Only translate this target language.",
        )
        parser.add_argument(
            "--overwrite",
            action="store_true",
            default=False,
            help="Overwrite existing non-empty translations.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Show what would be translated without saving.",
        )

    def handle(self, *args, **options):
        from ai.services.auto_translate import get_translatable_models

        model_filter = options.get("model")
        lang_filter = options.get("lang")
        overwrite = options.get("overwrite")
        dry_run = options.get("dry_run")

        target_langs = (lang_filter,) if lang_filter else None

        models = get_translatable_models()

        total_translated = 0
        total_skipped = 0
        total_failed = 0

        for model_class, field_bases in models:
            if model_filter and model_class.__name__ != model_filter:
                continue

            qs = model_class.objects.all()
            count = qs.count()
            self.stdout.write(
                self.style.HTTP_INFO(
                    f"\n[{model_class.__name__}] {count} record(s) — fields: {field_bases}"
                )
            )

            for obj in qs.iterator():
                self.stdout.write(f"  ID={obj.pk} — {obj}", ending="")

                if dry_run:
                    # Check which fields are empty
                    missing = []
                    for fb in field_bases:
                        for lang in (target_langs or ("en", "fr")):
                            attr = f"{fb}_{lang}"
                            current = getattr(obj, attr, None) or ""
                            if not current.strip() or overwrite:
                                source = getattr(obj, f"{fb}_ru", None) or getattr(obj, fb, None) or ""
                                if source:
                                    missing.append(attr)
                    if missing:
                        self.stdout.write(f" → needs: {', '.join(missing)}")
                    else:
                        self.stdout.write(" — all fields set")
                        total_skipped += 1
                    continue

                from ai.services.auto_translate import translate_object

                results = translate_object(
                    obj,
                    field_bases,
                    target_langs=target_langs,
                    overwrite=overwrite,
                )

                has_fail = False
                has_translated = False
                for attr, val in results.items():
                    if val.startswith("skipped"):
                        total_skipped += 1
                    elif val == "failed":
                        total_failed += 1
                        has_fail = True
                    else:
                        total_translated += 1
                        has_translated = True

                if has_fail:
                    self.stdout.write(self.style.ERROR(" — FAILED"))
                elif has_translated:
                    self.stdout.write(self.style.SUCCESS(" — translated ✓"))
                else:
                    self.stdout.write(" — skipped (already set)")

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                f"Done: {total_translated} translated, {total_skipped} skipped, "
                f"{total_failed} failed"
            )
        )