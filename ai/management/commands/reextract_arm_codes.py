"""Ретроспективный ре-экстракт кода из сырых ответов моделей (вариант B).

Заполняет поле `code` у результатов ARM-прогонов, где экстракция тогда
провалилась («Не удалось извлечь код из ответа модели», status=error/failed,
verdict != solved), используя ТЕКУЩИЙ конвейер структурных экстракторов
(services/code_carver.py — С-МПА-aware: не режет `jmp begin` и дата-секцию).

Никогда не трогает решённые (verdict='solved'): их код уже прошёл DL-тест —
ре-экстракт не перепроверяет код в DL, только восстанавливает файл по сырому
ответу. Статус/вердикт не меняет; при заполнении кода у пары с пустым кодом
dl_comment «Не удалось извлечь код…» заменяется на честную пометку.

Запуск:
    python manage.py reextract_arm_codes --run 100            # dry-run: только показать
    python manage.py reextract_arm_codes --run 100 --commit
    python manage.py reextract_arm_codes --all                 # все прогоны с «не извлечённых»
    python manage.py reextract_arm_codes --refresh-unsolved    # пересечь и код с кодом (verdict != solved)
"""

import json

from django.core.management.base import BaseCommand, CommandError

from ai.models import AIModelTestResult, AIModelTestRun
from ai.arm_runner import _extract_code_from_response
from ai.services.code_carver import has_language_marker

_NO_EXTRACT_MARK = "Не удалось извлечь код из ответа модели"
_REEXTRACT_MARK = "код восстановлен ре-экстрактом (DL не перепроверялся)"


class Command(BaseCommand):
    help = "Ре-экстракт кода из сырых ответов: только не-решённые пары; dry-run по умолчанию."

    def add_arguments(self, parser):
        parser.add_argument("--run", action="append", type=int, dest="run_ids",
                            help="pk прогона (можно несколько раз)")
        parser.add_argument("--all", action="store_true", dest="all_runs",
                            help="все результаты с пустым кодом и вердикт != solved")
        parser.add_argument("--commit", action="store_true",
                            help="записать изменения (по умолчанию только показать)")
        parser.add_argument("--refresh-unsolved", action="store_true",
                            help="у непустых непроверенных кодов (verdict != solved) тоже пересчитать")

    def handle(self, *args, **opts):
        qs = AIModelTestResult.objects.exclude(raw_response="").select_related("run")
        if opts["run_ids"]:
            qs = qs.filter(run_id__in=opts["run_ids"])
        elif opts["all_runs"]:
            qs = qs.filter(verdict="failed", code="")
        elif not opts["refresh_unsolved"]:
            qs = qs.filter(verdict="failed", code="")
        if opts["refresh_unsolved"]:
            qs = qs.filter(verdict="failed")

        changed = kept = 0
        for r in qs.order_by("run_id", "id").iterator():
            if r.verdict == "solved":
                continue  # их код уже прошёл DL — рука не берём
            raw = r.raw_response or ""
            ext = (r.file_extension_snapshot or "").strip()
            new_code = _extract_code_from_response(raw, file_extension=ext).strip()
            old_code = (r.code or "").strip()
            if new_code and not has_language_marker(new_code, ext):
                # Гейт мусора: без структурного маркера языка в code не пишем
                new_code = ""
            if not new_code or new_code == old_code:
                kept += 1
                continue
            if not opts["commit"]:
                self.stdout.write(
                    f"[dry] res#{r.pk} run#{r.run_id} ext={ext}\n"
                    f"   старый: {old_code[:90]!r}\n"
                    f"   новый:  {new_code[:90]!r}"
                )
            else:
                r.code = new_code
                if (r.dl_comment or "").strip() == _NO_EXTRACT_MARK:
                    r.dl_comment = _REEXTRACT_MARK
                r.save(update_fields=["code", "dl_comment"])
                self.stdout.write(f"[commit] res#{r.pk} run#{r.run_id}: код записан ({len(new_code)} симв.)")
            changed += 1

        head = "ЗАПИСАНО" if opts["commit"] else "DRY-RUN (без записи; добавь --commit)"
        self.stdout.write(self.style.SUCCESS(f"{head}: изменено {changed}, без изменений {kept}"))