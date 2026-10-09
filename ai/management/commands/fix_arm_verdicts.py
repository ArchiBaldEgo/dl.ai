"""Разовая чистка вердиктов ARM-результатов по DL-комментариям (легаси).

До коммита 8e0f14e (2026-09-15) вердикт ставился без приоритета маркеров
провала, и часть строк с «Ошибка компиляции…» в dl_comment записалась как
solved (~70 строк). Задача команды: пересчитать вердикт для строк, где он
ПРОТИВОРЕЧИТ dl_comment по КАНОНИЧНОЙ функции dl_verdict_from_comment
(маркеры провала приоритетны, нераспознанное — failed).

- solved → failed: dl_comment содержит маркер провала (ошибка/неверн/...).
- failed → solved: dl_comment содержит чистый успех («все тесты…»/«задача
  решена»/accepted...) без маркеров провала — редкий обратный кейс.
- Строки с пустым dl_comment НЕ трогаются (им вердикт честно не определим).
- status пересчитывается вместе с вердиктом (ok/error), как делает воркер.

Запуск:
    python manage.py fix_arm_verdicts            # dry-run: таблица изменений
    python manage.py fix_arm_verdicts --commit   # применить
"""

from django.core.management.base import BaseCommand

from ai.dl_api_client import dl_verdict_from_comment
from ai.models import AIModelTestResult


class Command(BaseCommand):
    help = "Пересчитать вердикты по dl_comment (dl_verdict_from_comment); dry-run по умолчанию."

    def add_arguments(self, parser):
        parser.add_argument("--commit", action="store_true",
                            help="записать изменения (по умолчанию только показать)")
        parser.add_argument("--run", action="append", type=int, dest="run_ids",
                            help="ограничиться прогонами (pk), можно несколько раз")

    def handle(self, *args, **opts):
        qs = AIModelTestResult.objects.filter(verdict__isnull=False).exclude(dl_comment="")
        if opts["run_ids"]:
            qs = qs.filter(run_id__in=opts["run_ids"])

        to_changed = []
        for r in qs.only("id", "run_id", "verdict", "status", "dl_comment", "model_key").order_by("id"):
            fresh = dl_verdict_from_comment(r.dl_comment)
            if fresh != r.verdict:
                to_changed.append((r, fresh))

        solved_to_failed = [(r, v) for r, v in to_changed if r.verdict == "solved"]
        failed_to_solved = [(r, v) for r, v in to_changed if r.verdict == "failed"]
        self.stdout.write(f"Проверено строк: {qs.count()}; противоречий: {len(to_changed)} "
                          f"(solved→failed: {len(solved_to_failed)}, failed→solved: {len(failed_to_solved)})")
        for r, fresh in to_changed[:40]:
            self.stdout.write(
                f"  res#{r.id} run#{r.run_id} {r.model_key} {r.verdict}→{fresh}: {r.dl_comment[:110]}"
            )

        if not opts["commit"]:
            self.stdout.write(self.style.SUCCESS("DRY-RUN (без записи; добавь --commit)"))
            return

        from django.utils import timezone
        now = timezone.now()
        for r, fresh in to_changed:
            r.verdict = fresh
            r.status = "ok" if fresh == "solved" else "error"
            r.save(update_fields=["verdict", "status"])
        head = self.style.SUCCESS(f"ЗАПИСАНО: {len(to_changed)} строк (now {now:%H:%M UTC})")
        self.stdout.write(head)