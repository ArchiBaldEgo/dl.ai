"""Импорт базы ошибочных программ режима «В чём ошибка?» из папки датасета.

Структура датасета::

    <root>/<Группа>/<Кейс>/1.mpc
    <root>/<Группа>/<Кейс>/error_N.txt
    <root>/<Группа>/url.txt      (nid=… / cid=…)
    <root>/<Группа>/info.txt     (условие, cp1251)

``.mpc`` — UTF-8; ``info.txt``/``url.txt``/``error_*.txt`` — cp1251 (читаем
BOM-aware UTF-8 с фолбэком). Команда идемпотентна: повторный запуск
пропускает кейсы с тем же ``source_hash`` и не затирает ручные правки в
админке; ``--force`` перезаписывает всегда.

Условие задачи берётся по приоритету (``--statement-source=auto``):
локальная ``Task`` по ``node_id`` → DL (``--fetch-statements`` + сессия) →
``info.txt``.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from ai.dl_api_client import DLApiError, fetch_task_info
from ai.models import (
    FindErrorCase,
    FindErrorCaseGroup,
    FindErrorCaseLabel,
    ProgrammingLanguage,
    Task,
    Topic,
)
from ai.services.find_error_catalog import VALID_ERROR_CODES

_URL_NID_RE = re.compile(r"nid=(\d+)", re.IGNORECASE)
_URL_CID_RE = re.compile(r"cid=(\d+)", re.IGNORECASE)
_ERROR_FILE_RE = re.compile(r"^error_(\d+)\.txt$", re.IGNORECASE)


def _read_text(path: Path) -> str:
    """UTF-8 (BOM-aware) с фолбэком на cp1251 — датасет смешанный."""
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("cp1251", errors="replace")


def _source_hash(code: str, labels) -> str:
    """sha256 кода и отсортированных пар (код, текст) — ключ идемпотентности."""
    digest = hashlib.sha256()
    digest.update(code.strip().encode("utf-8"))
    for code_num, text in sorted(labels, key=lambda pair: int(pair[0])):
        digest.update(b"\x00")
        digest.update(f"{code_num}\x00{text.strip()}".encode("utf-8"))
    return digest.hexdigest()


class Command(BaseCommand):
    help = "Импортировать ошибочные программы «В чём ошибка?» из папки датасета."

    def add_arguments(self, parser):
        parser.add_argument("root", help="Корень датасета (папка с группами).")
        parser.add_argument(
            "--dataset", default="odnomerny_massiv",
            help="Имя датасета для source_ref (по умолчанию odnomerny_massiv).",
        )
        parser.add_argument("--language", default="", help="Язык: имя или id.")
        parser.add_argument(
            "--topic", default="",
            help="Тема: имя или id. Необязательно — тогда тема не назначается.",
        )
        parser.add_argument(
            "--create-missing", action="store_true",
            help="Создать отсутствующий язык/тему (по умолчанию — ошибка со списком).",
        )
        parser.add_argument(
            "--statement-source", choices=("auto", "task", "dl", "info"), default="auto",
            help="Откуда брать условие (auto: task → dl → info).",
        )
        parser.add_argument(
            "--fetch-statements", action="store_true",
            help="Тянуть условие из DL по nid/cid (нужен --session-id).",
        )
        parser.add_argument(
            "--session-id", default="",
            help="DLSID для запросов к DL (обязателен вместе с --fetch-statements).",
        )
        parser.add_argument("--force", action="store_true", help="Перезаписать существующие кейсы.")
        parser.add_argument("--dry-run", action="store_true", help="Только показать план.")

    # --- резолв языка/темы ------------------------------------------------

    def _resolve_language(self, value: str, create_missing: bool) -> ProgrammingLanguage | None:
        value = (value or "").strip()
        if not value:
            return None
        qs = ProgrammingLanguage.objects.all()
        obj = qs.filter(pk=int(value)).first() if value.isdigit() else qs.filter(
            language_name__iexact=value
        ).first()
        if obj is None and create_missing:
            obj = ProgrammingLanguage.objects.create(language_name=value)
            self.stdout.write(self.style.SUCCESS(f"Создан язык: {value}"))
        if obj is None:
            names = ", ".join(qs.values_list("language_name", flat=True).order_by("language_name"))
            raise CommandError(f"Язык не найден: {value}. Доступные: {names or '—'}")
        return obj

    def _resolve_topic(self, value: str, language, create_missing: bool) -> Topic | None:
        value = (value or "").strip()
        if not value:
            return None
        qs = Topic.objects.all()
        obj = qs.filter(pk=int(value)).first() if value.isdigit() else qs.filter(
            topic_name_ru__iexact=value
        ).first()
        if obj is None and language is not None and create_missing:
            obj = Topic.objects.create(topic_name_ru=value, programming_language=language)
            self.stdout.write(self.style.SUCCESS(f"Создана тема: {value}"))
        if obj is None:
            names = ", ".join(qs.values_list("topic_name_ru", flat=True).order_by("topic_name_ru"))
            raise CommandError(f"Тема не найдена: {value}. Доступные: {names or '—'}")
        return obj

    # --- условие ----------------------------------------------------------

    def _statement_for_group(self, *, node_id, course_id, info_text, options) -> tuple[str, str]:
        source = options["statement_source"]
        if source in ("auto", "task") and node_id:
            task = Task.objects.filter(node_id=node_id).exclude(statement="").first()
            if task is not None:
                return task.statement.strip(), "task"
            if source == "task":
                self.stdout.write(self.style.WARNING(
                    f"  Условие: локальная Task для nid={node_id} не найдена."
                ))
        if source in ("auto", "dl") and options["fetch_statements"] and node_id:
            session_id = options["session_id"]
            if not session_id:
                self.stdout.write(self.style.WARNING(
                    "  Условие: --fetch-statements без --session-id — DL пропущен, беру info.txt."
                ))
            else:
                try:
                    data = fetch_task_info(node_id, session_id, course_id=course_id)
                    statement = (data.get("statement") or "").strip()
                    if statement:
                        return statement, "dl"
                    self.stdout.write(self.style.WARNING(
                        f"  Условие: DL вернул пустое условие для nid={node_id}."
                    ))
                except DLApiError as exc:
                    self.stdout.write(self.style.WARNING(
                        f"  Условие: DL недоступен для nid={node_id} ({exc}); беру info.txt."
                    ))
        if info_text.strip():
            return info_text.strip(), "info_txt"
        return "", ""

    # --- основная логика --------------------------------------------------

    def handle(self, *args, **options):
        root = Path(options["root"]).expanduser()
        if not root.is_dir():
            raise CommandError(f"Папка не найдена: {root}")
        if options["fetch_statements"] and not options["session_id"]:
            self.stdout.write(self.style.WARNING(
                "--fetch-statements без --session-id: условия будут из info.txt."
            ))
        language = self._resolve_language(options["language"], options["create_missing"])
        topic = self._resolve_topic(options["topic"], language, options["create_missing"])

        created_groups = updated_groups = created_cases = updated_cases = skipped_cases = 0
        with transaction.atomic():
            for group_dir in sorted(p for p in root.iterdir() if p.is_dir()):
                url_path = group_dir / "url.txt"
                info_path = group_dir / "info.txt"
                case_dirs = sorted(
                    child for child in group_dir.iterdir()
                    if child.is_dir() and (child / "1.mpc").is_file()
                )
                if not case_dirs and not url_path.is_file():
                    continue

                url_text = _read_text(url_path) if url_path.is_file() else ""
                node_id = int(_URL_NID_RE.search(url_text).group(1)) if _URL_NID_RE.search(url_text) else None
                course_id = int(_URL_CID_RE.search(url_text).group(1)) if _URL_CID_RE.search(url_text) else None
                info_text = _read_text(info_path) if info_path.is_file() else ""
                statement, statement_source = self._statement_for_group(
                    node_id=node_id, course_id=course_id, info_text=info_text, options=options,
                )

                source_ref = f"{options['dataset']}/{group_dir.name}"
                defaults = {
                    "dataset": options["dataset"],
                    "name": group_dir.name,
                    "dl_node_id": node_id,
                    "dl_course_id": course_id,
                    "statement": statement,
                    "statement_source": statement_source,
                    "statement_updated_at": timezone.now() if statement else None,
                }
                if language is not None:
                    defaults["programming_language"] = language
                if topic is not None:
                    defaults["topic_name"] = topic.topic_name_ru

                group = FindErrorCaseGroup.objects.filter(source_ref=source_ref).first()
                if group is None:
                    group = FindErrorCaseGroup.objects.create(source_ref=source_ref, **defaults)
                    created_groups += 1
                    self.stdout.write(f"[группа] создана: {group_dir.name}")
                else:
                    changed = False
                    for field, value in defaults.items():
                        if field == "statement_updated_at":
                            continue
                        if value is not None and getattr(group, field) != value:
                            setattr(group, field, value)
                            changed = True
                    if group.statement != statement:
                        # Метка времени — только при реальной смене условия,
                        # иначе каждый повторный импорт «обновлял» бы группу.
                        group.statement_updated_at = defaults["statement_updated_at"]
                    if changed:
                        group.save()
                        updated_groups += 1
                    self.stdout.write(f"[группа] {group_dir.name}: {len(case_dirs)} кейсов")

                for case_dir in case_dirs:
                    code = _read_text(case_dir / "1.mpc")
                    labels = []
                    for label_path in sorted(case_dir.iterdir()):
                        match = _ERROR_FILE_RE.match(label_path.name)
                        if not match:
                            continue
                        code_num = int(match.group(1))
                        if code_num not in VALID_ERROR_CODES:
                            self.stdout.write(self.style.WARNING(
                                f"  {case_dir}: код {code_num} вне каталога — пропущен."
                            ))
                            continue
                        labels.append((code_num, _read_text(label_path).strip()))
                    labels.sort(key=lambda pair: pair[0])
                    digest = _source_hash(code, labels)
                    case_ref = f"{source_ref}/{case_dir.name}"

                    existing = FindErrorCase.objects.filter(source_ref=case_ref).first()
                    if existing is not None and not options["force"] and existing.source_hash == digest:
                        skipped_cases += 1
                        continue

                    if options["dry_run"]:
                        action = "обновить" if existing else "создать"
                        self.stdout.write(
                            f"  [кейс] {action}: {case_dir.name} "
                            f"(метки: {', '.join(str(n) for n, _ in labels) or '—'})"
                        )
                        continue

                    case = existing
                    if case is None:
                        case = FindErrorCase.objects.create(
                            group=group,
                            name=case_dir.name,
                            source_ref=case_ref,
                            code=code,
                            status=FindErrorCase.STATUS_READY if statement else FindErrorCase.STATUS_DRAFT,
                            source_hash=digest,
                        )
                        created_cases += 1
                    else:
                        case.group = group
                        case.name = case_dir.name
                        case.code = code
                        case.source_hash = digest
                        case.save(update_fields=["group", "name", "code", "source_hash", "updated_at"])
                        case.labels.all().delete()
                        updated_cases += 1
                    FindErrorCaseLabel.objects.bulk_create([
                        FindErrorCaseLabel(case=case, code=code_num, text=text)
                        for code_num, text in labels
                    ])

            if options["dry_run"]:
                transaction.set_rollback(True)

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("Dry-run: изменения не сохранены."))
            return
        self.stdout.write(self.style.SUCCESS(
            f"Готово: групп +{created_groups}/~{updated_groups}, "
            f"кейсов +{created_cases}/~{updated_cases}, пропущено {skipped_cases}."
        ))
