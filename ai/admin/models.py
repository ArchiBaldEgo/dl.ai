"""ModelAdmin классы для моделей AI-приложения.

Содержит админ-конфигурацию для Prompt, SharedPrompt, Topic, ProgrammingLanguage,
Task, AIRequestLog, AIModelTestRun, PromptTestCase, PromptTestRun и др.
Включает кастомные actions (экспорт CSV, refresh DL-задач) и ACL-проверки.
"""

import csv

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin
from django.db.models import Q
from django.http import HttpResponse
from django.urls import path
from django.utils import timezone

from ..models import (
    AIAppSettings,
    ExternalDLAccount,
    ProgrammingLanguage,
    Prompt,
    PromptTestCase,
    PromptTestRun,
    SharedPrompt,
    Task,
    TaskSolution,
    Topic,
)
from ..querysets import prompt_queryset_for_user
from .forms import PromptForm, SharedPromptForm, TopicForm
from .permissions import can_access_logs, is_prompt_developer_user, is_staff_or_superuser, is_superuser_user
from ..dl_api_client import (
    DLApiError,
    fetch_task_info,
)
from ..http_utils import resolve_dl_session_id
from ..services.task_registry import apply_dl_task_info
from .logs import dl_task_url

User = get_user_model()


class TopicInline(admin.TabularInline):
    model = Topic
    extra = 1
    fk_name = 'programming_language'
    show_change_link = True
    fields = ('topic_name_ru', 'topic_name_en', 'topic_name_fr')


class _StaffOnlyAdminMixin:
    """Mixin that restricts all admin access to staff/superuser only."""

    def has_module_permission(self, request):
        return is_staff_or_superuser(request.user)

    def has_view_permission(self, request, obj=None):
        return is_staff_or_superuser(request.user)

    def has_add_permission(self, request):
        return is_staff_or_superuser(request.user)

    def has_change_permission(self, request, obj=None):
        return is_staff_or_superuser(request.user)

    def has_delete_permission(self, request, obj=None):
        return is_staff_or_superuser(request.user)


class ProgrammingLanguageAdmin(_StaffOnlyAdminMixin, admin.ModelAdmin):
    inlines = [TopicInline]
    list_display = ('language_name',)
    search_fields = ('language_name',)


class TopicAdmin(_StaffOnlyAdminMixin, admin.ModelAdmin):
    form = TopicForm
    list_display = ('topic_name_ru', 'programming_language')
    list_filter = ('programming_language',)
    # «Show counts» — COUNT(*) на каждый вариант фильтра, тормозит загрузку списка.
    show_facets = admin.ShowFacets.NEVER
    search_fields = ('topic_name_ru', 'topic_name_en', 'topic_name_fr')
    raw_id_fields = ('programming_language',)
    fieldsets = (
        (None, {"fields": ("topic_name_ru", "topic_name_en", "topic_name_fr", "programming_language")}),
    )
    actions = ("auto_translate_selected",)

    @admin.action(description="Автоперевод → EN / FR")
    def auto_translate_selected(self, request, queryset):
        from ai.services.auto_translate import translate_object
        translated, skipped, failed = 0, 0, 0
        for obj in queryset:
            results = translate_object(obj, ["topic_name"])
            for v in results.values():
                if v.startswith("skipped"):
                    skipped += 1
                elif v == "failed":
                    failed += 1
                else:
                    translated += 1
        msg = f"Переведено: {translated}, пропущено: {skipped}, ошибок: {failed}"
        self.message_user(request, msg, level=("success" if not failed else "warning"))


class PromptUserIdFilter(admin.SimpleListFilter):
    title = "ID владельца"
    parameter_name = "user_id"

    def lookups(self, request, model_admin):
        if not request.user.is_superuser:
            return ()

        users = (
            User.objects.filter(Q(owned_prompts__isnull=False) | Q(editable_prompts__isnull=False))
            .distinct()
            .order_by("id")
        )
        return [(str(user.id), f"{user.id}: {user.get_username()}") for user in users]

    def queryset(self, request, queryset):
        value = self.value()
        if not value:
            return queryset
        return queryset.filter(Q(owner_id=value) | Q(editors__id=value)).distinct()


class PromptAdmin(admin.ModelAdmin):
    form = PromptForm
    list_display = (
        'prompt_name_ru',
        'programming_language_name',
        'topic',
        'mode_name',
        'owner_user_id',
        'owner_username',
        'short_prompt_text',
    )
    list_display_links = ('prompt_name_ru',)
    # Режим первым: чипы «Все / Реши задачу / В чём ошибка» — главный
    # селектор списка (см. MODE_SOLVE / MODE_FIND_ERROR).
    list_filter = ('mode', PromptUserIdFilter, 'topic__programming_language', 'topic')
    # «Show counts» — COUNT(*) на каждый вариант фильтра, тормозит загрузку списка.
    show_facets = admin.ShowFacets.NEVER
    list_per_page = 25
    search_fields = ('prompt_name_ru', 'prompt_text_ru', 'owner__username', '=owner__id')
    autocomplete_fields = ("owner", "editors")
    actions = ("export_prompts_csv", "auto_translate_selected", "set_mode_solve", "set_mode_find_error")

    @admin.action(description='Назначить режим «Реши задачу»')
    def set_mode_solve(self, request, queryset):
        queryset.update(mode=Prompt.MODE_SOLVE)
        self.message_user(request, "Выбранным промптам назначен режим «Реши задачу».")

    @admin.action(description='Назначить режим «В чём ошибка»')
    def set_mode_find_error(self, request, queryset):
        queryset.update(mode=Prompt.MODE_FIND_ERROR)
        self.message_user(request, "Выбранным промптам назначен режим «В чём ошибка».")
    # Prompt has no created_at field, so date_hierarchy is intentionally None.
    date_hierarchy = None

    def get_queryset(self, request):
        queryset = (
            super()
            .get_queryset(request)
            .select_related("topic", "topic__programming_language", "owner")
            .prefetch_related("editors")
        )
        from .my_prompt import is_mine_only_request
        if is_mine_only_request(request):
            return prompt_queryset_for_user(queryset, request.user)
        return queryset

    @admin.display(description="Режим", ordering="mode")
    def mode_name(self, obj):
        return obj.get_mode_display()

    def lookup_allowed(self, lookup, value, request=None):
        if lookup == "mine":
            return True
        return super().lookup_allowed(lookup, value)

    def _can_edit_prompt(self, request, obj):
        if not (is_staff_or_superuser(request.user) or is_prompt_developer_user(request.user)):
            return False
        if request.user.is_superuser:
            return True
        if obj is None:
            return True
        if obj.owner_id == request.user.pk:
            return True
        return obj.editors.filter(pk=request.user.pk).exists()

    def has_module_permission(self, request):
        if is_staff_or_superuser(request.user):
            return True
        return is_prompt_developer_user(request.user)

    def has_view_permission(self, request, obj=None):
        return is_staff_or_superuser(request.user) or is_prompt_developer_user(request.user)

    def has_change_permission(self, request, obj=None):
        return self._can_edit_prompt(request, obj)

    def has_add_permission(self, request):
        return is_staff_or_superuser(request.user) or is_prompt_developer_user(request.user)

    def has_delete_permission(self, request, obj=None):
        if request.user.is_superuser:
            return True
        if not (is_staff_or_superuser(request.user) or is_prompt_developer_user(request.user)):
            return False
        if obj is None:
            return True
        return obj.owner_id == request.user.pk

    def get_fieldsets(self, request, obj=None):
        # Локализованные поля (prompt_name_ru/en/fr, prompt_text_ru/en/fr)
        # рендерятся ЯЗЫКОВЫМИ ТАБАМИ RU/EN/FR (prompt_translate_tabs.js):
        # на экране всегда 1 название + 1 textarea; при переключении таба
        # пустой язык авто-переводится с заполненного (AJAX →
        # translate-field/ → Google Translate). Базовые prompt_name /
        # prompt_text (fallback для старых записей) и переопределение текста
        # спрятаны в свёрнутый блок, чтобы не громоздить форму.
        main_fields = (
            "mode", "programming_language", "topic", "shared_prompt",
            "prompt_name_ru", "prompt_name_en", "prompt_name_fr",
            "prompt_text_ru", "prompt_text_en", "prompt_text_fr",
        )
        advanced_fields = ("prompt_text_override",)
        if request.user.is_superuser:
            return (
                (None, {"fields": main_fields}),
                ("Переопределение текста", {"fields": advanced_fields, "classes": ("collapse",)}),
                ("Доступ", {"fields": ("owner", "editors"), "classes": ("collapse",)}),
            )
        return (
            (None, {"fields": main_fields}),
            ("Переопределение текста", {"fields": advanced_fields, "classes": ("collapse",)}),
        )

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "translate-field/",
                self.admin_site.admin_view(self.admin_translate_field_view),
                name="ai_prompt_translate_field",
            ),
        ]
        return custom + urls

    def admin_translate_field_view(self, request):
        """AJAX-перевод для языковых табов формы препромпта.

        POST text + target (ru|en|fr) → Google Translate (deep-translator,
        тот же сервис, что у массового автоперевода). Только для тех, кто
        может редактировать промпты; /ai/admin/ исключён из RateLimitMiddleware.
        """
        from django.http import JsonResponse

        if request.method != "POST":
            return JsonResponse({"success": False, "error": "Метод не поддерживается"}, status=403)
        if not self.has_change_permission(request):
            return JsonResponse({"success": False, "error": "Нет прав на редактирование промптов"}, status=403)
        text = (request.POST.get("text") or "").strip()
        target = request.POST.get("target") or ""
        if target not in ("ru", "en", "fr"):
            return JsonResponse({"success": False, "error": "Неизвестный целевой язык"}, status=400)
        if not text:
            return JsonResponse({"success": False, "error": "Нет текста для перевода"}, status=400)
        from ..services.auto_translate import translate_text
        translated = translate_text(text, target)
        if not translated:
            return JsonResponse(
                {"success": False, "error": "Сервис перевода недоступен, попробуйте позже"},
                status=502,
            )
        return JsonResponse({"success": True, "text": translated})

    def get_readonly_fields(self, request, obj=None):
        if is_staff_or_superuser(request.user):
            return ()
        if self._can_edit_prompt(request, obj):
            return ()
        return (
            "programming_language", "topic",
            "prompt_name_ru", "prompt_name_en", "prompt_name_fr",
            "shared_prompt", "prompt_text_override",
            "prompt_text_ru", "prompt_text_en", "prompt_text_fr",
        )

    def save_model(self, request, obj, form, change):
        if not change and not obj.owner_id:
            obj.owner = request.user
        super().save_model(request, obj, form, change)
        if not request.user.is_superuser:
            obj.editors.add(request.user)

    def export_prompts_csv(self, request, queryset):
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="prompts.csv"'
        writer = csv.writer(response)
        writer.writerow(["id", "prompt_name", "language", "topic", "owner_id", "owner_username", "prompt_text"])
        for prompt in queryset.select_related("topic", "topic__programming_language", "owner"):
            topic = prompt.topic
            language = topic.programming_language.language_name if topic and topic.programming_language else ""
            writer.writerow([
                prompt.id,
                prompt.prompt_name_ru or "",
                language,
                topic.topic_name_ru if topic else "",
                prompt.owner_id or "",
                prompt.owner.username if prompt.owner else "",
                prompt.prompt_text_ru,
            ])
        return response
    export_prompts_csv.short_description = "Экспорт выбранных промптов в CSV"

    @admin.action(description="Автоперевод → EN / FR")
    def auto_translate_selected(self, request, queryset):
        from ai.services.auto_translate import translate_object
        translated, skipped, failed = 0, 0, 0
        for obj in queryset:
            results = translate_object(obj, ["prompt_name", "prompt_text"])
            for v in results.values():
                if v.startswith("skipped"):
                    skipped += 1
                elif v == "failed":
                    failed += 1
                else:
                    translated += 1
        msg = f"Переведено: {translated}, пропущено: {skipped}, ошибок: {failed}"
        self.message_user(request, msg, level=("success" if not failed else "warning"))

    def programming_language_name(self, obj):
        if obj.topic and obj.topic.programming_language:
            return obj.topic.programming_language.language_name
        return "-"
    programming_language_name.short_description = "Язык программирования"
    programming_language_name.admin_order_field = "topic__programming_language__language_name"

    def programming_language(self, obj):
        # Display method for the read-only rendering of the declared
        # ``programming_language`` form field (see PromptForm). These two are
        # coupled — remove the declared field and this method breaks the
        # readonly/fieldset path with FieldError.
        return self.programming_language_name(obj)
    programming_language.short_description = "Язык программирования"

    def owner_user_id(self, obj):
        return obj.owner_id or "-"
    owner_user_id.short_description = "ID владельца"
    owner_user_id.admin_order_field = "owner_id"

    def owner_username(self, obj):
        return obj.owner.username if obj.owner else "-"
    owner_username.short_description = "Владелец"

    def short_prompt_text(self, obj):
        text = obj.prompt_text_ru or ""
        return f"{text[:100]}..." if len(text) > 100 else text
    short_prompt_text.short_description = "Текст промпта"


class SharedPromptAdmin(admin.ModelAdmin):
    form = SharedPromptForm
    list_display = ('prompt_name_ru', 'mode', 'language_list', 'updated_at', 'owner_username')
    list_display_links = ('prompt_name_ru',)
    list_filter = ('mode', 'programming_languages')
    # «Show counts» — COUNT(*) на каждый вариант фильтра, тормозит загрузку списка.
    show_facets = admin.ShowFacets.NEVER
    search_fields = ('prompt_name_ru', 'prompt_text_ru')
    autocomplete_fields = ('owner', 'editors')
    # 'editors' is rendered by autocomplete_fields above (autocomplete wins in
    # Django's formfield_for_manytomany), so only 'programming_languages' uses
    # the horizontal filter widget — listing 'editors' here was dead config.
    filter_horizontal = ('programming_languages',)
    actions = ("auto_translate_selected",)

    def language_list(self, obj):
        langs = obj.programming_languages.all()
        return ", ".join([l.language_name for l in langs]) if langs else "Все языки"
    language_list.short_description = "Языки"

    def owner_username(self, obj):
        return obj.owner.username if obj.owner else "-"
    owner_username.short_description = "Владелец"

    @admin.action(description="Автоперевод → EN / FR")
    def auto_translate_selected(self, request, queryset):
        from ai.services.auto_translate import translate_object
        translated, skipped, failed = 0, 0, 0
        for obj in queryset:
            results = translate_object(obj, ["prompt_name", "prompt_text"])
            for v in results.values():
                if v.startswith("skipped"):
                    skipped += 1
                elif v == "failed":
                    failed += 1
                else:
                    translated += 1
        msg = f"Переведено: {translated}, пропущено: {skipped}, ошибок: {failed}"
        self.message_user(request, msg, level=("success" if not failed else "warning"))

    def has_module_permission(self, request):
        return is_staff_or_superuser(request.user)

    def has_view_permission(self, request, obj=None):
        return is_staff_or_superuser(request.user)

    def has_add_permission(self, request):
        return is_staff_or_superuser(request.user)

    def has_change_permission(self, request, obj=None):
        return is_staff_or_superuser(request.user)

    def has_delete_permission(self, request, obj=None):
        return is_staff_or_superuser(request.user)

    def get_fieldsets(self, request, obj=None):
        return (
            (None, {"fields": (
                "prompt_name_ru", "prompt_name_en", "prompt_name_fr",
                "mode",
                "prompt_text_ru", "prompt_text_en", "prompt_text_fr",
                "programming_languages",
            )}),
            ("Доступ", {"fields": ("owner", "editors"), "classes": ("collapse",)}),
        )


class AIAppSettingsAdmin(_StaffOnlyAdminMixin, admin.ModelAdmin):
    list_display = ("is_enabled", "updated_at")
    # favorites_epoch на форме не нужен (меняется только командой
    # reset_favorites_epoch); updated_at — авторасчётное, тоже не редактируем.
    exclude = ("favorites_epoch", "updated_at")
    change_form_template = "admin/ai/aiappsettings_change_form.html"

    def has_add_permission(self, request):
        if not is_staff_or_superuser(request.user):
            return False
        if AIAppSettings.objects.exists():
            return False
        return super().has_add_permission(request)

    def has_delete_permission(self, request, obj=None):
        if not is_staff_or_superuser(request.user):
            return False
        return False

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        """Добавляем на страницу настроек таблицы последних запросов.

        Два блока: последние 5 batch-solve прогонов (по клику на строку
        разворачивается та же таблица результатов, что после прогона на
        /arm/solve/) и последние 5 записей журнала — те же, что в «Журнале
        запросов», но сокращённые. Если нужно искать конкретную запись —
        кнопка ведёт в полный журнал с поиском по всему журналу.
        """
        from .logs import build_recent_batch_rows, build_recent_log_rows

        extra_context = {**(extra_context or {})}
        extra_context.update(build_recent_log_rows(request, limit=5))
        extra_context.update(build_recent_batch_rows(request, limit=5))
        return super().changeform_view(request, object_id, form_url, extra_context)


class ExternalDLAccountAdmin(_StaffOnlyAdminMixin, admin.ModelAdmin):
    """Admin for viewing external DL accounts (dl.gsu.by user info)."""
    list_display = ("external_user_id", "external_login", "external_first_name",
                    "external_last_name", "dl_email", "education_summary",
                    "user_link", "created_at", "updated_at")
    list_display_links = ("external_user_id",)
    list_filter = ("created_at", "updated_at")
    # «Show counts» — COUNT(*) на каждый вариант фильтра, тормозит загрузку списка.
    show_facets = admin.ShowFacets.NEVER
    search_fields = ("external_user_id", "external_login",
                     "external_first_name", "external_last_name",
                     "dl_email", "education_school_no", "user__username")
    # Учебные данные приходят из dl.gsu.by при входе пользователя (REST API.md
    # get-user-info) — руками их не правим, только смотрим.
    readonly_fields = ("dl_email", "education_form", "education_school_id",
                       "education_school_kind", "education_school_no",
                       "education_group_mask_id", "education_form_letter",
                       "created_at", "updated_at")
    raw_id_fields = ("user",)

    def user_link(self, obj):
        if obj.user:
            return obj.user.username
        return "-"
    user_link.short_description = "Локальный пользователь"
    user_link.admin_order_field = "user__username"

    def education_summary(self, obj):
        """Компактная сводка блока education: «форма · школа № · группа · класс»."""
        parts = [obj.education_form, obj.education_school_no,
                 obj.education_group_mask_id, obj.education_form_letter]
        parts = [p for p in parts if p]
        return " · ".join(parts) if parts else "—"
    education_summary.short_description = "Учебные данные"


class PromptEditorshipInline(admin.TabularInline):
    """Права редактирования промптов на странице пользователя (суперюзер).

    Работает с implicit through-моделью ``Prompt.editors.through``: страница
    аккаунта редактирует строки «кто какой чужой промпт может править».
    ``fk_name="user"`` — точка монтирования со страницы пользователя. Сама
    through-модель в ai_admin_site НЕ регистрируется.
    """

    model = Prompt.editors.through
    fk_name = "user"
    autocomplete_fields = ("prompt",)
    extra = 0
    classes = ("collapse",)
    verbose_name = "Право на промпт"
    verbose_name_plural = "Права редактирования промптов"


class RestrictedUserAdmin(_StaffOnlyAdminMixin, UserAdmin):
    """User management restricted to staff/superuser in the AI admin site."""

    list_display = ("last_name", "first_name", "dl_id", "email", "is_staff_badge")
    list_display_links = ("dl_id",)
    list_filter = ("is_staff", "is_superuser", "groups")
    # «Show counts» — COUNT(*) на каждый вариант фильтра, тормозит загрузку списка.
    show_facets = admin.ShowFacets.NEVER
    search_fields = ("username", "first_name", "last_name", "email")
    ordering = ("last_name", "first_name")

    def get_inlines(self, request, obj=None):
        # Выдача прав на чужие промпты — только суперюзеру (зеркало правила
        # «Доступ» fieldset в PromptAdmin).
        if not is_superuser_user(request.user):
            return super().get_inlines(request, obj)
        return [*(super().get_inlines(request, obj) or []), PromptEditorshipInline]

    def is_staff_badge(self, obj):
        if obj.is_superuser:
            return "🟣"
        if obj.is_staff:
            return "🔵"
        return ""
    is_staff_badge.short_description = "Роль"
    is_staff_badge.admin_order_field = "is_staff"

    def dl_id(self, obj):
        """Display the DL user ID, stripping the 'user_' prefix."""
        name = obj.username or ""
        if name.startswith("user_"):
            return name[5:]
        return name
    dl_id.short_description = "ID в dl.gsu.by"
    dl_id.admin_order_field = "username"


class TaskAdmin(_StaffOnlyAdminMixin, admin.ModelAdmin):
    """Admin for the local Task table used by batch-solve ARM.

    The operator enters the DL ``node_id`` and assigns a topic / programming
    language / ``file_extension`` locally. ``name``/``statement``/``task_id``
    are DL-owned (fetched via ``refresh_from_dl``) and shown read-only.
    """

    list_display = ("node_id", "name", "topic", "programming_language", "file_extension", "active", "updated_at")
    list_display_links = ("node_id", "name")
    list_filter = ("active", "topic", "topic__programming_language")
    # «Show counts» — COUNT(*) на каждый вариант фильтра, тормозит загрузку списка.
    show_facets = admin.ShowFacets.NEVER
    list_editable = ("file_extension", "active")
    search_fields = ("node_id", "task_id", "name", "statement")
    autocomplete_fields = ("topic", "programming_language")
    readonly_fields = ("name", "statement", "task_id", "created_at", "updated_at")
    actions = ("refresh_from_dl",)

    fieldsets = (
        (None, {"fields": ("node_id", "task_id", "name", "statement")}),
        ("Локальная привязка", {"fields": ("topic", "programming_language", "file_extension", "active")}),
        ("Метаданные", {"fields": ("created_at", "updated_at")}),
    )

    def get_readonly_fields(self, request, obj=None):
        # node_id is editable only on add; once set it identifies the DL task.
        base = list(self.readonly_fields)
        if obj is not None:
            base.append("node_id")
        return base

    def refresh_from_dl(self, request, queryset):
        """Fetch name/statement/task_id from DL for the selected tasks.

        Requires the admin's session to carry a valid DL session id (DLSID
        flow), exactly like ``get_task_info_view``.
        """
        session_id = request.session.get("external_session_id", "").strip()
        if not session_id:
            session_id = resolve_dl_session_id(request)
        if not session_id:
            self.message_user(request, "Нет DLSID — обновление из DL невозможно.", level="ERROR")
            return

        updated = 0
        failed = 0
        for task in queryset:
            try:
                data = fetch_task_info(task.node_id, session_id=session_id, remove_html_tags=True)
            except DLApiError as exc:
                failed += 1
                self.message_user(
                    request,
                    f"DL #{task.node_id}: не удалось обновить ({exc}).",
                    level="WARNING",
                )
                continue
            apply_dl_task_info(task, data)
            task.save(update_fields=["task_id", "name", "statement"])
            updated += 1
        self.message_user(request, f"Обновлено из DL: {updated}, ошибок: {failed}.")
    refresh_from_dl.short_description = "Обновить название и условие из DL"


class TaskSolutionAdmin(_StaffOnlyAdminMixin, admin.ModelAdmin):
    """Кэш решённых задач «Реши задачу» (узел DL + язык → проверенный код).

    Записи создаются автоматически (send-solution + успешное тестирование со
    страницы «Реши задачу») и выдаются повторным запросам той же задачи без
    вызова модели. Редактирование вручную не предусмотрено — только просмотр.

    Список — кастомный (change_list_template): строка показывает дату,
    название задачи со ссылкой в DL, имя языка, тему и препромпт; клик по
    строке (кроме ссылки на задачу) раскрывает сохранённый код.
    """

    change_list_template = "admin/ai/tasksolution_changelist.html"

    # Без list_filter: в кэш попадают только решения, прошедшие тестирование
    # (verdict=passed всегда); на странице нужны только поиск и пагинация.
    search_fields = ("task_node_id", "external_user_id", "model_key", "topic_name", "prompt_name")
    # Каждая строка несёт полный код решения — режем страницу.
    list_per_page = 25
    readonly_fields = ("task_node_id", "programming_language_id", "file_extension", "code", "verdict",
                       "dl_comment", "model_key", "model_title", "topic_id", "topic_name",
                       "prompt_id", "prompt_name", "queue_id", "test_log", "submitted_at",
                       "created_by", "external_user_id", "times_used", "created_at", "updated_at")

    fieldsets = (
        (None, {"fields": ("task_node_id", "programming_language_id", "file_extension", "verdict")}),
        ("Решение", {"fields": ("code", "dl_comment", "model_key", "model_title")}),
        ("Контекст", {"fields": ("topic_id", "topic_name", "prompt_id", "prompt_name")}),
        ("Метаданные", {"fields": ("queue_id", "test_log", "submitted_at", "created_by", "external_user_id", "times_used", "created_at", "updated_at")}),
    )

    def changelist_view(self, request, extra_context=None):
        """Строки списка, подготовленные для отображения: название задачи
        (один запрос Task по node_id страницы), имя языка вместо сырого id,
        локализованная дата (МСК), ссылка на задачу в DL.

        Шаблон admin/ai/tasksolution_changelist.html рендерит sol_rows;
        поиск/фильтр/пагинация работают штатно через cl.
        """
        response = super().changelist_view(request, extra_context)
        if response.status_code != 200 or not getattr(response, "context_data", None):
            return response
        solutions = list(response.context_data["cl"].result_list)
        task_names = {
            t.node_id: t.name
            for t in Task.objects.filter(
                node_id__in={s.task_node_id for s in solutions}
            ).only("node_id", "name")
        }
        lang_names = {
            lang.pk: lang.language_name
            for lang in ProgrammingLanguage.objects.filter(
                pk__in={s.programming_language_id for s in solutions if s.programming_language_id}
            ).only("pk", "language_name")
        }
        rows = []
        for s in solutions:
            dt = s.submitted_at or s.created_at
            rows.append({
                "obj": s,
                "date": timezone.localtime(dt).strftime("%d.%m.%Y %H:%M") if dt else "—",
                "task_name": task_names.get(s.task_node_id) or f"Задача #{s.task_node_id}",
                "task_url": dl_task_url(s.task_node_id, s.course_id) or "",
                "lang_name": lang_names.get(s.programming_language_id) or (
                    str(s.programming_language_id) if s.programming_language_id else "—"
                ),
                "topic_name": s.topic_name or "—",
                "prompt_name": s.prompt_name or "—",
                "user": s.external_user_id or "—",
                "times_used": s.times_used,
                "log_url": f"/ai/admin/ai/airequestlog/{s.test_log_id}/" if s.test_log_id else "",
            })
        response.context_data["sol_rows"] = rows
        return response

    # Кэш пишется только автоматикой; ручное добавление/правка не предусмотрены.
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return is_superuser_user(request.user)


class PromptTestCaseAdmin(admin.ModelAdmin):
    """Admin for prompt regression test fixtures (input + golden + comparator).

    Доступ только суперпользователю (тест-кейсы промптов — по решению владельца
    закрыты от staff/prompt_developer).
    """

    def has_module_permission(self, request):
        return is_superuser_user(request.user)

    def has_view_permission(self, request, obj=None):
        return is_superuser_user(request.user)

    def has_add_permission(self, request):
        return is_staff_or_superuser(request.user)

    def has_change_permission(self, request, obj=None):
        return is_staff_or_superuser(request.user)

    def has_delete_permission(self, request, obj=None):
        return is_staff_or_superuser(request.user)

    list_display = ("name", "mode", "topic", "programming_language", "comparator", "active", "updated_at")
    list_display_links = ("name",)
    list_filter = ("mode", "active", "topic", "comparator")
    # «Show counts» — COUNT(*) на каждый вариант фильтра, тормозит загрузку списка.
    show_facets = admin.ShowFacets.NEVER
    list_editable = ("active",)
    search_fields = ("name", "input_text", "expected_text")
    autocomplete_fields = ("topic", "programming_language")
    readonly_fields = ("created_at", "updated_at")

    fieldsets = (
        (None, {"fields": ("name", "mode", "active")}),
        ("Ввод и эталон", {"fields": ("input_text", "expected_text", "comparator", "match_threshold")}),
        ("Привязка", {"fields": ("topic", "programming_language", "ui_language", "owner")}),
        ("Метаданные", {"fields": ("created_at", "updated_at")}),
    )


class PromptTestRunAdmin(admin.ModelAdmin):
    """Read-only admin for prompt regression runs (the rich UI is the custom page).

    Доступ только суперпользователю (прогоны регрессионных тестов — по решению
    владельца закрыты от staff/prompt_developer).
    """

    def has_module_permission(self, request):
        return is_superuser_user(request.user)

    def has_view_permission(self, request, obj=None):
        return is_superuser_user(request.user)

    list_display = ("run_id", "model_title", "prompt_name", "status", "total_cases", "started_at", "finished_at")
    list_filter = ("status", "model_key")
    # «Show counts» — COUNT(*) на каждый вариант фильтра, тормозит загрузку списка.
    show_facets = admin.ShowFacets.NEVER
    search_fields = ("run_id", "model_title", "prompt_name", "error_message")
    readonly_fields = (
        "run_id", "status", "model_key", "model_title", "prompt_id", "prompt_name",
        "ui_language", "user", "started_at", "finished_at", "error_message", "report", "total_cases",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
