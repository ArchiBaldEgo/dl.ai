"""Admin config for small AI app models."""

from django.contrib import admin

from ..models import AIAppSettings


@admin.register(AIAppSettings)
class AIAppSettingsAdmin(admin.ModelAdmin):
    list_display = ("is_enabled", "updated_at")

    def has_add_permission(self, request):
        if AIAppSettings.objects.exists():
            return False
        return super().has_add_permission(request)

    def has_delete_permission(self, request, obj=None):
        return False
