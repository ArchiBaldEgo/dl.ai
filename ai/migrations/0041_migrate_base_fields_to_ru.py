# Data migration: перенос legacy-базовых полей в *_ru перед их удалением.
#
# Базовые поля prompt_name/prompt_text (Prompt, SharedPrompt) и topic_name
# (Topic) исторически содержали русский текст и служили fallback-кандидатом
# цепочки локализации. Кандидат базового поля удалён из цепочки
# (ai/i18n.get_localized_name/get_localized_text) — чтобы данные не терялись,
# значение копируется в *_ru там, где _ru пусто, после чего поля удаляются
# (см. 0042_remove_…).

from django.db import migrations


def copy_prompt_fields(apps, schema_editor):
    Prompt = apps.get_model("ai", "Prompt")
    for obj in Prompt.objects.all().iterator():
        updates = {}
        if not (obj.prompt_name_ru or "").strip() and (obj.prompt_name or "").strip():
            updates["prompt_name_ru"] = obj.prompt_name
        if not (obj.prompt_text_ru or "").strip() and (obj.prompt_text or "").strip():
            updates["prompt_text_ru"] = obj.prompt_text
        if updates:
            Prompt.objects.filter(pk=obj.pk).update(**updates)


def copy_sharedprompt_fields(apps, schema_editor):
    SharedPrompt = apps.get_model("ai", "SharedPrompt")
    for obj in SharedPrompt.objects.all().iterator():
        updates = {}
        if not (obj.prompt_name_ru or "").strip() and (obj.prompt_name or "").strip():
            updates["prompt_name_ru"] = obj.prompt_name
        if not (obj.prompt_text_ru or "").strip() and (obj.prompt_text or "").strip():
            updates["prompt_text_ru"] = obj.prompt_text
        if updates:
            SharedPrompt.objects.filter(pk=obj.pk).update(**updates)


def copy_topic_fields(apps, schema_editor):
    Topic = apps.get_model("ai", "Topic")
    for obj in Topic.objects.all().iterator():
        if not (obj.topic_name_ru or "").strip() and (obj.topic_name or "").strip():
            Topic.objects.filter(pk=obj.pk).update(topic_name_ru=obj.topic_name)


def noop(apps, schema_editor):
    # Обратный перенос не выполняем: старые поля остаются пустыми после
    # откката схемы, данные осознанно «переезжают» в *_ru насовсем.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('ai', '0040_alter_airequestlog_mode_alter_airequestlog_source_and_more'),
    ]

    operations = [
        migrations.RunPython(copy_prompt_fields, noop),
        migrations.RunPython(copy_sharedprompt_fields, noop),
        migrations.RunPython(copy_topic_fields, noop),
    ]