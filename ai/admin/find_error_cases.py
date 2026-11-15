"""Админка базы ошибочных программ «В чём ошибка?».

Доступ — ключ ``page.prompt_lab`` (он же открывает лабораторию). Удаление
кейсов/групп — только staff/superuser: контент-владельцы пополняют базу, но
не должны терять историю случайным удалением.

Формы добавления намеренно короткие (KISS): при создании показываются только
поля, нужные разработчику и привязке к DL (название, язык/тема, nid/cid,
условие; для кейса — группа, название, код и метки). Технические поля импорта
(``dataset``/``source_ref``/``source_hash``/``statement_source`` и служебные
даты) заполняются автоматически и видны только на форме редактирования.
"""

from __future__ import annotations

from django import forms
from django.contrib import admin, messages
from django.db.models import Count
from django.utils import timezone
from django.utils.html import format_html

from ..dl_api_client import DLApiError, fetch_task_info
from ..http_utils import resolve_dl_session_id
from ..models import FindErrorCase, FindErrorCaseGroup, FindErrorCaseLabel
from ..services.find_error_catalog import ERROR_CATALOG
from ..services.task_registry import dl_task_url
from .changelist import FastChangelistAdminMixin
from .models import _StaffOnlyAdminMixin
from .permissions import is_staff_or_superuser


class FindErrorCaseLabelInline(admin.TabularInline):
    model = FindErrorCaseLabel
    extra = 1
    fields = ("code", "text")

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        formfield = super().formfield_for_dbfield(db_field, request, **kwargs)
        if db_field.name == "code":
            formfield.widget = forms.Select(choices=[
                (code, f"error_{code} — {label}")
                for code, label in sorted(ERROR_CATALOG.items())
            ])
        return formfield


def _unique_source_ref(model, base: str) -> str:
    """Уникальный ``source_ref`` для записи, созданной вручную в админке.

    Импорт ищет группы/кейсы по ``source_ref``; ручные записи получают
    префикс ``manual/`` и суффикс ``(2)``, ``(3)``… при совпадении имени.
    """
    candidate = base
    index = 2
    while model.objects.filter(source_ref=candidate).exists():
        candidate = f"{base} ({index})"
        index += 1
    return candidate


def _fetch_statement_for_group(group, request) -> tuple[bool, str]:
    """Обновить условие группы из DL; (успех, сообщение)."""
    if not group.dl_node_id:
        return False, f"{group.name}: нет dl_node_id — пропущено."
    session_id = resolve_dl_session_id(request)
    if not session_id:
        raise DLApiError("Нет активной сессии DL (DLSID) — войдите на dl.gsu.by и повторите.")
    data = fetch_task_info(group.dl_node_id, session_id, course_id=group.dl_course_id)
    statement = (data.get("statement") or "").strip()
    if not statement:
        return False, f"{group.name}: DL вернул пустое условие."
    group.statement = statement
    group.statement_source = "dl"
    group.statement_updated_at = timezone.now()
    group.save(update_fields=["statement", "statement_source", "statement_updated_at", "updated_at"])
    return True, f"{group.name}: условие обновлено из DL."


class FindErrorCaseGroupAdmin(FastChangelistAdminMixin, _StaffOnlyAdminMixin, admin.ModelAdmin):
    ACCESS_KEY = "page.prompt_lab"

    list_display = (
        "name", "dataset", "language_column", "node_column", "statement_source",
        "cases_count", "updated_at",
    )
    list_filter = ("dataset", "programming_language", "statement_source")
    search_fields = ("name", "statement", "topic_name", "source_ref")
    readonly_fields = ("created_at", "updated_at", "statement_updated_at")
    list_per_page = 50
    show_facets = admin.ShowFacets.NEVER
    actions = ("fetch_statement_from_dl", "mark_verified")

    # Полная форма — только при редактировании: на добавлении технические
    # поля импорта (dataset/source_ref/источник условия) не показываем,
    # их заполняет save_model.
    fieldsets = (
        (None, {"fields": (
            "name", "programming_language", "topic_name", "notes",
        )}),
        ("DL", {
            "description": (
                "Скопируйте nid= (и cid=) из адреса задачи на dl.gsu.by. "
                "Условие можно вставить вручную или обновить из DL действием "
                "«Загрузить условие из DL» в списке групп."
            ),
            "fields": ("dl_node_id", "dl_course_id"),
        }),
        ("Условие", {"fields": ("statement", "statement_source", "statement_updated_at")}),
        ("Импорт", {"fields": ("dataset", "source_ref")}),
        ("Служебное", {"fields": ("created_at", "updated_at")}),
    )

    def get_fieldsets(self, request, obj=None):
        if obj is None:
            return (
                (None, {"fields": ("name", "programming_language", "topic_name")}),
                ("DL", {
                    "description": (
                        "Скопируйте nid= (и cid=) из адреса задачи на dl.gsu.by — "
                        "по ним обновляется условие. Необязательно для ручных кейсов."
                    ),
                    "fields": ("dl_node_id", "dl_course_id"),
                }),
                ("Условие", {"fields": ("statement", "notes")}),
            )
        return self.fieldsets

    def save_model(self, request, obj, form, change):
        if not (obj.source_ref or "").strip():
            # Ручная запись: отдельный «датасет», чтобы импорт её не искал.
            obj.dataset = "manual"
            obj.source_ref = _unique_source_ref(
                FindErrorCaseGroup, f"manual/{obj.name.strip()}"
            )
        if not (obj.statement_source or "").strip() and obj.statement.strip():
            obj.statement_source = "manual"
        previous_statement = (form.initial.get("statement") or "").strip()
        if obj.statement.strip() and obj.statement != previous_statement:
            obj.statement_updated_at = timezone.now()
        super().save_model(request, obj, form, change)

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(case_count=Count("cases"))

    @admin.display(description="Язык")
    def language_column(self, obj):
        return obj.programming_language.language_name if obj.programming_language_id else "—"

    @admin.display(description="DL")
    def node_column(self, obj):
        if not obj.dl_node_id:
            return "—"
        url = dl_task_url(obj.dl_node_id, obj.dl_course_id)
        return format_html('<a href="{}" target="_blank" rel="noopener">nid={}</a>', url, obj.dl_node_id)

    @admin.display(description="Кейсов", ordering="case_count")
    def cases_count(self, obj):
        count = getattr(obj, "case_count", None)
        if count is None:
            count = obj.cases.count()
        return count

    @admin.action(description="Загрузить условие из DL")
    def fetch_statement_from_dl(self, request, queryset):
        ok = failed = 0
        try:
            for group in queryset:
                success, message = _fetch_statement_for_group(group, request)
                if success:
                    ok += 1
                else:
                    failed += 1
                    self.message_user(request, message, level=messages.WARNING)
        except DLApiError as exc:
            self.message_user(request, str(exc), level=messages.ERROR)
            return
        if ok:
            self.message_user(
                request, f"Условие обновлено из DL: {ok}.", level=messages.SUCCESS,
            )
        if failed and not ok:
            self.message_user(
                request, "Ни одно условие не обновлено.", level=messages.WARNING,
            )

    @admin.action(description="Пометить программы проверенными")
    def mark_verified(self, request, queryset):
        updated = queryset.update(status=FindErrorCase.STATUS_VERIFIED)
        self.message_user(
            request, f"Помечено проверенными: {updated} программ.", level=messages.SUCCESS,
        )

    def has_delete_permission(self, request, obj=None):
        return is_staff_or_superuser(request.user)


class FindErrorCaseAdmin(FastChangelistAdminMixin, _StaffOnlyAdminMixin, admin.ModelAdmin):
    ACCESS_KEY = "page.prompt_lab"

    list_display = ("name", "group", "status", "codes_column", "updated_at")
    list_filter = ("status", "group__programming_language", "group", "labels__code")
    search_fields = ("name", "source_ref", "code", "group__name", "group__statement")
    autocomplete_fields = ("group",)
    readonly_fields = ("source_hash", "created_by", "created_at", "updated_at")
    inlines = [FindErrorCaseLabelInline]
    list_per_page = 50
    show_facets = admin.ShowFacets.NEVER
    actions = ("mark_ready", "mark_verified", "mark_draft", "fetch_statement_from_dl")

    # На добавлении — только то, что реально нужно разработчику: группа,
    # название, код и метки (inline). Ссылка в датасете и служебные поля
    # заполняются автоматически и видны при редактировании.
    fieldsets = (
        (None, {"fields": ("group", "name", "status", "notes")}),
        ("Код", {"fields": ("code",)}),
        ("Импорт", {"fields": ("source_ref",)}),
        ("Служебное", {"fields": ("source_hash", "created_by", "created_at", "updated_at")}),
    )

    def get_fieldsets(self, request, obj=None):
        if obj is None:
            return (
                (None, {
                    "description": (
                        "Выберите группу (задачу) и вставьте ошибочную программу. "
                        "Метки ошибок добавьте ниже — код из каталога и пояснение."
                    ),
                    "fields": ("group", "name", "status"),
                }),
                ("Код", {"fields": ("code",)}),
            )
        return self.fieldsets

    def get_queryset(self, request):
        return (
            super().get_queryset(request)
            .select_related("group")
            .prefetch_related("labels")
        )

    @admin.display(description="Метки")
    def codes_column(self, obj):
        return ", ".join(f"error_{label.code}" for label in obj.labels.all()) or "—"

    def save_model(self, request, obj, form, change):
        if not obj.created_by_id:
            obj.created_by = request.user
        if not (obj.source_ref or "").strip():
            obj.source_ref = _unique_source_ref(
                FindErrorCase,
                f"{obj.group.source_ref}/{obj.name.strip()}",
            )
        super().save_model(request, obj, form, change)

    @admin.action(description="Статус: готова")
    def mark_ready(self, request, queryset):
        updated = queryset.update(status=FindErrorCase.STATUS_READY)
        self.message_user(request, f"Готовы: {updated}.", level=messages.SUCCESS)

    @admin.action(description="Статус: проверена")
    def mark_verified(self, request, queryset):
        updated = queryset.update(status=FindErrorCase.STATUS_VERIFIED)
        self.message_user(request, f"Проверены: {updated}.", level=messages.SUCCESS)

    @admin.action(description="Статус: черновик")
    def mark_draft(self, request, queryset):
        updated = queryset.update(status=FindErrorCase.STATUS_DRAFT)
        self.message_user(request, f"Черновики: {updated}.", level=messages.SUCCESS)

    @admin.action(description="Загрузить условие группы из DL")
    def fetch_statement_from_dl(self, request, queryset):
        groups = FindErrorCaseGroup.objects.filter(
            pk__in=queryset.values_list("group_id", flat=True)
        )
        ok = 0
        try:
            for group in groups:
                success, message = _fetch_statement_for_group(group, request)
                if success:
                    ok += 1
                else:
                    self.message_user(request, message, level=messages.WARNING)
        except DLApiError as exc:
            self.message_user(request, str(exc), level=messages.ERROR)
            return
        if ok:
            self.message_user(
                request, f"Условие обновлено для групп: {ok}.", level=messages.SUCCESS,
            )

    def has_delete_permission(self, request, obj=None):
        return is_staff_or_superuser(request.user)
