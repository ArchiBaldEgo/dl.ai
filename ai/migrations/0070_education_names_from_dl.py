"""Названия вуза/школы и группы — прямо из DL (EducationInfo 1.4.2).

REST API (dl-rest-api.json, версия 1.4.2) в блоке ``EducationInfo`` отдаёт
человекочитаемые ``schoolName``/``formName``/``groupName`` рядом с ID. До
этого названий в API не было, и их вели вручную в справочнике
``DLEducationName``. Теперь DL-ответ сохраняется в аккаунте — рендер берёт
название оттуда, а ручной справочник остаётся лишь запасным источником.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("ai", "0069_batch_delta_and_trgm_indexes"),
    ]

    operations = [
        migrations.AddField(
            model_name="externaldlaccount",
            name="education_school_name",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Название учебного заведения (education.schoolName)",
                max_length=255,
                verbose_name="Школа/вуз: название",
            ),
        ),
        migrations.AddField(
            model_name="externaldlaccount",
            name="education_form_name",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Название класса/курса (education.formName)",
                max_length=255,
                verbose_name="Класс/курс: название",
            ),
        ),
        migrations.AddField(
            model_name="externaldlaccount",
            name="education_group_name",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Название группы (education.groupName)",
                max_length=255,
                verbose_name="Группа: название",
            ),
        ),
    ]
