# Generated for prompt regression tests (PromptTestCase/Run/Result).

import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ai', '0022_task_batch_solve'),
    ]

    operations = [
        migrations.CreateModel(
            name='PromptTestCase',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=255, verbose_name='Название')),
                ('mode', models.CharField(
                    choices=[('solve', 'Solve'), ('find_error', 'Find error'), ('chat', 'Chat')],
                    db_index=True, max_length=16, verbose_name='Режим',
                )),
                ('input_text', models.TextField(verbose_name='Ввод (условие / код / сообщение)')),
                ('expected_text', models.TextField(blank=True, default='', verbose_name='Эталон')),
                ('comparator', models.CharField(
                    choices=[
                        ('ratio', 'ratio (difflib)'),
                        ('contains_all', 'contains_all (все строки эталона)'),
                        ('exact', 'exact (нормализованное равенство)'),
                        ('set', 'set (равенство множеств строк)'),
                    ],
                    default='ratio', max_length=16, verbose_name='Компаратор',
                )),
                ('match_threshold', models.FloatField(
                    blank=True, help_text='Для компаратора ratio (по умолчанию 0.85).',
                    null=True, verbose_name='Порог ratio',
                )),
                ('ui_language', models.CharField(default='Русский', max_length=16, verbose_name='Язык интерфейса')),
                ('active', models.BooleanField(db_index=True, default=True, verbose_name='Активен')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('owner', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name='owned_prompt_test_cases', to='auth.user', verbose_name='Владелец',
                )),
                ('programming_language', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name='prompt_test_cases', to='ai.programminglanguage',
                    verbose_name='Язык программирования',
                )),
                ('topic', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name='prompt_test_cases', to='ai.topic', verbose_name='Тема',
                )),
            ],
            options={
                'verbose_name': 'Тест-кейс промпта',
                'verbose_name_plural': 'Тест-кейсы промптов',
                'db_table': 'ai_prompttestcase',
                'ordering': ('-created_at',),
            },
        ),
        migrations.CreateModel(
            name='PromptTestRun',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('run_id', models.CharField(db_index=True, max_length=64, unique=True)),
                ('status', models.CharField(
                    choices=[('running', 'Running'), ('completed', 'Completed'), ('failed', 'Failed')],
                    db_index=True, default='running', max_length=16,
                )),
                ('model_key', models.CharField(db_index=True, max_length=128)),
                ('model_title', models.CharField(blank=True, default='', max_length=255)),
                ('prompt_id', models.IntegerField(blank=True, db_index=True, null=True)),
                ('prompt_name', models.CharField(blank=True, default='', max_length=255)),
                ('ui_language', models.CharField(default='Русский', max_length=16)),
                ('started_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('finished_at', models.DateTimeField(blank=True, null=True)),
                ('error_message', models.TextField(blank=True, default='')),
                ('report', models.JSONField(blank=True, default=dict)),
                ('total_cases', models.PositiveSmallIntegerField(default=0)),
                ('user', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name='prompt_test_runs', to='auth.user',
                )),
            ],
            options={
                'verbose_name': 'Прогон регрессионных тестов промпта',
                'verbose_name_plural': 'Прогоны регрессионных тестов промптов',
                'db_table': 'ai_prompttest_run',
                'ordering': ('-started_at',),
            },
        ),
        migrations.CreateModel(
            name='PromptTestResult',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('model_key', models.CharField(db_index=True, max_length=128)),
                ('model_title', models.CharField(blank=True, default='', max_length=255)),
                ('status', models.CharField(
                    choices=[('ok', 'OK'), ('error', 'Error')], default='ok', max_length=16,
                )),
                ('verdict', models.CharField(
                    choices=[('match', 'Совпадает'), ('mismatch', 'Отклонение'), ('skipped', 'Пропущен')],
                    db_index=True, default='mismatch', max_length=16,
                )),
                ('actual_response', models.TextField(blank=True, default='')),
                ('expected_snapshot', models.TextField(blank=True, default='')),
                ('diff_hint', models.CharField(blank=True, default='', max_length=255)),
                ('duration_seconds', models.FloatField(blank=True, null=True)),
                ('tokens', models.PositiveIntegerField(blank=True, null=True)),
                ('case_name_snapshot', models.CharField(blank=True, default='', max_length=255)),
                ('mode_snapshot', models.CharField(blank=True, default='', max_length=16)),
                ('topic_name_snapshot', models.CharField(blank=True, default='', max_length=255)),
                ('prog_lang_snapshot', models.CharField(blank=True, default='', max_length=255)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('run', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE, related_name='results',
                    to='ai.prompttestrun',
                )),
                ('test_case', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name='test_results', to='ai.prompttestcase',
                )),
            ],
            options={
                'verbose_name': 'Результат регрессионного теста промпта',
                'verbose_name_plural': 'Результаты регрессионных тестов промптов',
                'db_table': 'ai_prompt_test_result',
                'ordering': ('case_name_snapshot',),
            },
        ),
        migrations.AddConstraint(
            model_name='prompttestresult',
            constraint=models.UniqueConstraint(
                fields=('run', 'test_case'), name='ai_prompt_test_result_run_case_uniq',
            ),
        ),
    ]