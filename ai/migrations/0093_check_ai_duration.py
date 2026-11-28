from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("ai", "0092_check_ai_protocol_single")]

    operations = [
        migrations.AddField(
            model_name="aicheckbatchfile",
            name="duration_seconds",
            field=models.FloatField(blank=True, null=True),
        ),
    ]
