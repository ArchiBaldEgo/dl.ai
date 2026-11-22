"""Удаляет AIAppSettings.favorites_epoch — «часто используемые модели» убраны."""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("ai", "0087_aiusermodel_public_requested_and_more"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="aiappsettings",
            name="favorites_epoch",
        ),
    ]
