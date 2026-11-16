"""Название прогона — колонка AIModelTestRun.run_name вместо AIAppSettings.batch_run_names.

Ручные названия прогонов (/arm/solve/) переносятся с singleton-словаря
«ISO-дата-время → название» на сам прогон: все читатели уже имеют
AIModelTestRun, а словарь на странице «Настройка ИИ-приложения» не нужен.
"""
from django.db import migrations, models


def backfill_run_names(apps, schema_editor):
    AIModelTestRun = apps.get_model("ai", "AIModelTestRun")
    AIAppSettings = apps.get_model("ai", "AIAppSettings")
    settings_row = AIAppSettings.objects.filter(pk=1).first()
    names = dict(getattr(settings_row, "batch_run_names", None) or {})
    if not names:
        return
    from django.utils.dateparse import parse_datetime

    for run in AIModelTestRun.objects.all().only("id", "started_at", "run_params"):
        # Точный ключ (ISO localtime старта) → фолбэк на название из run_params.
        name = names.get(run.started_at.isoformat() if run.started_at else "")
        if not name:
            name = (run.run_params or {}).get("run_name")
        if name:
            run.run_name = str(name)[:255]
            run.save(update_fields=["run_name"])


class Migration(migrations.Migration):

    dependencies = [
        ("ai", "0044_prompt_mode"),
    ]

    operations = [
        migrations.AddField(
            model_name="aimodeltestrun",
            name="run_name",
            field=models.CharField(blank=True, default="", max_length=255, verbose_name="Название прогона"),
        ),
        migrations.RunPython(backfill_run_names, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name="aiappsettings",
            name="batch_run_names",
        ),
    ]