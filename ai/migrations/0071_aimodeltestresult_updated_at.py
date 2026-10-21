from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("ai", "0070_education_names_from_dl"),
    ]

    operations = [
        migrations.AddField(
            model_name="aimodeltestresult",
            name="updated_at",
            # Пишется явно (arm_runner._write_pair_row), а не auto_now: на
            # SQLite добавление auto_now-поля пересоздаёт таблицу, а у модели
            # есть postgres-only GinIndex (gin_trgm_ops) — пересоздание
            # падало бы с ошибкой SQL. Nullable + без default → обычный
            # ALTER TABLE ADD COLUMN, без пересоздания таблицы.
            field=models.DateTimeField(blank=True, null=True, verbose_name="Обновлён"),
        ),
    ]
