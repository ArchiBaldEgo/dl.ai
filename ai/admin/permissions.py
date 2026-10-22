"""Хелперы проверки прав доступа для AI admin views и ModelAdmin.

Все can_access_* резолвятся единообразно: superuser видит всё по умолчанию,
остальным доступ определяется ключами их групп (AIAccessGroup, страница
«Группы доступа» /ai/admin/ai-groups/). Участники групп — не staff: правки
чужих промптов/тем/языков/препромтов и студентов им недоступны не из-за
ключей, а потому что соответствующие ModelAdmin'ы за _StaffOnlyAdminMixin,
правку чужого промпта открывает вложенный ключ page.prompt_admin.edit_others. Жёстких Django-Group больше
нет: legacy-группа prompt_developer перенесена во встроенную AIAccessGroup
«Разработчик промптов» миграцией и из проверок удалена.
"""

from ..constants import AI_CACHE_KEY_PREFIX, BUILTIN_GROUP_DEVELOPER, BUILTIN_GROUP_PROMPT_DEV
from ..models import AIAccessGroup, AIAdminGroupPermission

#: pk группы-эмуляции режима «Посмотреть как…» (ai/admin/guest_mode.py), на
#: время запроса проставляется атрибутом на request.user. Центральные
#: проверки здесь и в ModelAdmin'ах честно вычисляют права участника
#: выбранной группы: супер/staff-обход отключён, членство = группа.
_VIEW_AS_ATTR = "_ai_view_as_group_pk"

#: Атрибут request.user для request-scoped кэша ключей доступа.
#: user_access_keys дёргается ~20 гейтами на КАЖДЫЙ запрос админки, и
#: каждый вызов — это m2m-джойн к таблице ключей. Кэш живёт на время
#: запроса (экземпляр user) и ключуется парой (user.pk, view_as_group_pk) —
#: кэш только по user.pk сломал бы эмуляцию «Посмотреть как…».
_ACCESS_KEYS_CACHE_ATTR = "_ai_access_keys_cache"

#: Общий (не request-scoped) кэш полного набора ключей — он от запроса не
#: зависит: суперюзер видит ВСЕ существующие ключи. Инвалидация — по
#: post_save/post_delete AIAdminGroupPermission (ai/signals.py).
_ALL_KEYS_CACHE_KEY = f"{AI_CACHE_KEY_PREFIX}:all_access_keys"
_ALL_KEYS_CACHE_TTL = 300

#: Версия набора прав (process-local счётчик). Любое изменение состава
#: групп или самих прав увеличивает счётчик — и request-scoped кэш ключей
#: (см. ``_ACCESS_KEYS_CACHE_ATTR``) перестаёт совпадать, т.е. права
#: пересчитываются на следующем гейте. Счётчик локальный для процесса
#: намеренно: кэш живёт на инстансе request.user, а запрос обслуживает
#: один процесс — рассинхрона между процессами тут не бывает.
_ACCESS_KEYS_EPOCH = 0


def bump_access_keys_epoch():
    """Инвалидировать request-scoped кэш ключей доступа (состав групп/права).

    Вызывается из ai/signals.py при изменении AIAccessGroup, его m2m-связей
    (users/permissions) и AIAdminGroupPermission. Без этого кэш на инстансе
    request.user отдавал бы права, отменённые прямо в ходе запроса.
    """
    global _ACCESS_KEYS_EPOCH
    _ACCESS_KEYS_EPOCH += 1


def _view_as_group_pk(user):
    return getattr(user, _VIEW_AS_ATTR, None)


def _all_access_keys() -> set:
    """Все существующие ключи доступа (для суперпользователя).

    Один SELECT + django-cache: набор ключей меняется редко (правка прав в
    «Группах доступа»), а суперюзер иначе платит этим запросом на КАЖДЫЙ
    гейт каждого запроса.
    """
    from django.core.cache import cache

    cached = cache.get(_ALL_KEYS_CACHE_KEY)
    if cached is not None:
        return set(cached)
    keys = set(AIAdminGroupPermission.objects.values_list("key", flat=True))
    cache.set(_ALL_KEYS_CACHE_KEY, keys, _ALL_KEYS_CACHE_TTL)
    return keys


#: Базовые права ЛЮБОГО, кто допущен в админку (ТЗ 2026-10-19): Пакетное
#: решение ARM, «Мой промпт» (= page.prompt_admin), «Видеть все промпты»
#: (чтение чужих), «Промпты по умолчанию», «Журнал запросов» (свои записи;
#: супер/staff — все). Таски и Состояние моделей НЕ в базе (решение
#: владельца 2026-10-20: у «Разработчика промптов» они сидируются
#: миграцией 0063, у «Разработчика» жили в сиде с 0060 — выдаются
#: группами, как и остальное).
#: Модель ДОБАВЛЯЮЩАЯ: группы поверх baseline выдают действия страниц
#: (создать/закрыть/переставить), «перезаписывать чужое», «читать все
#: записи» журнала, главы документации и т.д. Отнять базовое нельзя.
BASELINE_ACCESS_KEYS = frozenset({
    "page.arm_solve",
    "page.prompt_admin",
    "page.prompt_admin_all",
    "page.prompt_defaults",
    "page.logs",
})


def is_prompt_developer_user(user):
    """Членство во встроенной группе «Разработчик промптов».

    Табличное право (AIAccessGroup), а не Django-Group: выдаётся
    автоматически при провиженинге нового DL-пользователя и правится
    суперюзером на «Группах доступа» (отзыв теперь «липкий»).
    В режиме «Посмотреть как…» — только выбранная группа.
    """
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_active", True) is False:
        return False
    if _view_as_group_pk(user):
        return user_has_group_name(user, BUILTIN_GROUP_PROMPT_DEV)
    return user.ai_access_groups.filter(name=BUILTIN_GROUP_PROMPT_DEV).exists()


def is_developer_user(user):
    """Членство во встроенной группе «Разработчик» (близко к staff).

    Табличное право, как у «Разработчика промптов»; в режиме
    «Посмотреть как…» — только выбранная группа.
    """
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_active", True) is False:
        return False
    if _view_as_group_pk(user):
        return user_has_group_name(user, BUILTIN_GROUP_DEVELOPER)
    return user.ai_access_groups.filter(name=BUILTIN_GROUP_DEVELOPER).exists()


def user_has_group_name(user, name):
    """Участие юзера в группе доступа ПО ИМЕНИ (честная эмуляция смотрит в неё же)."""
    if not user or not getattr(user, "is_authenticated", False):
        return False
    group_pk = _view_as_group_pk(user)
    if group_pk:
        group = AIAccessGroup.objects.filter(pk=group_pk).first()
        return bool(group and group.name == name)
    return user.ai_access_groups.filter(name=name).exists()


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
    """Проверяет, является ли пользователь staff или суперпользователем.

    В режиме «Посмотреть как…» — всегда False: эмуляция честная
    (супер-обход отключён, доступ = ключи группы).
    """
    if not user:
        return False
    if getattr(user, "is_active", True) is False:
        return False
    if _view_as_group_pk(user):
        return False
    return bool(user.is_superuser or user.is_staff)


def is_superuser_user(user):
    """Проверяет, является ли пользователь суперпользователем.

    Отдельный от ``is_staff_or_superuser`` помощник: некоторые инструменты
    (тестовая консоль и другие супер-инструменты) по решению
    владельца доступны ТОЛЬКО суперпользователю — даже staff без флага
    ``is_superuser`` их не видит (если ключ не выдан через группы).
    В режиме «Посмотреть как…» — всегда False (честная эмуляция).
    """
    if not user:
        return False
    if getattr(user, "is_active", True) is False:
        return False
    if _view_as_group_pk(user):
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
    Режим «Посмотреть как…»: ключи РОВНО выбранной группы (+baseline при
    page.admin) — реальные группы юзера игнорируются.

    Результат кэшируется на время запроса (атрибут экземпляра user) под
    ключом (user.pk, view_as_group_pk): гейтов ~20, а каждый без кэша —
    это запрос к m2m-таблице ключей.
    """
    if not user or not getattr(user, "is_authenticated", False):
        return set()
    if getattr(user, "is_active", True) is False:
        return set()
    view_as_pk = _view_as_group_pk(user)
    cache_key = (getattr(user, "pk", None), view_as_pk, _ACCESS_KEYS_EPOCH)
    cached = getattr(user, _ACCESS_KEYS_CACHE_ATTR, None)
    if cached is not None and cached[0] == cache_key:
        return set(cached[1])
    keys = _compute_user_access_keys(user, view_as_pk)
    try:
        setattr(user, _ACCESS_KEYS_CACHE_ATTR, (cache_key, keys))
    except AttributeError:  # pragma: no cover — экземпляр без __dict__
        pass
    return set(keys)


def _compute_user_access_keys(user, view_as_pk) -> set:
    """Собственно вычисление ключей (без кэша) — см. ``user_access_keys``."""
    if view_as_pk:
        group_keys = set(
            AIAccessGroup.objects.filter(pk=view_as_pk)
            .values_list("permissions__key", flat=True)
        )
        if "page.admin" in group_keys:
            group_keys |= BASELINE_ACCESS_KEYS
        return group_keys
    if user.is_superuser:
        return _all_access_keys()
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


def can_access_test_console(request):
    """Тестовая консоль: суперюзер ИЛИ ключ page.test_console."""
    if not request.user.is_authenticated:
        return False
    return user_has_key(request.user, "page.test_console")


def can_access_pinned_runs(request):
    """«Закреплённые пакетные решения»: те же права, что у журнала, ИЛИ ключ.

    Раньше права зашивались на журнал (пин живёт в журнале), поэтому ключ
    page.logs/page.logs_own/page.logs_all признаётся наравне с новым
    page.pinned_runs — старые группы не теряют доступ.
    """
    if can_access_logs(request):
        return True
    return user_has_key(request.user, "page.pinned_runs")


def can_access_token_usage(request):
    """«Контроль токенов»: staff/superuser ИЛИ ключ page.tokens.

    Ключ НЕ входит в BASELINE_ACCESS_KEYS: страница показывает расход
    ВСЕХ пользователей — выдаётся группами осознанно (через «Группы
    доступа»), как «Состояние моделей» и «Таски».
    """
    if not request.user.is_authenticated:
        return False
    return is_staff_or_superuser(request.user) or user_has_key(
        request.user, "page.tokens"
    )


def can_access_dl_api_requests(request):
    """«Запросы по DL API»: staff/superuser ИЛИ ключ page.dl_api_requests.

    Заявки на создание API к dl.gsu.by видят те, кому их доверили: ключ
    выдаётся группами, в baseline не входит.
    """
    if not request.user.is_authenticated:
        return False
    return is_staff_or_superuser(request.user) or user_has_key(
        request.user, "page.dl_api_requests"
    )


def can_access_user_models(request):
    """«Мои AI-модели»: сотрудник админки ИЛИ владелец своих моделей.

    Страницу видит любой сотрудник админки (она — про СВОИ модели), но
    чужие модели чужие: доступ к записям — только владельцу
    (проверка в ai/services/user_models.py).
    """
    if not request.user.is_authenticated:
        return False
    return is_staff_or_superuser(request.user) or user_has_key(
        request.user, "page.user_models"
    )


def can_access_prompt_developer_docs(user):
    """Док «Разработчик промптов»: staff/super ИЛИ ключ docs.prompt_developer.

    Решение владельца 2026-10-20: членство во встроенной группе
    «Разработчик промптов» главу больше НЕ открывает — вся документация
    выдается ключами в «Группах доступа», как и страницы. (Роль_pd оставляет
    доступ к «Моему промпту» и т.п., но не к докам.)
    """
    if is_staff_or_superuser(user):
        return True
    return user_has_key(user, "docs.prompt_developer")


def can_access_test_data(request):
    """«Тестовые данные»: суперюзер ИЛИ ключ page.test_data.

    Раньше только суперюзер (генератор фейковых записей журнала); ключ
    доверяет не-staff управление журналом — выдаётся осознанно. Гостевой
    режим здесь не проверяется: он прячет строку нава (site.py), прямой
    URL в режиме «Посмотреть как…» честно считается по ключам группы.
    """
    if not request.user.is_authenticated:
        return False
    return is_superuser_user(request.user) or user_has_key(
        request.user, "page.test_data"
    )


def can_access_access_groups(request):
    """«Группы доступа»: суперюзер ИЛИ ключ page.groups.

    Держатель ключа управляет правами групп, включая собственную; выдаётся
    осознанно (он может раздать любой из существующих ключей). Гость
    отсекается в access_groups.py::_require_superuser (единственная
    страница, у которой прямой URL закрыт в эмуляции).
    """
    if not request.user.is_authenticated:
        return False
    return is_superuser_user(request.user) or user_has_key(
        request.user, "page.groups"
    )


def can_edit_access_groups(request):
    """Право РАЗДАВАТЬ группы доступа: менять состав AIAccessGroup.

    Держатель page.groups (или суперюзер) может выдавать группы не только
    из «Групп доступа», но и на странице пользователя / DL-аккаунта. Отличие
    от can_access_access_groups — режим «Посмотреть как…»: там состав групп
    менять нельзя нигде (эмуляция честная, 403 как на самих «Группах»).
    """
    if not can_access_access_groups(request):
        return False
    from .guest_mode import is_viewing_as_guest  # ленивый: guest_mode импортирует permissions

    return not is_viewing_as_guest(request)


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