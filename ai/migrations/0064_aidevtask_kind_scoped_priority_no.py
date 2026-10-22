"""AIDevTask: у «Хотелок» тоже нумерация — своя последовательность с №1
(паритет с фронтом), уникальность теперь по (kind, priority_no).

- Хотелки-легаси (NULL-номера) получают номера 1..M по возрастанию
  (created_at, id); фронт не трогается.
- Частичный UniqueConstraint меняется с (priority_no) на (kind,
  priority_no): виды нумеруются независимо, номер 1 у фронта и у хотолки
  больше не конфликтуют. Констрейнт возвращается ПОСЛЕ бэкфилла — иначе
  он падал бы на переходных двойных «№1».
"""

from django.db import migrations, models
from django.db.models import Q


def _renumber_wishes(apps, schema_editor):
    Task = apps.get_model("ai", "AIDevTask")
    wishes = list(
        Task.objects.filter(kind="wish", priority_no__isnull=True)
        .order_by("created_at", "id")
    )
    for number, task in enumerate(wishes, start=1):
        task.priority_no = number
    Task.objects.bulk_update(wishes, ["priority_no"])


class Migration(migrations.Migration):

    dependencies = [
        ("ai", "0063_tasks_modelstatus_out_of_baseline"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="aidevtask",
            name="ai_dev_task_priority_no_uniq",
        ),
        migrations.RunPython(_renumber_wishes, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="aidevtask",
            constraint=models.UniqueConstraint(
                condition=Q(priority_no__isnull=False),
                fields=("kind", "priority_no"),
                name="ai_dev_task_priority_no_uniq",
            ),
        ),
    ]