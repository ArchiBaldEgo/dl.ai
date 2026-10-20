"""Бэкфилл причины сбоя для старых прогонов ARM (легаси-строки).

Таксономия сбоев (AIModelTestResult.failure_reason) появилась в 2026-10, а
старые прогоны записаны без неё: по ним причина выводится только по
«dl_comment», который хранился лишь у части строк (у «модельных» сбоев
комментария нет вовсе).

Команда пересчитывает причину для строк, где она пуста:
* строки с НЕПУСТЫМ dl_comment — по каноническому классификатору
  ai/dl_api_client.py::dl_failure_reason_from_comment (ошибка компиляции
  vs неправильный ответ — по тексту комментария);
* строки с пустым dl_comment и вердиктом failed — «сбой ответа модели»,
  если провайдерская ошибка была (status=error и нет кода), иначе
  «причина не распознана»;
* решённые пары (verdict=solved) остаются с пустой причиной.

Запуск:
    python manage.py backfill_arm_failure_reasons             # dry-run
    python manage.py backfill_arm_failure_reasons --commit    # применить
"""

from django.core.management.base import BaseCommand

from ai.dl_api_client import dl_failure_reason_from_comment
from ai.models import AIModelTestResult

# Причины, которые НЕ восстанавливаются из текста (значения из моделей).
_REASON_MODEL_ERROR = "model_error"
_REASON_UNDEFINED = "undefined"


def _reason_for(row) -> str:
    """Причина сбоя для строки по её текстовым данным."""
    verdict = (row.get("verdict") or "").strip()
    if verdict == "solved":
        return ""
    comment = (row.get("dl_comment") or "").strip()
    if comment:
        return dl_failure_reason_from_comment(comment) or _REASON_UNDEFINED
    # Комментария нет — провалилась сама модель (нет кода) или запись легаси.
    if not (row.get("code") or "").strip():
        return _REASON_MODEL_ERROR
    return _REASON_UNDEFINED


class Command(BaseCommand):
    help = "Восстановить причину сбоя в старых прогонах ARM (dry-run по умолчанию)."

    def add_arguments(self, parser):
        parser.add_argument("--commit", action="store_true",
                            help="записать изменения (по умолчанию только показать)")
        parser.add_argument("--run", action="append", type=int, dest="run_ids",
                            help="ограничиться прогонами (pk), можно несколько раз")

    def handle(self, *args, **opts):
        qs = AIModelTestResult.objects.all()
        if opts["run_ids"]:
            qs = qs.filter(run_id__in=opts["run_ids"])
        # Пустая причина = строки, которые ещё не бэкфиллены.
        qs = qs.filter(failure_reason__in=["", None]).exclude(verdict="solved")

        rows = list(
            qs.only("id", "run_id", "model_key", "verdict", "status", "dl_comment", "code")
            .order_by("id")
        )
        to_write = []
        counts = {}
        for row in rows:
            reason = _reason_for({
                "verdict": row.verdict,
                "dl_comment": row.dl_comment,
                "code": row.code,
            })
            if not reason:
                continue
            counts[reason] = counts.get(reason, 0) + 1
            to_write.append((row, reason))

        self.stdout.write(
            f"Проверено строк без причины: {len(rows)}; к записи: {len(to_write)}"
        )
        for reason, count in sorted(counts.items(), key=lambda kv: -kv[1]):
            label = AIModelTestResult.FAILURE_REASON_LABELS.get(reason, reason)
            self.stdout.write(f"  {label}: {count}")
        for row, reason in to_write[:40]:
            self.stdout.write(
                f"  res#{row.id} run#{row.run_id} {row.model_key} → {reason}"
            )

        if not opts["commit"]:
            self.stdout.write(self.style.SUCCESS("DRY-RUN (без записи; добавь --commit)"))
            return

        from django.utils import timezone
        now = timezone.now()
        for row, reason in to_write:
            row.failure_reason = reason
            row.save(update_fields=["failure_reason"])
        self.stdout.write(
            self.style.SUCCESS(f"ЗАПИСАНО: {len(to_write)} строк (now {now:%H:%M UTC})")
        )
