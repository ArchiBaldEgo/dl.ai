"""Создание/обновление production-препромпта «В чём ошибка» для C-MPA.

Единственный способ попасть текстом промпта в БД без ручного копипаста:
source of truth — ``docs/prompts/find_error_cmpa_arrays.ru.txt``. Команда
идемпотентна: по умолчанию существующий промпт не перезаписывается (правки
в админке канонические), для обновления текста нужен явный ``--force``.

Примеры::

    python manage.py seed_find_error_prompt --language "C-MPA"
    python manage.py seed_find_error_prompt --language "C-MPA" \
        --topic "Одномерный массив" --bind
    python manage.py seed_find_error_prompt --language "C-MPA" --force
"""

from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from ai.models import ArmPromptBinding, ProgrammingLanguage, Prompt, Topic

DEFAULT_PROMPT_FILE = "docs/prompts/find_error_cmpa_arrays.ru.txt"
DEFAULT_PROMPT_NAME = "C-MPA: одномерный массив — поиск ошибок (v1)"


def _candidate_list(queryset, field: str) -> str:
    rows = list(queryset.values_list("pk", field)[:30])
    return ", ".join(f"#{pk} {name}" for pk, name in rows) or "(пусто)"


def _resolve_language(raw: str) -> ProgrammingLanguage:
    value = (raw or "").strip()
    if not value:
        raise CommandError("Укажите язык: --language \"C-MPA\" (имя или id).")
    if value.isdigit():
        language = ProgrammingLanguage.objects.filter(pk=int(value)).first()
        if language is not None:
            return language
        raise CommandError(f"Язык с id={value} не найден.")
    exact = ProgrammingLanguage.objects.filter(language_name__iexact=value)
    if exact.count() == 1:
        return exact.first()
    partial = ProgrammingLanguage.objects.filter(language_name__icontains=value)
    if partial.count() == 1:
        return partial.first()
    available = _candidate_list(ProgrammingLanguage.objects.order_by("language_name"), "language_name")
    if partial.count() > 1:
        raise CommandError(
            f"Название «{value}» неоднозначно. Кандидаты: {_candidate_list(partial, 'language_name')}"
        )
    raise CommandError(f"Язык «{value}» не найден. Доступные: {available}")


def _resolve_topic(raw: str, language: ProgrammingLanguage) -> Topic:
    value = (raw or "").strip()
    base = Topic.objects.filter(programming_language=language)
    if value.isdigit():
        topic = base.filter(pk=int(value)).first()
        if topic is not None:
            return topic
        raise CommandError(
            f"Тема с id={value} у языка «{language.language_name}» не найдена. "
            f"Доступные: {_candidate_list(base, 'topic_name_ru')}"
        )
    exact = base.filter(topic_name_ru__iexact=value)
    if exact.count() == 1:
        return exact.first()
    partial = base.filter(topic_name_ru__icontains=value)
    if partial.count() == 1:
        return partial.first()
    if partial.count() > 1:
        raise CommandError(
            f"Тема «{value}» неоднозначна. Кандидаты: {_candidate_list(partial, 'topic_name_ru')}"
        )
    raise CommandError(
        f"Тема «{value}» у языка «{language.language_name}» не найдена. "
        f"Доступные: {_candidate_list(base.order_by('topic_name_ru'), 'topic_name_ru')}"
    )


class Command(BaseCommand):
    help = (
        "Создать (или обновить по --force) препромпт «В чём ошибка» для C-MPA "
        "из docs/prompts/find_error_cmpa_arrays.ru.txt."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--file", default=DEFAULT_PROMPT_FILE,
            help=f"Файл с текстом промпта (по умолчанию {DEFAULT_PROMPT_FILE}).",
        )
        parser.add_argument(
            "--language", default="C-MPA",
            help="Язык программирования: имя или id (по умолчанию C-MPA).",
        )
        parser.add_argument(
            "--topic", default="",
            help="Тема: имя или id. Без темы промпт действует на весь язык.",
        )
        parser.add_argument(
            "--name", default=DEFAULT_PROMPT_NAME,
            help=f"Название промпта (по умолчанию «{DEFAULT_PROMPT_NAME}»).",
        )
        parser.add_argument(
            "--force", action="store_true",
            help="Перезаписать текст существующего промпта.",
        )
        parser.add_argument(
            "--bind", action="store_true",
            help="Создать/обновить ArmPromptBinding (промпт по умолчанию для ARM).",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Показать план, ничего не записывая в БД.",
        )

    def handle(self, *args, **options):
        prompt_path = Path(settings.BASE_DIR) / options["file"]
        if not prompt_path.is_file():
            raise CommandError(f"Файл промпта не найден: {prompt_path}")
        prompt_text = prompt_path.read_text(encoding="utf-8").strip()
        if not prompt_text:
            raise CommandError(f"Файл промпта пуст: {prompt_path}")

        language = _resolve_language(options["language"])
        topic = _resolve_topic(options["topic"], language) if options["topic"] else None
        name = (options["name"] or "").strip() or DEFAULT_PROMPT_NAME
        dry_run = bool(options["dry_run"])

        existing = Prompt.objects.filter(
            mode=Prompt.MODE_FIND_ERROR,
            programming_language=language,
            topic=topic,
            prompt_name_ru=name,
        ).first()

        if existing is not None and not options["force"]:
            self.stdout.write(self.style.WARNING(
                f"Промпт уже существует: #{existing.pk} «{name}» "
                f"(язык «{language.language_name}», тема «{topic or 'весь язык'}»). "
                "Текст не изменён; для перезаписи запустите с --force."
            ))
            prompt = existing
        elif existing is not None:
            if dry_run:
                self.stdout.write(f"[dry-run] обновил бы промпт #{existing.pk} «{name}».")
                return
            existing.prompt_text_ru = prompt_text
            existing.save(update_fields=["prompt_text_ru"])
            prompt = existing
            self.stdout.write(self.style.SUCCESS(
                f"Промпт обновлён: #{prompt.pk} «{name}»."
            ))
        else:
            if dry_run:
                self.stdout.write(
                    f"[dry-run] создал бы промпт «{name}» "
                    f"(язык «{language.language_name}», тема «{topic or 'весь язык'}»)."
                )
                return
            prompt = Prompt.objects.create(
                mode=Prompt.MODE_FIND_ERROR,
                programming_language=language,
                topic=topic,
                prompt_name_ru=name,
                prompt_text_ru=prompt_text,
            )
            self.stdout.write(self.style.SUCCESS(
                f"Промпт создан: #{prompt.pk} «{name}»."
            ))

        if options["bind"]:
            if dry_run:
                self.stdout.write(
                    f"[dry-run] создал/обновил бы привязку ARM для "
                    f"«{language.language_name}» / «{topic or 'весь язык'}» → #{prompt.pk}."
                )
            else:
                with transaction.atomic():
                    binding, created = ArmPromptBinding.objects.update_or_create(
                        programming_language=language,
                        topic=topic,
                        mode=ArmPromptBinding.MODE_FIND_ERROR,
                        defaults={"prompt": prompt},
                    )
                action = "создана" if created else "обновлена"
                self.stdout.write(self.style.SUCCESS(
                    f"Привязка ARM {action}: #{binding.pk} → промпт #{prompt.pk}."
                ))

        self.stdout.write(
            "Готово. На странице «В чём ошибка» выберите язык"
            + (f" и тему «{topic}»" if topic is not None else "")
            + " — промпт подставится автоматически (единственная подходящая опция)."
        )
