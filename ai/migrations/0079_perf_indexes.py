"""Индексы под горячие запросы: составные btree + триграммы/GIN.

Что ускоряем (все — найденные при аудите места, а не «на всякий случай»):

* ``AIRequestLog(user, status, sent_at)`` — топ-модели пользователя
  (``ai/views.py::_get_user_top_model_keys``: success + срез по дате +
  ``ORDER BY -sent_at LIMIT 200``).
* ``AIRequestLog.model_names`` GIN ``jsonb_path_ops`` — фильтр журнала
  ``model_names__contains=[…]`` (``jsonb @> …``) без GIN сканирует всю таблицу.
* ``AIModelTestResult(run, updated_at, id)`` — лёгкий снапшот прогона
  (``ORDER BY -updated_at, -pk``), поллинг /arm/solve/ каждые 2 с.
* ``AIModelTestResult(run, verdict)`` — счётчики solved/failed/skipped.
* ``AIModelTestRun(run_type, started_at)`` — выборки batch-прогонов.
* ``ExternalDLAccount(created_at/updated_at)`` — фильтры/иерархия дат админки.
* ``UpdateLog(hidden, commit_date, created_at, id)`` — Meta.ordering + фильтр.
* ``Task(created_at)``, ``TaskSolution(updated_at)`` — Meta.ordering.
* ``AIDevTask(kind, priority_no, created_at, id)`` — Meta.ordering доски.
* ``AIUserModel(user, is_active)`` / ``(is_public, is_active)`` — селектор.
* ``AIDLApiRequest(status, created_at)`` — вкладки «актуальные/неактуальные».

Плюс триграммные GIN-индексы на поля, по которым админки ищут через
``icontains``: Django компилирует его в ``UPPER(col::text) LIKE UPPER(%p%)``,
и обычный индекс по колонке этот предикат НЕ обслуживает — нужен индекс по
выражению ``UPPER(col)`` с ``gin_trgm_ops``.

Почему GIN вынесены в ``SeparateDatabaseAndState``: ``gin_trgm_ops`` и
``jsonb_path_ops`` — postgres-специфичны, SQLite (на нём идёт тестовая сюита)
их не знает. Поэтому БД-часть — сырой SQL под проверкой вендора, а состояние
модели обновляется обычными ``AddIndex`` (их видит ``makemigrations --check``
и они не расходятся с ``Meta.indexes``). Тот же приём, что в 0069.

Для очень больших таблиц (журнал запросов) в проде индексы можно создать
вручную через ``CREATE INDEX CONCURRENTLY`` с теми же именами — миграция
идемпотентна (``IF NOT EXISTS``) и ничего не пересоздаст.
"""

import django.contrib.postgres.indexes
import django.db.models.functions.text
from django.conf import settings
from django.db import migrations, models

# --- GIN-часть: только Postgres -------------------------------------------
_CREATE_GIN_INDEXES = """
CREATE INDEX IF NOT EXISTS ai_log_model_names_gin_idx
    ON ai_airequestlog USING gin (model_names jsonb_path_ops);

CREATE INDEX IF NOT EXISTS ai_ext_dl_extid_trgm_idx
    ON ai_externaldlaccount USING gin (UPPER(external_user_id) gin_trgm_ops);
CREATE INDEX IF NOT EXISTS ai_ext_dl_login_trgm_idx
    ON ai_externaldlaccount USING gin (UPPER(external_login) gin_trgm_ops);
CREATE INDEX IF NOT EXISTS ai_ext_dl_fname_trgm_idx
    ON ai_externaldlaccount USING gin (UPPER(external_first_name) gin_trgm_ops);
CREATE INDEX IF NOT EXISTS ai_ext_dl_lname_trgm_idx
    ON ai_externaldlaccount USING gin (UPPER(external_last_name) gin_trgm_ops);

CREATE INDEX IF NOT EXISTS ai_prompt_name_trgm_idx
    ON ai_prompt USING gin (UPPER(prompt_name_ru) gin_trgm_ops);
CREATE INDEX IF NOT EXISTS ai_prompt_text_trgm_idx
    ON ai_prompt USING gin (UPPER(prompt_text_ru) gin_trgm_ops);

CREATE INDEX IF NOT EXISTS ai_task_name_trgm_idx
    ON ai_task USING gin (UPPER(name) gin_trgm_ops);
CREATE INDEX IF NOT EXISTS ai_task_statement_trgm_idx
    ON ai_task USING gin (UPPER(statement) gin_trgm_ops);

CREATE INDEX IF NOT EXISTS ai_tsol_extid_trgm_idx
    ON ai_task_solution USING gin (UPPER(external_user_id) gin_trgm_ops);
CREATE INDEX IF NOT EXISTS ai_tsol_model_trgm_idx
    ON ai_task_solution USING gin (UPPER(model_key) gin_trgm_ops);
CREATE INDEX IF NOT EXISTS ai_tsol_topic_trgm_idx
    ON ai_task_solution USING gin (UPPER(topic_name) gin_trgm_ops);
CREATE INDEX IF NOT EXISTS ai_tsol_prompt_trgm_idx
    ON ai_task_solution USING gin (UPPER(prompt_name) gin_trgm_ops);

CREATE INDEX IF NOT EXISTS ai_update_desc_trgm_idx
    ON ai_update_log USING gin (UPPER(description) gin_trgm_ops);
CREATE INDEX IF NOT EXISTS ai_update_author_trgm_idx
    ON ai_update_log USING gin (UPPER(author) gin_trgm_ops);
"""

_DROP_GIN_INDEXES = """
DROP INDEX IF EXISTS ai_log_model_names_gin_idx;
DROP INDEX IF EXISTS ai_ext_dl_extid_trgm_idx;
DROP INDEX IF EXISTS ai_ext_dl_login_trgm_idx;
DROP INDEX IF EXISTS ai_ext_dl_fname_trgm_idx;
DROP INDEX IF EXISTS ai_ext_dl_lname_trgm_idx;
DROP INDEX IF EXISTS ai_prompt_name_trgm_idx;
DROP INDEX IF EXISTS ai_prompt_text_trgm_idx;
DROP INDEX IF EXISTS ai_task_name_trgm_idx;
DROP INDEX IF EXISTS ai_task_statement_trgm_idx;
DROP INDEX IF EXISTS ai_tsol_extid_trgm_idx;
DROP INDEX IF EXISTS ai_tsol_model_trgm_idx;
DROP INDEX IF EXISTS ai_tsol_topic_trgm_idx;
DROP INDEX IF EXISTS ai_tsol_prompt_trgm_idx;
DROP INDEX IF EXISTS ai_update_desc_trgm_idx;
DROP INDEX IF EXISTS ai_update_author_trgm_idx;
"""


def _create_gin_indexes(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(_CREATE_GIN_INDEXES)


def _drop_gin_indexes(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(_DROP_GIN_INDEXES)


class Migration(migrations.Migration):

    dependencies = [
        ('ai', '0078_users_access_group'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # --- Обычные btree: создаются и на SQLite -------------------------
        migrations.AddIndex(
            model_name='aidevtask',
            index=models.Index(fields=['kind', 'priority_no', 'created_at', 'id'], name='ai_dev_task_order_idx'),
        ),
        migrations.AddIndex(
            model_name='aidlapirequest',
            index=models.Index(fields=['status', 'created_at'], name='ai_dl_api_req_status_idx'),
        ),
        migrations.AddIndex(
            model_name='aimodeltestresult',
            index=models.Index(fields=['run', 'updated_at', 'id'], name='ai_mtr_run_updated_idx'),
        ),
        migrations.AddIndex(
            model_name='aimodeltestresult',
            index=models.Index(fields=['run', 'verdict'], name='ai_mtr_run_verdict_idx'),
        ),
        migrations.AddIndex(
            model_name='aimodeltestrun',
            index=models.Index(fields=['run_type', 'started_at'], name='ai_model_test_run_type_idx'),
        ),
        migrations.AddIndex(
            model_name='airequestlog',
            index=models.Index(fields=['user', 'status', 'sent_at'], name='ai_log_user_status_sent_idx'),
        ),
        migrations.AddIndex(
            model_name='aiusermodel',
            index=models.Index(fields=['user', 'is_active'], name='ai_user_model_owner_idx'),
        ),
        migrations.AddIndex(
            model_name='aiusermodel',
            index=models.Index(fields=['is_public', 'is_active'], name='ai_user_model_public_idx'),
        ),
        migrations.AddIndex(
            model_name='externaldlaccount',
            index=models.Index(fields=['created_at'], name='ai_ext_dl_created_idx'),
        ),
        migrations.AddIndex(
            model_name='externaldlaccount',
            index=models.Index(fields=['updated_at'], name='ai_ext_dl_updated_idx'),
        ),
        migrations.AddIndex(
            model_name='task',
            index=models.Index(fields=['created_at'], name='ai_task_created_idx'),
        ),
        migrations.AddIndex(
            model_name='tasksolution',
            index=models.Index(fields=['updated_at'], name='ai_task_solution_updated_idx'),
        ),
        migrations.AddIndex(
            model_name='updatelog',
            index=models.Index(fields=['hidden', 'commit_date', 'created_at', 'id'], name='ai_update_log_order_idx'),
        ),
        # --- GIN: состояние для makemigrations, в БД — только Postgres ----
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunPython(_create_gin_indexes, _drop_gin_indexes),
            ],
            state_operations=[
                migrations.AddIndex(
                    model_name='airequestlog',
                    index=django.contrib.postgres.indexes.GinIndex(django.contrib.postgres.indexes.OpClass('model_names', name='jsonb_path_ops'), name='ai_log_model_names_gin_idx'),
                ),
                migrations.AddIndex(
                    model_name='externaldlaccount',
                    index=django.contrib.postgres.indexes.GinIndex(django.contrib.postgres.indexes.OpClass(django.db.models.functions.text.Upper('external_user_id'), 'gin_trgm_ops'), name='ai_ext_dl_extid_trgm_idx'),
                ),
                migrations.AddIndex(
                    model_name='externaldlaccount',
                    index=django.contrib.postgres.indexes.GinIndex(django.contrib.postgres.indexes.OpClass(django.db.models.functions.text.Upper('external_login'), 'gin_trgm_ops'), name='ai_ext_dl_login_trgm_idx'),
                ),
                migrations.AddIndex(
                    model_name='externaldlaccount',
                    index=django.contrib.postgres.indexes.GinIndex(django.contrib.postgres.indexes.OpClass(django.db.models.functions.text.Upper('external_first_name'), 'gin_trgm_ops'), name='ai_ext_dl_fname_trgm_idx'),
                ),
                migrations.AddIndex(
                    model_name='externaldlaccount',
                    index=django.contrib.postgres.indexes.GinIndex(django.contrib.postgres.indexes.OpClass(django.db.models.functions.text.Upper('external_last_name'), 'gin_trgm_ops'), name='ai_ext_dl_lname_trgm_idx'),
                ),
                migrations.AddIndex(
                    model_name='prompt',
                    index=django.contrib.postgres.indexes.GinIndex(django.contrib.postgres.indexes.OpClass(django.db.models.functions.text.Upper('prompt_name_ru'), 'gin_trgm_ops'), name='ai_prompt_name_trgm_idx'),
                ),
                migrations.AddIndex(
                    model_name='prompt',
                    index=django.contrib.postgres.indexes.GinIndex(django.contrib.postgres.indexes.OpClass(django.db.models.functions.text.Upper('prompt_text_ru'), 'gin_trgm_ops'), name='ai_prompt_text_trgm_idx'),
                ),
                migrations.AddIndex(
                    model_name='task',
                    index=django.contrib.postgres.indexes.GinIndex(django.contrib.postgres.indexes.OpClass(django.db.models.functions.text.Upper('name'), 'gin_trgm_ops'), name='ai_task_name_trgm_idx'),
                ),
                migrations.AddIndex(
                    model_name='task',
                    index=django.contrib.postgres.indexes.GinIndex(django.contrib.postgres.indexes.OpClass(django.db.models.functions.text.Upper('statement'), 'gin_trgm_ops'), name='ai_task_statement_trgm_idx'),
                ),
                migrations.AddIndex(
                    model_name='tasksolution',
                    index=django.contrib.postgres.indexes.GinIndex(django.contrib.postgres.indexes.OpClass(django.db.models.functions.text.Upper('external_user_id'), 'gin_trgm_ops'), name='ai_tsol_extid_trgm_idx'),
                ),
                migrations.AddIndex(
                    model_name='tasksolution',
                    index=django.contrib.postgres.indexes.GinIndex(django.contrib.postgres.indexes.OpClass(django.db.models.functions.text.Upper('model_key'), 'gin_trgm_ops'), name='ai_tsol_model_trgm_idx'),
                ),
                migrations.AddIndex(
                    model_name='tasksolution',
                    index=django.contrib.postgres.indexes.GinIndex(django.contrib.postgres.indexes.OpClass(django.db.models.functions.text.Upper('topic_name'), 'gin_trgm_ops'), name='ai_tsol_topic_trgm_idx'),
                ),
                migrations.AddIndex(
                    model_name='tasksolution',
                    index=django.contrib.postgres.indexes.GinIndex(django.contrib.postgres.indexes.OpClass(django.db.models.functions.text.Upper('prompt_name'), 'gin_trgm_ops'), name='ai_tsol_prompt_trgm_idx'),
                ),
                migrations.AddIndex(
                    model_name='updatelog',
                    index=django.contrib.postgres.indexes.GinIndex(django.contrib.postgres.indexes.OpClass(django.db.models.functions.text.Upper('description'), 'gin_trgm_ops'), name='ai_update_desc_trgm_idx'),
                ),
                migrations.AddIndex(
                    model_name='updatelog',
                    index=django.contrib.postgres.indexes.GinIndex(django.contrib.postgres.indexes.OpClass(django.db.models.functions.text.Upper('author'), 'gin_trgm_ops'), name='ai_update_author_trgm_idx'),
                ),
            ],
        ),
    ]
