from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("ai", "0045_aimodeltestrun_run_name"),
    ]

    operations = [
        migrations.AddField(
            model_name="tasksolution",
            name="course_id",
            field=models.IntegerField(blank=True, null=True, verbose_name="ID курса DL"),
        ),
    ]