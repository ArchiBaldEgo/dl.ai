"""Emails и «Проверка на ИИ» — ключи в «Группах доступа», без служебных групп.

Страницы уже имеют собственные проверки (суперюзер или ключ), но ключ
page.emails не был засижен, а миграция 0085 создала пустую группу «Check AI»,
которую пользователь не заказывал. Здесь:
- page.emails и page.check_ai становятся обычными чекбоксами «Страниц»
  (права выдаются вручную через «Группы доступа»);
- служебная группа «Check AI» удаляется, страницы продолжают работать.
"""

from django.db import migrations

CHECK_AI_GROUP = "Check AI"


def forwards(apps, schema_editor):
    Permission = apps.get_model("ai", "AIAdminGroupPermission")
    AccessGroup = apps.get_model("ai", "AIAccessGroup")
    db = schema_editor.connection.alias
    permissions = Permission.objects.using(db)

    permissions.update_or_create(
        key="page.emails", defaults={"label": "Emails", "section": "page"},
    )
    permissions.update_or_create(
        key="page.check_ai",
        defaults={"label": "Проверка на ИИ", "section": "page"},
    )

    # Артефакт 0085: группа с 0 участников, права нужны не ей, а выбранным
    # пользователем группам. Удаляем только группу, строку права оставляем.
    AccessGroup.objects.using(db).filter(name=CHECK_AI_GROUP).delete()


def backwards(apps, schema_editor):
    Permission = apps.get_model("ai", "AIAdminGroupPermission")
    AccessGroup = apps.get_model("ai", "AIAccessGroup")
    db = schema_editor.connection.alias
    permissions = Permission.objects.using(db)

    check, _ = permissions.get_or_create(
        key="page.check_ai", defaults={"label": "Check AI", "section": "page"},
    )
    group, _ = AccessGroup.objects.using(db).get_or_create(name=CHECK_AI_GROUP)
    group.permissions.add(permissions.get(key="page.admin"), check)


class Migration(migrations.Migration):
    dependencies = [
        ("ai", "0085_check_ai_access"),
    ]
    operations = [migrations.RunPython(forwards, backwards)]
