"""Ключ доступа «Лаборатория промптов» (``page.prompt_lab``).

Лаборатория прогоняет препромпты на базе ошибочных программ и тратит токены,
поэтому её ключ выдаём обеим встроенным группам инженеров: «Разработчик
промптов» (основной пользователь) и «Разработчик». Ключи ``page.*``
синхронизируются с навигацией автоматически, миграция нужна только чтобы
выдать страницу существующим группам.
"""

from django.db import migrations

_KEY = "page.prompt_lab"
_LABEL = "Лаборатория промптов"
_GROUPS = ("Разработчик", "Разработчик промптов")


def grant(apps, schema_editor):
    Permission = apps.get_model("ai", "AIAdminGroupPermission")
    AccessGroup = apps.get_model("ai", "AIAccessGroup")
    perm, _ = Permission.objects.get_or_create(
        key=_KEY, defaults={"label": _LABEL, "section": "page"}
    )
    for name in _GROUPS:
        group = AccessGroup.objects.filter(name=name).first()
        if group is not None:
            group.permissions.add(perm)


def ungrant(apps, schema_editor):
    Permission = apps.get_model("ai", "AIAdminGroupPermission")
    AccessGroup = apps.get_model("ai", "AIAccessGroup")
    perm = Permission.objects.filter(key=_KEY).first()
    if perm is None:
        return
    for name in _GROUPS:
        group = AccessGroup.objects.filter(name=name).first()
        if group is not None:
            group.permissions.remove(perm)


class Migration(migrations.Migration):
    dependencies = [
        ("ai", "0080_find_error_cases_and_prompt_lab"),
    ]

    operations = [
        migrations.RunPython(grant, ungrant),
    ]
