from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("ai", "0071_aimodeltestresult_updated_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="task",
            name="tree_path",
            field=models.CharField(blank=True, default="", max_length=512, verbose_name="Путь в дереве задач DL"),
        ),
    ]
