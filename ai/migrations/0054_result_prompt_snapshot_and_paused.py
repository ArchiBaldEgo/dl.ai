from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ai', '0053_aiwizardseen'),
    ]

    operations = [
        # Снимок фактически использованного препромпта пары (старт «по
        # привязке»); у легаси-строк NULL/пусто — отображается «—».
        migrations.AddField(
            model_name='aimodeltestresult',
            name='prompt_id',
            field=models.IntegerField(blank=True, null=True, verbose_name='ID препромпта (снимок)'),
        ),
        migrations.AddField(
            model_name='aimodeltestresult',
            name='prompt_name',
            field=models.CharField(blank=True, default='', max_length=255, verbose_name='Препромпт (снимок)'),
        ),
        # Кооперативная пауза batch-прогона (choices-only, без DDL-эффекта).
        migrations.AlterField(
            model_name='aimodeltestrun',
            name='status',
            field=models.CharField(
                choices=[
                    ('running', 'Running'),
                    ('completed', 'Completed'),
                    ('failed', 'Failed'),
                    ('cancelled', 'Cancelled'),
                    ('paused', 'Paused'),
                ],
                default='running',
                max_length=16,
                verbose_name='Статус',
            ),
        ),
    ]