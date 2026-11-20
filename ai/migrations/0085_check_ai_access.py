"""Создаёт группу доступа Check AI."""

from django.db import migrations


def grant_check_ai_access(apps, schema_editor):
    Permission = apps.get_model("ai", "AIAdminGroupPermission")
    AccessGroup = apps.get_model("ai", "AIAccessGroup")
    db = schema_editor.connection.alias
    permissions = Permission.objects.using(db)
    check, _ = permissions.get_or_create(
        key="page.check_ai", defaults={"label": "Check AI", "section": "page"},
    )
    group, _ = AccessGroup.objects.using(db).get_or_create(name="Check AI")
    group.permissions.add(permissions.get(key="page.admin"), check)


class Migration(migrations.Migration):
    dependencies = [
        ("ai", "0084__usertoemaildictionary_alter_authoralias_email"),
    ]
    # Сохраняем состав группы при откате кода.
    operations = [migrations.RunPython(grant_check_ai_access, migrations.RunPython.noop)]
