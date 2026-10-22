"""AIDevTask: числовой приоритет (priority_no) + тип (kind) вместо Высокий/
Средний/Низкий.

- kind: front (default) / wish; хотелки — без нумерации.
- priority_no: уникальный номер доски (меньше = важнее), NULL у хотелок.
  Частичный UniqueConstraint добавляется ПОСЛЕ бэкфилла — иначе он падал бы
  на NULL/дубликатах переходного состояния.
- Бэкфилл: старая сортировка доски (Высокий → Средний → Низкий, внутри —
  свежие сверху) переносится в номера 1,2,3… — строка, отображавшаяся
  первой, получает 1.
- Legacy-поле priority удаляется.
"""

from django.db import migrations, models
import django.db.models


def _renumber_by_old_rank(apps, schema_editor):
    Task = apps.get_model("ai", "AIDevTask")
    # EXACT текущая сортировка доски: ранг Высокий=0/Средний=1/Низкий=2,
    # внутри — свежие сверху (см. старый _PRIORITY_RANK в ai/admin/tasks.py):
    # ключ сортировки — (ранг ASC, время DESC, id DESC).
    rank = {"high": 0, "normal": 1, "low": 2}
    ordered = sorted(
        Task.objects.all(),
        key=lambda t: (rank.get(t.priority, 3), -t.created_at.timestamp(), -t.pk),
    )
    for number, task in enumerate(ordered, start=1):
        task.priority_no = number
        task.kind = "front"
        task.save(update_fields=["priority_no", "kind"])


class Migration(migrations.Migration):

    dependencies = [
        ("ai", "0060_ai_access_groups"),
    ]

    operations = [
        migrations.AddField(
            model_name="aidevtask",
            name="kind",
            field=models.CharField(
                choices=[("front", "Фронт работы"), ("wish", "Хотелки")],
                default="front", max_length=10, verbose_name="Тип",
            ),
        ),
        migrations.AddField(
            model_name="aidevtask",
            name="priority_no",
            field=models.PositiveIntegerField(
                blank=True, null=True, verbose_name="Номер приоритета",
            ),
        ),
        migrations.RunPython(_renumber_by_old_rank, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name="aidevtask",
            name="priority",
        ),
        migrations.AddConstraint(
            model_name="aidevtask",
            constraint=models.UniqueConstraint(
                condition=django.db.models.Q(priority_no__isnull=False),
                fields=("priority_no",),
                name="ai_dev_task_priority_no_uniq",
            ),
        ),
        migrations.AlterModelOptions(
            name="aidevtask",
            options={
                "ordering": ("kind", "priority_no", "-created_at", "-id"),
                "verbose_name": "Таск (доска задач)",
                "verbose_name_plural": "Таски (доска задач)",
            },
        ),
    ]