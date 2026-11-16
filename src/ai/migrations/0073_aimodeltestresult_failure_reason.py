"""Новая причина сбоя «Ошибка DL: не авторизован» (dl_unauthorized).

Изменение ТОЛЬКО в состоянии модели (choices) — на уровне БД ничего не
меняется: Django не создаёт CHECK-констрейнт на choices, колонка уже
max_length=16 (значение «dl_unauthorized» — 15 символов, влезает).

Почему SeparateDatabaseAndState, а не обычный AlterField: на SQLite любое
изменение поля таблицы = пересоздание таблицы, а у AIModelTestResult в
Meta.indexes есть postgres-only GinIndex (gin_trgm_ops) — пересоздание
падало бы с «near "gin_trgm_ops": syntax error» (тестовый SQLite--suite).
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("ai", "0072_task_tree_path"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[],
            state_operations=[
                migrations.AlterField(
                    model_name="aimodeltestresult",
                    name="failure_reason",
                    field=models.CharField(
                        blank=True,
                        choices=[
                            ("model_error", "Сбой ответа модели"),
                            ("no_code", "Не выкушен код"),
                            ("compile_error", "Ошибка компиляции"),
                            ("wrong_answer", "Неправильный ответ"),
                            ("undefined", "Причина не распознана"),
                            ("skipped", "Пропущено"),
                            ("dl_unauthorized", "Ошибка DL: не авторизован"),
                        ],
                        db_index=True,
                        default="",
                        max_length=16,
                        null=True,
                        verbose_name="Причина сбоя",
                    ),
                ),
            ],
        ),
    ]
