"""Хелперы проверки прав доступа для AI admin views и ModelAdmin.

Все can_access_* резолвятся единообразно: superuser видит всё по умолчанию,
остальным доступ определяется ключами их групп (AIAccessGroup, страница
«Группы доступа» /ai/admin/ai-groups/). Участники групп — не staff: правки
чужих промптов/тем/языков/препромтов и студентов им недоступны не из-за
ключей, а потому что соответствующие ModelAdmin'ы за _StaffOnlyAdminMixin,
а свои промпты режет prompt_queryset_for_user. Жёстких Django-Group больше
нет: legacy-группа prompt_developer перенесена во встроенную AIAccessGroup
«Разработчик промптов» миграцией и из проверок удалена.
"""

from ..constants import BUILTIN_GROUP_PROMPT_DEV
from ..models import AIAccessGroup, AIAdminGroupPermission


#: Базовые права ЛЮБОГО, кто допущен в админку (ТЗ 2026-10-19): весь
#: «Раздел ИИ» (ARM + состояние моделей + таски + промпты), «Мой промпт»
#: (= page.prompt_admin), «Журнал запросов» (свои записи; супер/staff — все).
#: Модель ДОБАВЛЯЮЩАЯ: группы поверх baseline выдают действия страниц
#: (создать/закрыть/переставить), «перезаписывать чужое», «читать все
#: записи» журнала, главы документации и т.д. Отнять базовое нельзя.
BASELINE_ACCESS_KEYS = frozenset({
    "page.arm_solve",
    "page.model_status",
    "page.tasks",
    "page.prompt_admin",
    "page.prompt_defaults",
    "page.logs",
})


def is_prompt_developer_user(user):
    """Членство во встроенной группе «Разработчик промптов».

    Табличное право (AIAccessGroup), а не Django-Group: выдаётся
    автоматически при провиженинге нового DL-пользователя и правится
    суперюзером на «Группах доступа» (отзыв теперь «липкий»).
    """
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_active", True) is False:
        return False
    return user.ai_access_groups.filter(name=BUILTIN_GROUP_PROMPT_DEV).exists()


def add_user_to_builtin_ai_group(user, name=BUILTIN_GROUP_PROMPT_DEV):
    """Зачисляет пользователя во встроенную группу доступа по имени.

    Используется провиженингом внешних DL-аккаунтов (baseline-доступ новой
    аудитории) и тестами; группу создаёт, если миграционный сид потерян.
    """
    group, _ = AIAccessGroup.objects.get_or_create(
        name=name, defaults={"is_builtin": True}
    )
    user.ai_access_groups.add(group)
    return group


def is_staff_or_superuser(user):
    """Проверяет, является ли пользователь staff или суперпользователем."""
    if not user:
        return False
    if getattr(user, "is_active", True) is False:
        return False
    return bool(user.is_superuser or user.is_staff)


def is_superuser_user(user):
    """Проверяет, является ли пользователь суперпользователем.

    Отдельный от ``is_staff_or_superuser`` помощник: некоторые инструменты
    (регрессионные тесты, тестовая консоль, тест-кейсы промптов) по решению
    владельца доступны ТОЛЬКО суперпользователю — даже staff без флага
    ``is_superuser`` их не видит (если ключ не выдан через группы).
    """
    if not user:
        return False
    if getattr(user, "is_active", True) is False:
        return False
    return bool(getattr(user, "is_superuser", False))


def user_access_keys(user):
    """Множество ключей доступа пользователя (superuser → все существующие).

    Ключи лежат в AIAccessGroup.permissions (AIAdminGroupPermission, сидирует
    миграция 0060). Superuser'у ключи не нужны — все проверки и так дают ему
    полный доступ через is_superuser_user; здесь все ключи возвращаются для
    удобства (шапка/навычисления). Неактивные/неаутентифицированные — пусто.

    К ключам ГРУПП поверх добавляются базовые права (BASELINE_ACCESS_KEYS):
    у любого, кто вообще допущен в админку (staff/super либо page.admin
    группой) — весь «Раздел ИИ» + журнал-свои.
    """
    if not user or not getattr(user, "is_authenticated", False):
        return set()
    if getattr(user, "is_active", True) is False:
        return set()
    if user.is_superuser:
        return set(AIAdminGroupPermission.objects.values_list("key", flat=True))
    group_keys = set(
        AIAccessGroup.objects.filter(users=user)
        .values_list("permissions__key", flat=True)
    )
    if is_staff_or_superuser(user) or "page.admin" in group_keys:
        group_keys |= BASELINE_ACCESS_KEYS
    return group_keys


def user_has_key(user, key):
    """Точечная проверка ключа: superuser всегда True, иначе — по группам."""
    if is_superuser_user(user):
        return True
    return key in user_access_keys(user)


def can_access_admin(user):
    """Доступ к админке ИИ: staff/superuser ИЛИ ключ page.admin."""
    return is_staff_or_superuser(user) or user_has_key(user, "page.admin")


def can_access_arm(request):
    """Доступ к ARM: staff/superuser ИЛИ ключ page.arm_solve."""
    if not request.user.is_authenticated:
        return False
    return is_staff_or_superuser(request.user) or user_has_key(
        request.user, "page.arm_solve"
    )


def can_access_model_status(request):
    """Доступ к статусу моделей: staff/superuser ИЛИ ключ page.model_status."""
    if not request.user.is_authenticated:
        return False
    return is_staff_or_superuser(request.user) or user_has_key(
        request.user, "page.model_status"
    )


def can_access_prompt_admin(request):
    """Доступ к управлению промптами: staff/superuser ИЛИ ключ page.prompt_admin."""
    if not request.user.is_authenticated:
        return False
    return is_staff_or_superuser(request.user) or user_has_key(
        request.user, "page.prompt_admin"
    )


def can_access_prompt_defaults(request):
    """Доступ к «Промптам по умолчанию»: суперюзер ИЛИ ключ page.prompt_defaults.

    NB: staff БЕЗ superuser и без ключа — нет (как раньше); ключ выдан
    встроенной «Разработчик промптов», поэтому страница доступна всей
    базовой аудитории админки.
    """
    if not request.user.is_authenticated:
        return False
    return user_has_key(request.user, "page.prompt_defaults")


def can_access_tasks(request):
    """Доступ к доске «Таски»: суперюзер ИЛИ ключ page.tasks (baseline).

    Доска по умолчанию видна любому, кто допущен в админку (page.tasks —
    baseline). Создать/закрыть/переставить — отдельные action-ключи
    (action.task_create/close/reorder, аккордеон страницы в «Группах
    доступа»), сами view проверяют их не здесь.
    """
    if not request.user.is_authenticated:
        return False
    return user_has_key(request.user, "page.tasks")


def can_access_prompt_regression(request):
    """Регрессионные тесты промптов: суперюзер ИЛИ ключ page.prompt_regression."""
    if not request.user.is_authenticated:
        return False
    return user_has_key(request.user, "page.prompt_regression")


def can_access_test_console(request):
    """Тестовая консоль: суперюзер ИЛИ ключ page.test_console."""
    if not request.user.is_authenticated:
        return False
    return user_has_key(request.user, "page.test_console")


def can_access_updates(request):
    """«Обновления»: staff/superuser ИЛИ ключ page.updates."""
    if not request.user.is_authenticated:
        return False
    return is_staff_or_superuser(request.user) or user_has_key(
        request.user, "page.updates"
    )


def can_access_logs(request):
    """Доступ к логам запросов: staff/superuser или ключ-страница журнала.

    Родительский ключ — page.logs («читать свои», baseline); page.logs_own —
    легаси-синоним; page.logs_all — «читать все записи» (вложенное действие
    страницы журнала). Разработчикам без staff-прав журнал открыт со строгим
    ограничением — только собственные запросы (см. ``logs_scope_is_own_user``).
    """
    if not request.user.is_authenticated:
        return False
    if is_staff_or_superuser(request.user):
        return True
    keys = user_access_keys(request.user)
    return bool({"page.logs", "page.logs_own", "page.logs_all"} & keys)


def logs_scope_is_own_user(user):
    """Строгий фильтр журнала запросов: только свои записи.

    True для не-staff юзера без page.logs_all (кто вообще может читать
    журнал) — queryset жёстко ограничивается ``user=<себя>`` и чужие записи
    недоступны даже по прямой ссылке (detail/xlsx/resend дают 403).
    """
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if is_staff_or_superuser(user):
        return False
    keys = user_access_keys(user)
    if "page.logs_all" in keys:
        return False
    return bool({"page.logs", "page.logs_own"} & keys)


def filter_app_list_for_user(app_list, request):
    """Фильтрует список приложений в sidebar по правам пользователя.

    Скрывает модели, к которым у пользователя нет доступа (has_module_permission).
    Пустые non-AI приложения удаляются целиком. AI-приложение оставляется,
    если видны кастомные ссылки (ARM и т.д.), даже без видимых моделей.
    """
    if not app_list:
        return app_list

    user = getattr(request, "user", None)
    is_pd = is_prompt_developer_user(user)
    is_staff = is_staff_or_superuser(user)
    has_any_custom_link = bool(
        getattr(request, "_show_arm_link_cached", None)
        or is_pd
    )

    registry = getattr(request, "_ai_admin_registry", None)
    filtered = []
    for app in app_list:
        app_label = app.get("app_label") if isinstance(app, dict) else getattr(app, "app_label", None)
        visible_models = []
        for model in app.get("models", []):
            # Django stores ModelAdmin by model *class* in _registry.
            # app_list entries use the string "object_name" though, so
            # look up by class if available, otherwise by name.
            model_cls = model.get("_model_cls") if isinstance(model, dict) else None
            model_admin = None
            if registry is not None:
                if model_cls is not None:
                    model_admin = registry.get(model_cls)
                if model_admin is None and model.get("object_name"):
                    for cls, admin_obj in registry.items():
                        if cls.__name__ == model["object_name"]:
                            model_admin = admin_obj
                            break
            if model_admin is None or model_admin.has_module_permission(request):
                visible_models.append(model)
        new_app = dict(app)
        new_app["models"] = visible_models
        if not visible_models and app_label != "ai":
            # Drop empty non-AI apps (e.g., "Authentication and Authorization" for non-staff).
            continue
        if not visible_models and app_label == "ai" and not (is_pd or is_staff):
            # The AI app shows custom links; keep it only if the user can see any of them.
            if has_any_custom_link:
                filtered.append(new_app)
            continue
        filtered.append(new_app)
    return filtered