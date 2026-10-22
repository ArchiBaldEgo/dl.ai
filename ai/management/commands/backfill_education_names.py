"""Дозагрузка названий вуза/школы и группы из DL (education.schoolName/groupName).

REST API (dl-rest-api.json, версия 1.4.2) в блоке ``EducationInfo`` отдаёт
человекочитаемые названия учебного заведения и группы рядом с ID. Раньше
названий в API не было — их вели вручную в справочнике ``DLEducationName``.
Теперь названия приходят из DL и сохраняются в ``ExternalDLAccount``.

Команда дозабирает их для УЖЕ известных студентов: ``GET
/restapi/get-id-user-info?userId=…`` (в отличие от ``get-user-info`` —
sessionId не требуется, поэтому массовый бэкфилл возможен).

Пустые значения НЕ затирают уже сохранённые (DL может не знать название для
конкретного студента). ``--overwrite`` — перезаписывать и пустыми.

Запуск:
    python manage.py backfill_education_names            # дозагрузка пустых
    python manage.py backfill_education_names --overwrite # перезаписать всё
    python manage.py backfill_education_names --limit 100 # ограничить выборку
"""
import time

from django.core.management.base import BaseCommand

from ai.dl_api_client import fetch_user_education
from ai.models import ExternalDLAccount

# Ключи DL-ответа → поля модели. Пустое значение не затирает старое (если
# только не --overwrite): DL не знает название для части студентов.
_EDU_FIELDS = (
    ("form", "education_form"),
    ("schoolId", "education_school_id"),
    ("schoolKind", "education_school_kind"),
    ("schoolNo", "education_school_no"),
    ("groupMaskId", "education_group_mask_id"),
    ("formLetter", "education_form_letter"),
    ("schoolName", "education_school_name"),
    ("formName", "education_form_name"),
    ("groupName", "education_group_name"),
)


class Command(BaseCommand):
    help = (
        "Дозагрузить из dl.gsu.by названия вуза/школы и группы/класса "
        "для известных студентов (ExternalDLAccount) и сохранить их в БД — "
        "дневной отчёт и «Таски» показывают названия без ручного справочника."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--overwrite",
            action="store_true",
            help="Перезаписывать и пустыми значениями (по умолчанию пустые не затирают старые).",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Ограничить число обработанных аккаунтов (0 — все).",
        )
        parser.add_argument(
            "--delay",
            type=float,
            default=0.0,
            help="Пауза между запросами к DL (секунды) — по умолчанию без пауз.",
        )

    def handle(self, *args, **options):
        overwrite = options.get("overwrite", False)
        limit = options.get("limit", 0) or 0
        delay = float(options.get("delay", 0) or 0)

        queryset = ExternalDLAccount.objects.exclude(external_user_id="").order_by("pk")
        if limit:
            queryset = queryset[:limit]
        accounts = list(queryset)

        self.stdout.write(f"Аккаунтов к обработке: {len(accounts)}")
        updated = 0
        with_names = 0
        failed = 0
        for index, account in enumerate(accounts, start=1):
            try:
                education = fetch_user_education(account.external_user_id)
            except Exception as exc:  # noqa: BLE001 — бэкфилл не должен падать
                failed += 1
                self.stderr.write(f"  ! userId={account.external_user_id}: {exc}")
                continue

            changes = {}
            for dl_key, model_field in _EDU_FIELDS:
                value = (education.get(dl_key) or "").strip()
                if not value and not overwrite:
                    continue
                if getattr(account, model_field, "") == value:
                    continue
                changes[model_field] = value
            if changes:
                for field, value in changes.items():
                    setattr(account, field, value)
                account.save(update_fields=list(changes.keys()))
                updated += 1
            if (account.education_school_name or account.education_group_name):
                with_names += 1

            if index % 25 == 0:
                self.stdout.write(f"  … обработано {index}/{len(accounts)}")
            if delay:
                time.sleep(delay)

        self.stdout.write(self.style.SUCCESS(
            f"Готово: обновлено аккаунтов — {updated}, с названиями — {with_names}, "
            f"ошибок — {failed} (всего {len(accounts)})."
        ))
