# Generated for batch-solve ARM + removal of AIModelTokenBudget.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ai', '0021_aimodeltestrun_error_message_and_more'),
    ]

    operations = [
        # --- Part 1: remove AI model token budgets ---------------------------
        migrations.DeleteModel(
            name='AIModelTokenBudget',
        ),

        # --- Part 3: Task table ---------------------------------------------
        migrations.CreateModel(
            name='Task',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('node_id', models.PositiveIntegerField(db_index=True, help_text='Идентификатор узла задачи на dl.gsu.by (nodeId).', unique=True, verbose_name='DL node id')),
                ('task_id', models.PositiveIntegerField(blank=True, db_index=True, help_text='Заполняется из get-task-info (поле taskId).', null=True, verbose_name='DL task id')),
                ('name', models.CharField(blank=True, default='', max_length=512, verbose_name='Название')),
                ('statement', models.TextField(blank=True, default='', verbose_name='Условие')),
                ('file_extension', models.CharField(blank=True, default='', help_text='Например .pas, .cpp, .py — используется для get-solution.', max_length=16, verbose_name='Расширение файла')),
                ('active', models.BooleanField(db_index=True, default=True, verbose_name='Активна')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('programming_language', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='tasks', to='ai.programminglanguage', verbose_name='Язык программирования')),
                ('topic', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='tasks', to='ai.topic', verbose_name='Тема')),
            ],
            options={
                'verbose_name': 'Задача (DL)',
                'verbose_name_plural': 'Задачи (DL)',
                'db_table': 'ai_task',
                'ordering': ('-created_at',),
            },
        ),

        # --- Part 3: AIModelTestRun.run_type --------------------------------
        migrations.AddField(
            model_name='aimodeltestrun',
            name='run_type',
            field=models.CharField(
                choices=[('single', 'Single (find-error)'), ('batch', 'Batch (solve)')],
                db_index=True, default='single', max_length=16,
            ),
        ),

        # --- Part 3: AIModelTestResult extensions ---------------------------
        migrations.AddField(
            model_name='aimodeltestresult',
            name='task',
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name='test_results', to='ai.task',
            ),
        ),
        migrations.AddField(
            model_name='aimodeltestresult',
            name='verdict',
            field=models.CharField(
                blank=True, choices=[('solved', 'Решено'), ('failed', 'Не решено'), ('skipped', 'Пропущено')],
                db_index=True, max_length=8, null=True,
            ),
        ),
        migrations.AddField(
            model_name='aimodeltestresult',
            name='topic_id_snapshot',
            field=models.IntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='aimodeltestresult',
            name='topic_name_snapshot',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
        migrations.AddField(
            model_name='aimodeltestresult',
            name='prog_lang_snapshot',
            field=models.CharField(blank=True, default='', max_length=255),
        ),

        # Replace the legacy (run, model_key) unique with a task-aware pair.
        migrations.RemoveConstraint(
            model_name='aimodeltestresult',
            name='ai_model_test_result_run_model_uniq',
        ),
        migrations.AddConstraint(
            model_name='aimodeltestresult',
            constraint=models.UniqueConstraint(
                fields=('run', 'model_key', 'task'),
                name='ai_model_test_result_run_model_task_uniq',
            ),
        ),
        migrations.AddConstraint(
            model_name='aimodeltestresult',
            constraint=models.UniqueConstraint(
                condition=models.Q(task__isnull=True),
                fields=('run', 'model_key'),
                name='ai_model_test_result_run_model_uniq',
            ),
        ),
    ]