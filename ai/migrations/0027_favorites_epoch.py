"""Добавляет AIAppSettings.favorites_epoch (дата-отсечка счётчика фаворитов)."""
from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('ai', '0026_populate_update_log'),
    ]

    operations = [
        migrations.AddField(
            model_name='aiappsettings',
            name='favorites_epoch',
            field=models.DateTimeField(
                blank=True,
                null=True,
                default=django.utils.timezone.now,
            ),
        ),
    ]