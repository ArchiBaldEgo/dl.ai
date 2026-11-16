"""Новые страницы админки: «Контроль токенов», «Мои AI-модели», «Запросы по DL API».

Ключи ``page.*`` синхронизируются с нава сами (ai/admin/access_groups.py::
_dynamic_page_doc_permissions), поэтому для их существования миграция не
нужна. Но встроенным группам («Разработчик», «Разработчик промптов») эти
страницы надо ВЫДАТЬ осознанно — иначе после переезда с baseline они были бы
не у кого.

Решение владельца (ТЗ 2026-10-20):
* «Мои AI-модели» — страница про СВОИ модели: доступна всей базовой
  аудитории админки (как «Мой промпт»), поэтому её ключ выдаём обеим
  встроенным группам;
* «Контроль токенов» — расход ВСЕХ пользователей: доступ выдаётся группе
  «Разработчик» (уровень выше базового), «Разработчику промптов» — нет;
* «Запросы по DL API» — заявки пользователей: доступ выдаётся группе
  «Разработчик» (кто ведёт API), «Разработчику промптов» — нет.
"""

from django.db import migrations

# Ключ → (подпись, список встроенных групп, которым выдаём).
_NEW_KEYS = (
    ("page.user_models", "Мои AI-модели", ("Разработчик", "Разработчик промптов")),
    ("page.tokens", "Контроль токенов", ("Разработчик",)),
    ("page.dl_api_requests", "Запросы по DL API", ("Разработчик",)),
)


def grant(apps, schema_editor):
    Permission = apps.get_model("ai", "AIAdminGroupPermission")
    AccessGroup = apps.get_model("ai", "AIAccessGroup")

    for key, label, group_names in _NEW_KEYS:
        perm, _ = Permission.objects.get_or_create(
            key=key, defaults={"label": label, "section": "page"}
        )
        for name in group_names:
            group = AccessGroup.objects.filter(name=name).first()
            if group is not None:
                group.permissions.add(perm)


def ungrant(apps, schema_editor):
    Permission = apps.get_model("ai", "AIAdminGroupPermission")
    AccessGroup = apps.get_model("ai", "AIAccessGroup")
    for key, _label, group_names in _NEW_KEYS:
        perm = Permission.objects.filter(key=key).first()
        if perm is None:
            continue
        for name in group_names:
            group = AccessGroup.objects.filter(name=name).first()
            if group is not None:
                group.permissions.remove(perm)


class Migration(migrations.Migration):
    dependencies = [
        ("ai", "0066_aimodeltestresult_failure_reason_and_more"),
    ]

    operations = [
        migrations.RunPython(grant, ungrant),
    ]
