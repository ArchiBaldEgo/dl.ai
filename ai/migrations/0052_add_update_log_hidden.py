from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ai', '0051_add_author_alias'),
    ]

    operations = [
        migrations.AddField(
            model_name='updatelog',
            name='hidden',
            field=models.BooleanField(default=False, verbose_name='Скрыто'),
        ),
    ]