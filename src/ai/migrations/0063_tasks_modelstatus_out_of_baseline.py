"""«Таски» и «Состояние моделей» выведены из BASELINE_ACCESS_KEYS.

Решение владельца 2026-10-20: эти две страницы выдаются группами, а не
базой. Чтобы существующие члены встроенной группы «Разработчик промптов»
не потеряли доступ (они получали его только baseline'ом — в сиде 0060
этих ключей у группы не было), оба ключа выдаются ей здесь. У «Разработчика»
они жили в сиде 0060 и так.
"""

from django.db import migrations


def grant_to_prompt_dev_group(apps, schema_editor):
    Permission = apps.get_model("ai", "AIAdminGroupPermission")
    AccessGroup = apps.get_model("ai", "AIAccessGroup")

    keys = ("page.tasks", "page.model_status")
    perms = {}
    for key, label in (
        ("page.tasks", "Доска «Таски» (вход + брать)"),
        ("page.model_status", "Состояние моделей"),
    ):
        perm, _ = Permission.objects.get_or_create(
            key=key, defaults={"label": label, "section": "page"}
        )
        perms[key] = perm

    group = AccessGroup.objects.filter(name="Разработчик промптов").first()
    if group is None:
        return
    group.permissions.add(*perms.values())


def ungrant(apps, schema_editor):
    Permission = apps.get_model("ai", "AIAdminGroupPermission")
    AccessGroup = apps.get_model("ai", "AIAccessGroup")
    group = AccessGroup.objects.filter(name="Разработчик промптов").first()
    if group is None:
        return
    for key in ("page.tasks", "page.model_status"):
        perm = Permission.objects.filter(key=key).first()
        if perm:
            group.permissions.remove(perm)


class Migration(migrations.Migration):
    dependencies = [
        ("ai", "0062_delete_prompt_tests"),
    ]

    operations = [
        migrations.RunPython(grant_to_prompt_dev_group, ungrant),
    ]