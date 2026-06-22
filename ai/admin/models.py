"""ModelAdmin classes for AI models."""

import csv

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin
from django.db.models import Q
from django.http import HttpResponse

from ..models import AIAppSettings, AIModelTokenBudget, ProgrammingLanguage, Prompt, SharedPrompt, Topic
from ..querysets import prompt_queryset_for_user
from .forms import PromptForm, SharedPromptForm
from .permissions import can_access_logs, is_prompt_developer_user, is_staff_or_superuser

User = get_user_model()


class TopicInline(admin.TabularInline):
    model = Topic
    extra = 1
    fk_name = 'programming_language'
    show_change_link = True


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
    list_display = ('topic_name', 'programming_language')
    list_filter = ('programming_language',)
    search_fields = ('topic_name', 'topic_name_ru', 'topic_name_en', 'topic_name_fr')
    raw_id_fields = ('programming_language',)
    fieldsets = (
        (None, {"fields": ("topic_name", "topic_name_ru", "topic_name_en", "topic_name_fr", "programming_language")}),
    )


class PromptUserIdFilter(admin.SimpleListFilter):
    title = "userId"
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
        'prompt_name',
        'programming_language_name',
        'topic',
        'owner_user_id',
        'owner_username',
        'short_prompt_text',
    )
    list_display_links = ('prompt_name',)
    list_filter = (PromptUserIdFilter, 'topic__programming_language', 'topic')
    list_per_page = 25
    search_fields = ('prompt_name', 'prompt_text', 'owner__username', '=owner__id')
    autocomplete_fields = ("owner", "editors")
    actions = ("export_prompts_csv",)
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
        main_fields = (
            "programming_language", "topic",
            "prompt_name", "prompt_name_ru", "prompt_name_en", "prompt_name_fr",
            "shared_prompt", "prompt_text_override",
            "prompt_text", "prompt_text_ru", "prompt_text_en", "prompt_text_fr",
        )
        if request.user.is_superuser:
            return (
                (None, {"fields": main_fields}),
                ("Access", {"fields": ("owner", "editors"), "classes": ("collapse",)}),
            )
        return ((None, {"fields": main_fields}),)

    def get_readonly_fields(self, request, obj=None):
        if is_staff_or_superuser(request.user):
            return ()
        if self._can_edit_prompt(request, obj):
            return ()
        return (
            "programming_language", "topic",
            "prompt_name", "prompt_name_ru", "prompt_name_en", "prompt_name_fr",
            "shared_prompt", "prompt_text_override",
            "prompt_text", "prompt_text_ru", "prompt_text_en", "prompt_text_fr",
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
                prompt.prompt_name or "",
                language,
                topic.topic_name if topic else "",
                prompt.owner_id or "",
                prompt.owner.username if prompt.owner else "",
                prompt.prompt_text,
            ])
        return response
    export_prompts_csv.short_description = "Export selected prompts to CSV"

    def programming_language_name(self, obj):
        if obj.topic and obj.topic.programming_language:
            return obj.topic.programming_language.language_name
        return "-"
    programming_language_name.short_description = "Language"
    programming_language_name.admin_order_field = "topic__programming_language__language_name"

    def programming_language(self, obj):
        # Display method for the read-only rendering of the declared
        # ``programming_language`` form field (see PromptForm). These two are
        # coupled — remove the declared field and this method breaks the
        # readonly/fieldset path with FieldError.
        return self.programming_language_name(obj)
    programming_language.short_description = "Programming language"

    def owner_user_id(self, obj):
        return obj.owner_id or "-"
    owner_user_id.short_description = "userId"
    owner_user_id.admin_order_field = "owner_id"

    def owner_username(self, obj):
        return obj.owner.username if obj.owner else "-"
    owner_username.short_description = "Owner"

    def short_prompt_text(self, obj):
        text = obj.prompt_text or ""
        return f"{text[:100]}..." if len(text) > 100 else text
    short_prompt_text.short_description = "Prompt Text"


class SharedPromptAdmin(admin.ModelAdmin):
    form = SharedPromptForm
    list_display = ('prompt_name', 'mode', 'language_list', 'updated_at', 'owner_username')
    list_display_links = ('prompt_name',)
    list_filter = ('mode', 'programming_languages')
    search_fields = ('prompt_name', 'prompt_text')
    autocomplete_fields = ('owner', 'editors')
    # 'editors' is rendered by autocomplete_fields above (autocomplete wins in
    # Django's formfield_for_manytomany), so only 'programming_languages' uses
    # the horizontal filter widget — listing 'editors' here was dead config.
    filter_horizontal = ('programming_languages',)

    def language_list(self, obj):
        langs = obj.programming_languages.all()
        return ", ".join([l.language_name for l in langs]) if langs else "Все языки"
    language_list.short_description = "Языки"

    def owner_username(self, obj):
        return obj.owner.username if obj.owner else "-"
    owner_username.short_description = "Owner"

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
                "prompt_name", "prompt_name_ru", "prompt_name_en", "prompt_name_fr",
                "mode",
                "prompt_text", "prompt_text_ru", "prompt_text_en", "prompt_text_fr",
                "programming_languages",
            )}),
            ("Доступ", {"fields": ("owner", "editors"), "classes": ("collapse",)}),
        )


class AIAppSettingsAdmin(_StaffOnlyAdminMixin, admin.ModelAdmin):
    list_display = ("is_enabled", "updated_at")

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


class RestrictedUserAdmin(_StaffOnlyAdminMixin, UserAdmin):
    """User management restricted to staff/superuser in the AI admin site."""


class AIModelTokenBudgetAdmin(_StaffOnlyAdminMixin, admin.ModelAdmin):
    list_display = ("label", "total_limit", "issued_at", "notes")
    list_display_links = ("label",)
    search_fields = ("label", "notes")
    fieldsets = (
        (None, {"fields": ("label", "total_limit", "issued_at", "notes")}),
    )
