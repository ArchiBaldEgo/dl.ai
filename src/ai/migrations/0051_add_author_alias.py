from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ai', '0050_aicoursetreecache'),
    ]

    operations = [
        migrations.CreateModel(
            name='AuthorAlias',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('nick', models.CharField(max_length=255, unique=True, verbose_name='Ник в git')),
                ('full_name', models.CharField(max_length=255, verbose_name='ФИО')),
                ('group', models.CharField(blank=True, default='', max_length=255, verbose_name='Группа')),
            ],
            options={
                'verbose_name': 'Автор коммитов (справочник)',
                'verbose_name_plural': 'Авторы коммитов (справочник)',
                'db_table': 'ai_author_alias',
                'ordering': ('full_name',),
            },
        ),
    ]