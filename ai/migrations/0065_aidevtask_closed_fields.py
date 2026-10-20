"""Таск-доска: закрытие — архив вместо удаления (вкладка «Закрытые»).

Прежде закрытый таск удалялся с доски (task.delete()), и историю закрытий
не было видно. Теперь закрытие уводит таск с доски, но сохраняет строку:
closed_at/closed_by — в архиве вкладки «Закрытые», reopen возвращает таск
на доску. taken_by остаётся записанным (кто выполнил). Легаси-закрытых
строк в БД нет (они удалялись) — обратного переноса не требуется.
"""
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("ai", "0064_aidevtask_kind_scoped_priority_no"),
    ]

    operations = [
        migrations.AddField(
            model_name="aidevtask",
            name="closed_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="closed_dev_tasks",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Кто закрыл",
            ),
        ),
        migrations.AddField(
            model_name="aidevtask",
            name="closed_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Закрыт"),
        ),
    ]