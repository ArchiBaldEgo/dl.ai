"""Группы доступа: AIAccessGroup + AIAdminGroupPermission, сиды, перенос legacy.

- Права — строковые ключи (page.* — вкладки/страницы, docs.* — главы
  документации, action.* — действия). Состав и подписи правит суперюзер на
  странице «Группы доступа».
- Встроенные группы: «Разработчик промптов» ( ровно прежние права
  Django-группы prompt_developer) и «Разработчик» (всё, кроме «Промптов
  по умолчанию», доки суперюзера, всех записей журнала, создания/закрытия
  тасков; порядок тасков можно менять — action.task_reorder выдана).
- Legacy-перенос: все члены Django-группы prompt_developer зачисляются во
  встроенную «Разработчик промптов»; сама Django-группа остаётся в БД
  как артефакт (новые проверки её не читают).
"""

from django.db import migrations, models

# Ключи доступа: (key, label, section). Единственный источник — сид ниже;
# страница «Группы доступа» читает строки из БД, не копии этих констант.
_ACCESS_KEYS = [
    ("page.admin", "Вход в админку ИИ", "page"),
    ("page.arm_solve", "Пакетное решение (ARM)", "page"),
    ("page.model_status", "Состояние моделей", "page"),
    ("page.prompt_admin", "Промпты (правки — свои)", "page"),
    ("page.prompt_defaults", "Промпты по умолчанию", "page"),
    ("page.tasks", "Доска «Таски» (вход + брать)", "page"),
    ("page.logs_own", "Журнал запросов (только свои)", "page"),
    ("page.logs_all", "Журнал запросов (все записи)", "page"),
    ("page.prompt_regression", "Регрессионные тесты промптов", "page"),
    ("page.test_console", "Тестовая консоль", "page"),
    ("page.updates", "Обновления (история изменений)", "page"),
    ("docs.developer", "Документация разработчика", "docs"),
    ("docs.superuser", "Документация суперюзера", "docs"),
    ("action.task_create", "Таски: создавать", "action"),
    ("action.task_close", "Таски: закрывать", "action"),
    ("action.task_reorder", "Таски: менять порядок", "action"),
]

# Встроенные группы: имя → (описание, ключи).
_BUILTIN_GROUPS = {
    "Разработчик промптов": (
        "Прежний уровень «prompt_developer»: админка, ARM, промпты (свои), "
        "промпты по умолчанию, журнал своих запросов.",
        [
            "page.admin", "page.arm_solve", "page.prompt_admin",
            "page.prompt_defaults", "page.logs_own",
        ],
    ),
    "Разработчик": (
        "Доступ ко всему, кроме: студенты, чужие промпты/темы/языки/препромты "
        "(правки — только свои), «Промпты по умолчанию», доки суперюзера, все "
        "записи журнала, создание/закрытие тасков. Таски видит и берёт, "
        "порядок может менять.",
        [
            "page.admin", "page.arm_solve", "page.model_status",
            "page.prompt_admin", "page.prompt_regression", "page.test_console",
            "page.tasks", "page.logs_own", "page.updates",
            "docs.developer", "action.task_reorder",
        ],
    ),
}


def _seed(apps, schema_editor):
    Permission = apps.get_model("ai", "AIAdminGroupPermission")
    AccessGroup = apps.get_model("ai", "AIAccessGroup")

    # 1. Ключи прав (get_or_create — идемпотентно при повторном прогоне).
    keys = {}
    for key, label, section in _ACCESS_KEYS:
        permission, _ = Permission.objects.get_or_create(
            key=key, defaults={"label": label, "section": section}
        )
        keys[key] = permission

    # 2. Встроенные группы.
    groups = {}
    for name, (description, permission_keys) in _BUILTIN_GROUPS.items():
        group, _ = AccessGroup.objects.get_or_create(
            name=name,
            defaults={
                "is_builtin": True,
                "description": description,
            },
        )
        group.is_builtin = True
        if not group.description:
            group.description = description
        group.save(update_fields=["is_builtin", "description"])
        group.permissions.add(*[keys[k] for k in permission_keys])
        groups[name] = group

    # 3. Legacy-перенос: члены Django-группы prompt_developer → встроенная
    #    «Разработчик промптов». Без неё после миграционных сидов никто из
    #    действующей аудитории не попал бы в новую модель. Запрос целиком по
    #    историческим моделям (auth.User → groups M2M): подмешивать реальные
    #    инстансы Django в M2M исторической модели нельзя.
    User = apps.get_model("auth", "User")
    legacy_members = User.objects.filter(groups__name="prompt_developer")
    prompt_dev_group = groups["Разработчик промптов"]
    prompt_dev_group.users.add(*legacy_members)


def _unseed(apps, schema_editor):
    """Обратный ход: удаляем только созданные группами строки (users/M2M)."""
    AccessGroup = apps.get_model("ai", "AIAccessGroup")
    AccessGroup.objects.filter(name__in=list(_BUILTIN_GROUPS)).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("ai", "0059_ai_dev_task"),
    ]

    operations = [
        migrations.CreateModel(
            name="AIAdminGroupPermission",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("key", models.SlugField(max_length=50, unique=True, verbose_name="Ключ")),
                ("label", models.CharField(max_length=200, verbose_name="Название")),
                ("section", models.CharField(
                    choices=[
                        ("page", "Страницы"),
                        ("docs", "Документация"),
                        ("action", "Действия"),
                    ],
                    default="page", max_length=20, verbose_name="Раздел",
                )),
            ],
            options={
                "verbose_name": "Право доступа (группы)",
                "verbose_name_plural": "Права доступа (группы)",
                "ordering": ("section", "key"),
            },
        ),
        migrations.CreateModel(
            name="AIAccessGroup",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=100, unique=True, verbose_name="Название")),
                ("description", models.TextField(blank=True, default="", verbose_name="Описание")),
                ("is_builtin", models.BooleanField(default=False, verbose_name="Встроенная")),
                ("permissions", models.ManyToManyField(
                    blank=True, related_name="access_groups",
                    to="ai.aiadmingrouppermission", verbose_name="Права",
                )),
                ("users", models.ManyToManyField(
                    blank=True, related_name="ai_access_groups",
                    to="auth.user", verbose_name="Участники",
                )),
            ],
            options={
                "verbose_name": "Группа доступа",
                "verbose_name_plural": "Группы доступа",
                "ordering": ("name",),
            },
        ),
        migrations.RunPython(_seed, _unseed),
    ]