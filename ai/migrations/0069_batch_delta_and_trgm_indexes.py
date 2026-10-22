"""Машина проверки (Дельта) в паре + триграмм-индексы для быстрых фильтров.

Две независимые вещи:

1. ``AIModelTestResult.delta_id`` / ``delta_name`` — на какой машине DL
   проверял решение (``get-solution-result`` → ``deltaId``/``deltaName``).
   Раньше приложение эти поля ВЫБРАСЫВАЛО — фактическую машину проверки
   было не видно нигде.

2. Expression-индексы на ``UPPER(col)`` с ``gin_trgm_ops`` — ускорение
   фильтров журнала по фамилии/логину/id и выборки пар по названию модели.
   Django генерирует для ``icontains`` предикат
   ``UPPER(col::text) LIKE UPPER(%p%)`` — обычный индекс по колонке его НЕ
   обслуживает, нужен индекс именно по выражению ``UPPER(col)``.

   Почему не ILIKE: в SQLite (на нём идёт тестовая сюита) его нет, и в
   Django 5.1 нет лукапа ``__ilike`` — фильтр остаётся ``icontains``.

Почему индексы только в state, а не в БД на SQLite: ``gin_trgm_ops`` —
   postgres-специфичный оператор классов, SQLite его не знает. Поэтому
   БД-часть миграции сделана через ``RunSQL`` с проверкой вендора, а
   django-состояние — обычными ``AddIndex`` (их видит ``makemigrations
   --check`` и они не расходятся с моделями).
"""

from django.contrib.postgres.indexes import GinIndex, OpClass
from django.contrib.postgres.operations import CreateExtension
from django.db import migrations, models
from django.db.models.functions import Upper

# SQL создаёт индексы ТОЛЬКО на Postgres (на SQLite gin_trgm_ops не существует).
# IF NOT EXISTS — миграция идемпотентна (ext уже может стоять в БД).
_CREATE_TRGM_INDEXES = """
CREATE INDEX IF NOT EXISTS ai_log_uname_trgm_idx
    ON ai_airequestlog USING gin (UPPER(user_full_name) gin_trgm_ops);
CREATE INDEX IF NOT EXISTS ai_log_login_trgm_idx
    ON ai_airequestlog USING gin (UPPER(username) gin_trgm_ops);
CREATE INDEX IF NOT EXISTS ai_log_extid_trgm_idx
    ON ai_airequestlog USING gin (UPPER(external_user_id) gin_trgm_ops);
CREATE INDEX IF NOT EXISTS ai_mtr_model_title_trgm_idx
    ON ai_ai_model_test_result USING gin (UPPER(model_title) gin_trgm_ops);
"""

_DROP_TRGM_INDEXES = """
DROP INDEX IF EXISTS ai_log_uname_trgm_idx;
DROP INDEX IF EXISTS ai_log_login_trgm_idx;
DROP INDEX IF EXISTS ai_log_extid_trgm_idx;
DROP INDEX IF EXISTS ai_mtr_model_title_trgm_idx;
"""


def _create_trgm_indexes(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(_CREATE_TRGM_INDEXES)


def _drop_trgm_indexes(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(_DROP_TRGM_INDEXES)


class Migration(migrations.Migration):

    dependencies = [
        ("ai", "0068_perf_indexes"),
    ]

    operations = [
        # Расширение для триграмм-индексов (pg_trgm). В БД уже стоит,
        # CreateExtension идемпотентен и на SQLite/SQLite-safe (выходит рано).
        CreateExtension("pg_trgm"),
        # --- Машина проверки (Дельта) в паре прогона ---
        migrations.AddField(
            model_name="aimodeltestresult",
            name="delta_id",
            field=models.IntegerField(blank=True, null=True, verbose_name="DL: машина (id)"),
        ),
        migrations.AddField(
            model_name="aimodeltestresult",
            name="delta_name",
            field=models.CharField(blank=True, default="", max_length=255, verbose_name="DL: машина"),
        ),
        # --- Триграмм-индексы (state — для makemigrations --check) ---
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunPython(_create_trgm_indexes, _drop_trgm_indexes),
            ],
            state_operations=[
                migrations.AddIndex(
                    model_name="aimodeltestresult",
                    index=GinIndex(
                        OpClass(Upper("model_title"), "gin_trgm_ops"),
                        name="ai_mtr_model_title_trgm_idx",
                    ),
                ),
                migrations.AddIndex(
                    model_name="airequestlog",
                    index=GinIndex(
                        OpClass(Upper("user_full_name"), "gin_trgm_ops"),
                        name="ai_log_uname_trgm_idx",
                    ),
                ),
                migrations.AddIndex(
                    model_name="airequestlog",
                    index=GinIndex(
                        OpClass(Upper("username"), "gin_trgm_ops"),
                        name="ai_log_login_trgm_idx",
                    ),
                ),
                migrations.AddIndex(
                    model_name="airequestlog",
                    index=GinIndex(
                        OpClass(Upper("external_user_id"), "gin_trgm_ops"),
                        name="ai_log_extid_trgm_idx",
                    ),
                ),
            ],
        ),
    ]
