"""Переименование расширения C-MPA с ``.cmp`` на ``.mpc``.

DL REST send-solution для задач C-MPA принимает расширение ``.mpc`` (а не
``.cmp``). Код и ``SOLVE_EXTENSION_CHOICES`` уже переведены на ``.mpc`` — эта
data-миграция приводит существующие записи ``Task.file_extension`` к новому
значению, чтобы авто-определение и batch-solve работали консистентно.

Исторические ``AIModelTestResult.file_extension_snapshot`` НЕ трогаем: это
журнал прогонов, и download старых результатов должен отдавать то расширение,
с которым прогон реально выполнялся (``.cmp``).
"""

from django.db import migrations


def rename_extension(apps, schema_editor):
    Task = apps.get_model("ai", "Task")
    Task.objects.filter(file_extension=".cmp").update(file_extension=".mpc")


def reverse_rename(apps, schema_editor):
    # Обратный переход не имеет смысла (.cmp больше не используется кодом),
    # но реализуем для симметрии.
    Task = apps.get_model("ai", "Task")
    Task.objects.filter(file_extension=".mpc").update(file_extension=".cmp")


class Migration(migrations.Migration):

    dependencies = [
        ("ai", "0031_aimodeltestresult_code_aimodeltestresult_dl_comment_and_more"),
    ]

    operations = [
        migrations.RunPython(rename_extension, reverse_code=reverse_rename),
    ]