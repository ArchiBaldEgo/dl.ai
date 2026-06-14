from django.db import migrations


def migrate_pascal_prompts_to_shared(apps, schema_editor):
    """
    Превращаем Pascal-препромпты (topic_id 13-19) в общие shared-препромпты.
    Создаём SharedPrompt для каждого уникального имени препромпта,
    затем создаём Prompt для КАЖДОГО языка программирования, ссылаясь на общий.
    """
    Prompt = apps.get_model("ai", "Prompt")
    SharedPrompt = apps.get_model("ai", "SharedPrompt")
    ProgrammingLanguage = apps.get_model("ai", "ProgrammingLanguage")
    Topic = apps.get_model("ai", "Topic")

    # Pascal topic IDs которые нужно превратить в общие
    pascal_topic_ids = [13, 14, 15, 16, 17, 18, 19]

    # Получаем все Pascal-препромпты
    pascal_prompts = Prompt.objects.filter(topic_id__in=pascal_topic_ids)

    if not pascal_prompts.exists():
        print("No Pascal prompts found, skipping migration.")
        return

    # Получаем Pascal язык
    pascal_lang = ProgrammingLanguage.objects.filter(language_name__icontains="Pascal").first()
    if not pascal_lang:
        print("Pascal language not found, skipping migration.")
        return

    # Получаем все языки программирования
    all_languages = list(ProgrammingLanguage.objects.all())

    # Группируем препромпты по имени
    prompts_by_name = {}
    for prompt in pascal_prompts:
        name = prompt.prompt_name or f"Prompt #{prompt.id}"
        if name not in prompts_by_name:
            prompts_by_name[name] = []
        prompts_by_name[name].append(prompt)

    for prompt_name, prompts_list in prompts_by_name.items():
        # Берём первый препромпт как источник текста
        source_prompt = prompts_list[0]
        # Заменяем "Turbo Pascal" и "Pascal" на placeholder {language}
        shared_text = source_prompt.prompt_text
        shared_text = shared_text.replace("Turbo Pascal", "{language}")
        shared_text = shared_text.replace("Pascal", "{language}")
        shared_text = shared_text.replace("Turbo {language}", "{language}")

        # Создаём SharedPrompt
        shared = SharedPrompt.objects.create(
            prompt_name=prompt_name,
            prompt_text=shared_text,
        )
        # Привязываем ко всем языкам
        shared.programming_languages.set(all_languages)

        # Теперь создаём Prompt для каждого языка
        for lang in all_languages:
            # Ищем существующую тему для этого языка с таким же именем
            # Если нет — создаём
            topic_name = source_prompt.topic.topic_name if source_prompt.topic else prompt_name
            topic, _ = Topic.objects.get_or_create(
                topic_name=topic_name,
                programming_language=lang,
                defaults={"topic_name": topic_name}
            )

            # Создаём Prompt для этого языка, ссылаясь на shared
            Prompt.objects.create(
                topic=topic,
                prompt_name=prompt_name,
                prompt_text="",  # текст берётся из shared
                shared_prompt=shared,
            )

        # Удаляем старые Pascal-препромпты
        for old_prompt in prompts_list:
            old_prompt.delete()

    print(f"Migrated {len(prompts_by_name)} Pascal prompts to shared prompts.")


def reverse_migration(apps, schema_editor):
    # Обратная миграция — удаляем SharedPrompt и созданные Prompt,
    # восстанавливать старые данные сложно, поэтому просто чистим
    SharedPrompt = apps.get_model("ai", "SharedPrompt")
    Prompt = apps.get_model("ai", "Prompt")
    SharedPrompt.objects.all().delete()
    # Удаляем Prompt, которые ссылаются на SharedPrompt (они уже удалены каскадом)
    Prompt.objects.filter(shared_prompt__isnull=False).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("ai", "0010_prompt_prompt_text_override_alter_prompt_table_and_more"),
    ]

    operations = [
        migrations.RunPython(migrate_pascal_prompts_to_shared, reverse_migration),
    ]
