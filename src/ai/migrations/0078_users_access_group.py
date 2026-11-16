"""Встроенная группа «Пользователи» — baseline без доступа в админку.

Решение владельца 2026-11: обычный пользователь не должен попадать в админку
вовсе. Группа создаётся пустой (ни одного права) и становится baseline'ом
провижининга. Одновременно выполняется разовый перевод аудитории:

* не переводятся суперпользователи, staff и члены встроенной «Разработчик»;
* все остальные (включая членов «Разработчик промптов», которые раньше
  зачислялись автоматически) получают РОВНО эту группу — прежние
  членства снимаются. Нужные роли суперюзер выдаёт заново на «Группах
  доступа».

Миграция намеренно необратима: восстановить прежний состав групп нельзя
(Reverse — noop). Перед деплоем — бэкап БД.
"""

from django.db import migrations

# Имена зафиксированы строками, а не берутся из ai.constants: миграция должна
# значить одно и то же при любом будущем переименовании констант.
USERS_GROUP_NAME = "Пользователи"
DEVELOPER_GROUP_NAME = "Разработчик"


def create_users_group(apps, schema_editor):
    AccessGroup = apps.get_model("ai", "AIAccessGroup")
    User = apps.get_model("auth", "User")

    users_group, _ = AccessGroup.objects.get_or_create(
        name=USERS_GROUP_NAME,
        defaults={
            "is_builtin": True,
            "description": "Обычные пользователи без доступа в админку",
        },
    )

    Through = AccessGroup.users.through
    # Имена FK через-таблицы не хардкодим: у авто-through они строятся из
    # имён моделей (aiaccessgroup_id/user_id), а исторические модели миграций
    # лучше спрашивать у их meta.
    group_attname = next(
        field.attname for field in Through._meta.fields
        if field.is_relation
        and field.related_model._meta.label_lower == "ai.aiaccessgroup"
    )
    user_attname = next(
        field.attname for field in Through._meta.fields
        if field.is_relation
        and field.related_model._meta.label_lower == "auth.user"
    )

    exempt_ids = set(
        User.objects.filter(is_superuser=True).values_list("pk", flat=True)
    )
    exempt_ids |= set(
        User.objects.filter(is_staff=True).values_list("pk", flat=True)
    )
    exempt_ids |= set(
        Through.objects.filter(**{f"{group_attname}__name": DEVELOPER_GROUP_NAME})
        .values_list(user_attname, flat=True)
    )

    # «Перевести всех»: снять прежние членства у не-исключённых и зачислить
    # их в «Пользователи» (bulk_create — без поштучных запросов на каждого).
    Through.objects.exclude(**{f"{user_attname}__in": exempt_ids}).delete()
    Through.objects.bulk_create(
        [
            Through(**{group_attname: users_group.pk, user_attname: user_id})
            for user_id in User.objects.exclude(pk__in=exempt_ids)
            .values_list("pk", flat=True)
        ],
        ignore_conflicts=True,
    )


class Migration(migrations.Migration):

    dependencies = [
        ("ai", "0077_aimodeltestresult_prompt_debug_and_more"),
    ]

    operations = [
        migrations.RunPython(create_users_group, migrations.RunPython.noop),
    ]
