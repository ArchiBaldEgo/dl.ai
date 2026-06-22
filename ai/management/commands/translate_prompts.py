"""Auto-translate Prompt and SharedPrompt name/text fields to en/fr.

Fills ONLY the empty localized fields (``prompt_name_en``/``_fr``,
``prompt_text_en``/``_fr``) from the Russian base field, unless ``--overwrite``
is given. Translation is performed by a registered model handler (default
``DeepSeek_V3_1`` via SambaNova/``SC_TOKEN``), using the same
``async_to_sync(handler)(prompt, conv_id)`` convention as the health check.

Placeholders of the form ``{language}``/``{язык}``/``{topic}``/``{тема}``/
``{message}``/``{code}`` are swapped for sentinel tokens before the call and
restored afterwards, so the model cannot translate or reword them.
"""

import re
import uuid

from asgiref.sync import async_to_sync
from django.core.management.base import BaseCommand, CommandError

from ai.model_clients import registry
from ai.models import Prompt, SharedPrompt

# Target language display labels used inside the translation instruction.
_LANG_LABELS = {"en": "English", "fr": "Français"}

# Placeholders that must survive translation verbatim.
_PLACEHOLDER_RE = re.compile(r"\{(language|язык|topic|тема|message|code)\}")

# Field pairs translated for every object: (source_attr, target_attr_template).
_FIELD_PAIRS = (
    ("prompt_name", "prompt_name_{}"),
    ("prompt_text", "prompt_text_{}"),
)

_NAME_MAX_LENGTH = 255


def _extract_text(result):
    """Normalize a handler return value ( ``(text, tokens)`` or ``text`` )."""
    if isinstance(result, tuple):
        if result and result[0] is not None:
            return str(result[0]).strip()
        return ""
    if result is None:
        return ""
    return str(result).strip()


def _protect_placeholders(text):
    """Replace ``{placeholder}`` tokens with ``@@PH<n>@@`` sentinels."""
    sentinels = []

    def _sub(match):
        index = len(sentinels)
        sentinels.append(match.group(0))
        return f"@@PH{index}@@"

    protected = _PLACEHOLDER_RE.sub(_sub, text)
    return protected, sentinels


def _restore_placeholders(text, sentinels):
    """Restore sentinel tokens back to the original placeholders (case-insensitive)."""
    for index, original in enumerate(sentinels):
        marker = f"@@PH{index}@@"
        text = re.sub(re.escape(marker), original, text, flags=re.IGNORECASE)
    # Strip stray whitespace the model may have inserted inside the braces.
    return re.sub(r"\{\s*([a-zA-Zа-яА-Я]+)\s*\}", r"{\1}", text)


def _translate(model_key, source_text, target_label, conv_id):
    """Translate ``source_text`` (Russian) to ``target_label`` via the model."""
    handler = registry.handler(model_key)
    if not handler:
        raise CommandError(f"Model '{model_key}' is not registered")

    protected, sentinels = _protect_placeholders(source_text)
    instruction = (
        f"Переведи следующий текст с русского на {target_label}. "
        "Выведи ТОЛЬКО перевод — без пояснений, без кавычек и без префиксов "
        "вроде «Перевод:». Сохрани все маркеры вида @@PH<n>@@ без изменений, "
        "на тех же местах. Если текст уже на нужном языке, верни его как есть."
        "\n\nТекст:\n"
    )
    result = async_to_sync(handler)(instruction + protected, conv_id)
    translated = _extract_text(result)
    if not translated:
        raise RuntimeError("Model returned an empty translation")
    return _restore_placeholders(translated, sentinels).strip()


def _coerce_name(value):
    value = value.strip()
    return value[:_NAME_MAX_LENGTH] if value else value


class Command(BaseCommand):
    help = (
        "Translate empty prompt_name/prompt_text _en/_fr fields for Prompt and "
        "SharedPrompt from the Russian base, using a registered model."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--model",
            default="DeepSeek_V3_1",
            help="Registry model key used for translation (default: DeepSeek_V3_1).",
        )
        parser.add_argument(
            "--languages",
            default="en,fr",
            help="Comma-separated target suffixes (default: en,fr).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would be translated without writing to the DB.",
        )
        parser.add_argument(
            "--overwrite",
            action="store_true",
            help="Re-translate even non-empty localized fields.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Stop after translating this many fields (0 = no limit).",
        )

    def handle(self, *args, **options):
        model_key = options["model"]
        if not registry.handler(model_key):
            raise CommandError(
                f"Model '{model_key}' is not registered. Check ai/model_clients/registry.py."
            )
        languages = [s.strip() for s in options["languages"].split(",") if s.strip()]
        for suffix in languages:
            if suffix not in _LANG_LABELS:
                raise CommandError(f"Unsupported language suffix: {suffix!r} (use: en, fr)")

        dry_run = options["dry_run"]
        overwrite = options["overwrite"]
        limit = options["limit"]

        # Validate the key/balance early with one cheap call so the user gets a
        # clear, immediate error instead of a long silent loop of failures.
        try:
            probe = _translate(model_key, "Проверка связи.", _LANG_LABELS[languages[0]],
                               f"translate-probe-{uuid.uuid4().hex[:8]}")
        except CommandError:
            raise
        except Exception as exc:
            raise CommandError(
                f"Probe call to model '{model_key}' failed: {exc}. "
                "Verify SC_TOKEN / balance (SambaNova) or bot pool, then retry."
            ) from exc
        self.stdout.write(self.style.SUCCESS(
            f"Model '{model_key}' reachable (probe reply: {probe[:40]!r})."
        ))

        counts = {"translated": 0, "would_translate": 0, "skipped": 0, "failed": 0}

        def _done_translations():
            return counts["translated"] + counts["would_translate"]

        def _process_object(obj, label):
            for suffix in languages:
                target_label = _LANG_LABELS[suffix]
                for source_attr, target_template in _FIELD_PAIRS:
                    target_attr = target_template.format(suffix)
                    source_value = getattr(obj, source_attr, "") or ""
                    existing = getattr(obj, target_attr, "") or ""
                    if not source_value or (existing and not overwrite):
                        counts["skipped"] += 1
                        continue
                    if limit and _done_translations() >= limit:
                        return
                    conv_id = f"translate-{model_key}-{obj.pk}-{target_attr}-{uuid.uuid4().hex[:8]}"
                    try:
                        translated = _translate(model_key, source_value, target_label, conv_id)
                    except Exception as exc:
                        counts["failed"] += 1
                        self.stdout.write(self.style.WARNING(
                            f"[{label} #{obj.pk}] {target_attr}: FAILED ({exc})"
                        ))
                        continue
                    if target_attr.startswith("prompt_name"):
                        translated = _coerce_name(translated)
                    self.stdout.write(
                        f"[{label} #{obj.pk}] {target_attr}: {translated[:80]!r}"
                    )
                    if dry_run:
                        counts["would_translate"] += 1
                        continue
                    setattr(obj, target_attr, translated)
                    obj.save(update_fields=[target_attr])
                    counts["translated"] += 1

        for shared in SharedPrompt.objects.all().iterator():
            _process_object(shared, "SharedPrompt")
        for prompt in Prompt.objects.all().iterator():
            _process_object(prompt, "Prompt")

        summary = (
            f"translate_prompts done — "
            f"translated={counts['translated']} "
            f"would_translate={counts['would_translate']} "
            f"skipped={counts['skipped']} "
            f"failed={counts['failed']}"
        )
        if dry_run:
            summary += " (DRY RUN — nothing written)"
        self.stdout.write(self.style.SUCCESS(summary))