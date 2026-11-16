"""Статус лога запроса по умолчанию — ошибка.

Раньше ``AIRequestLog.status`` по умолчанию был ``success``: запись лога
создавалась в момент отправки запроса (LogWriter.create) и закрывалась
update_success/update_error после ответа модели. Если ответа не приходило
(модель зависла, соединение оборвалось, consumer упал до update_*), запись
оставалась со статусом «success» и пустым ответом — то есть «ответа от модели
нет, но успех». Теперь default = ``error``: незавершённый/осиротевший запрос
по умолчанию считается ошибкой, а update_success переводит в «success» только
при непустом ответе.

Дополнительно data-миграция переклассифицирует уже накопленные записи:
``status='success'`` без ответа → ``status='error'``.
"""

from django.db import migrations, models


def reclassify_empty_response_success_to_error(apps, schema_editor):
    AIRequestLog = apps.get_model("ai", "AIRequestLog")
    AIRequestLog.objects.filter(
        status="success",
    ).filter(
        models.Q(response_text__isnull=True) | models.Q(response_text=""),
    ).update(
        status="error",
        error_message="Модель не вернула ответ",
    )


def noop_reverse(apps, schema_editor):
    # Обратное преобразование небезопасно (успешные записи без ответа
    # всё равно некорректны) — намеренно ничего не делаем.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("ai", "0028_alter_aiappsettings_options_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="airequestlog",
            name="status",
            field=models.CharField(
                choices=[("success", "Success"), ("error", "Error")],
                default="error",
                max_length=16,
                verbose_name="Статус",
            ),
        ),
        migrations.RunPython(
            reclassify_empty_response_success_to_error,
            reverse_code=noop_reverse,
        ),
    ]