"""Точечное удаление пакетного прогона ARM из БД + вычет вклада из AIModelStats.

Для случая «прогон удался плохим»: удаляет AIModelTestRun (CASCADE — все
AIModelTestResult), связанную запись журнала AIRequestLog (message
«Batch solve run/rerun <hex>»; её CASCADE уносит и закрепления
AIPinnedBatchRun) и вычитает вклад пар прогона из накопленной статистики
моделей — зеркально ``record_batch_solve_stats``. Вклад вычитается только у
прогонов с ``run_params.record_stats`` — остальные в статистику не вносили.

Кэш решённых задач (TaskSolution) по умолчанию НЕ трогается: записи создаются
только для пар, реально прошедших DL-тестирование. Флаг ``--drop-solutions``
удаляет и решения, созданные этим прогоном (test_log = лог прогона).

Запуск в контейнере:
    docker exec dl_ai_web python manage.py delete_arm_run --list
    docker exec dl_ai_web python manage.py delete_arm_run --run-id <hex>          # dry-run
    docker exec dl_ai_web python manage.py delete_arm_run --run-id <hex> --yes
"""
import re

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import F, Value
from django.db.models.functions import Greatest

from ai.constants import MOSCOW_TZ
from ai.models import AIModelStats, AIModelTestRun, AIRequestLog, TaskSolution

# run_id в сообщении лога (та же форма, что ai/admin/logs.py::_BATCH_RUN_ID_RE).
_LOG_RUN_RE = re.compile(r"Batch solve (?:rerun|run) ([0-9a-f]{32})")


class Command(BaseCommand):
    help = (
        "Удалить один batch-прогон ARM из БД (прогон + результаты + запись "
        "журнала) и вычесть его вклад из накопленной статистики моделей "
        "AIModelStats. Сначала посмотрите список: --list; удаление выполняется "
        "только с --yes (без флага — только показ что будет удалено)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--list", action="store_true",
                            help="Показать последние 20 batch-прогонов и выйти.")
        parser.add_argument("--run-id", default="",
                            help="run_id прогона (hex32) или pk строки AIModelTestRun.")
        parser.add_argument("--yes", action="store_true",
                            help="Реально удалить (по умолчанию — только отчёт).")
        parser.add_argument("--drop-solutions", action="store_true",
                            help="Также удалить решения из кэша «Решённых задач», "
                                 "записанные этим прогоном.")

    # === Вычет статистики: зеркальный record_batch_solve_stats ===

    @staticmethod
    def _per_model_deltas(results):
        per_model = {}
        for res in results:
            key = res.model_key or ""
            if not key:
                continue
            bucket = per_model.setdefault(
                key, {"solved": 0, "total": 0, "dur_n": 0, "dur_sum": 0.0})
            verdict = res.verdict or ""
            if verdict in ("solved", "failed"):
                bucket["total"] += 1
                if verdict == "solved":
                    bucket["solved"] += 1
            duration = res.duration_seconds
            if duration is not None and verdict != "skipped":
                bucket["dur_n"] += 1
                bucket["dur_sum"] += float(duration or 0.0)
        return per_model

    def _subtract_stats(self, per_model, apply=False):
        """Вычесть вклад пар из AIModelStats с полом на нуле (Greatest).

        ``apply=False`` (dry-run) только показывает дельты — БД не трогает.
        """
        for key, b in per_model.items():
            if apply:
                updated = AIModelStats.objects.filter(model_key=key).update(
                    solved_count=Greatest(F("solved_count") - b["solved"], Value(0)),
                    total_count=Greatest(F("total_count") - b["total"], Value(0)),
                    duration_count=Greatest(F("duration_count") - b["dur_n"], Value(0)),
                    duration_sum=Greatest(F("duration_sum") - b["dur_sum"], Value(0.0)),
                )
            else:
                updated = AIModelStats.objects.filter(model_key=key).count()
            self.stdout.write(
                f"  статистика {key}: −{b['solved']} решено, −{b['total']} пар, "
                f"−{b['dur_sum']:.1f} сек (строк таблицы: {updated})"
            )

    def handle(self, *list_args, **options):
        if options["list"]:
            qs = AIModelTestRun.objects.filter(run_type="batch").order_by("-started_at")[:20]
            for r in qs:
                name = r.run_name or (r.run_params or {}).get("run_name") or "—"
                contributed = "да" if (r.run_params or {}).get("record_stats") else "—"
                started = r.started_at.astimezone(MOSCOW_TZ)
                self.stdout.write(
                    f"{r.run_id}  {started:%Y-%m-%d %H:%M}  {name[:40]:40}  "
                    f"{r.status:10}  пар={r.results.count():4}  в статистике: {contributed}"
                )
            return

        run_ref = (options["run_id"] or "").strip()
        if not run_ref:
            raise CommandError("Укажите --run-id (см. --list) или --list.")
        run = AIModelTestRun.objects.filter(run_id=run_ref).first()
        if run is None and run_ref.isdigit():
            run = AIModelTestRun.objects.filter(pk=run_ref).first()
        if run is None:
            raise CommandError(f"Прогон «{run_ref}» не найден (см. --list).")

        results = list(run.results.all())
        log_rows = [l for l in AIRequestLog.objects.filter(message__contains=run.run_id)
                    if _LOG_RUN_RE.search(l.message or "")]
        solutions = TaskSolution.objects.filter(
            test_log__in=[l.pk for l in log_rows]) if options["drop_solutions"] \
            else TaskSolution.objects.none()

        self.stdout.write(f"Прогон {run.run_id} «{run.run_name or '—'}» "
                          f"от {run.started_at.astimezone(MOSCOW_TZ):%Y-%m-%d %H:%M}, "
                          f"статус {run.status}, пар={len(results)}")
        self.stdout.write(f"Записей журнала: {[l.pk for l in log_rows]}")
        self.stdout.write(f"Решений в кэше «Решённых задач» (при --drop-solutions): "
                          f"{solutions.count() if log_rows else 0}")
        if (run.run_params or {}).get("record_stats"):
            self.stdout.write("Выбирается из AIModelStats:")
            self._subtract_stats(self._per_model_deltas(results), apply=options["yes"])
        else:
            self.stdout.write("Вклад в AIModelStats не вычитается "
                              "(«заносить результаты в БД» не был включён).")

        if not options["yes"]:
            self.stdout.write(self.style.WARNING(
                "DRY-RUN: ничего не удалено. Повторите с --yes."))
            return

        with transaction.atomic():
            if options["drop_solutions"] and log_rows:
                count, _ = TaskSolution.objects.filter(
                    test_log__in=[l.pk for l in log_rows]).delete()
                self.stdout.write(f"Удалено решений из кэша: {count}")
            for l in log_rows:
                l.delete()  # CASCADE забирает и закрепления прогона
            run.delete()
        self.stdout.write(self.style.SUCCESS(
            f"Прогон {run.run_id} удалён из БД."))