"""Удаление фичи «Регрессионные тесты промптов» (feat. 2026-10).

Модели PromptTestCase/PromptTestRun/PromptTestResult и их данные удаляются
целиком; строка права «page.prompt_regression» выпиливается из
AIAdminGroupPermission (M2M-члены встроенных групп каскадом).
"""

from django.db import migrations


def delete_regression_permission(apps, schema_editor):
    AIAdminGroupPermission = apps.get_model("ai", "AIAdminGroupPermission")
    AIAdminGroupPermission.objects.filter(key="page.prompt_regression").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("ai", "0061_aidevtask_priority_no_and_kind"),
    ]

    operations = [
        migrations.DeleteModel(name="PromptTestResult"),
        migrations.DeleteModel(name="PromptTestRun"),
        migrations.DeleteModel(name="PromptTestCase"),
        migrations.RunPython(delete_regression_permission, migrations.RunPython.noop),
    ]