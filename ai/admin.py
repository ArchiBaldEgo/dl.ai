from django.contrib import admin
from django import forms
from .models import ProgrammingLanguage, Topic, Prompt

# Форма для Prompt с улучшенным Textarea
class PromptForm(forms.ModelForm):
    class Meta:
        model = Prompt
        widgets = {
            'prompt_text': forms.Textarea(attrs={
                'rows': 25,
                'style': 'width: 95%; font-family: monospace; line-height: 1.4; white-space: pre-wrap;'
            }),
        }
        fields = '__all__'

# Inline для Prompt
class PromptInline(admin.TabularInline):
    model = Prompt
    form = PromptForm
    extra = 1
    fields = ('prompt_name',)  # Показываем только нужные поля
    classes = ('collapse',)  # Делаем сворачиваемым

# Inline для Topic (исправлено: было "ininlines")
class TopicInline(admin.TabularInline):
    model = Topic
    extra = 1
    fk_name = 'programming_language'
    show_change_link = True  # Добавляем ссылку на изменение

class ProgrammingLanguageAdmin(admin.ModelAdmin):
    inlines = [TopicInline]
    list_display = ('language_name',)
    search_fields = ('language_name',)

class TopicAdmin(admin.ModelAdmin):
    inlines = [PromptInline]
    list_display = ('topic_name', 'programming_language')
    list_filter = ('programming_language',)
    search_fields = ('topic_name',)
    raw_id_fields = ('programming_language',)  # Для удобства при многих языках

class PromptAdmin(admin.ModelAdmin):
    form = PromptForm
    list_display = ('prompt_name', 'topic', 'short_prompt_text')
    list_filter = ('topic__programming_language', 'topic')
    search_fields = ('prompt_name', 'prompt_text')
    
    def short_prompt_text(self, obj):
        return f"{obj.prompt_text[:100]}..." if len(obj.prompt_text) > 100 else obj.prompt_text
    short_prompt_text.short_description = "Prompt Text"

# Регистрация
admin.site.register(ProgrammingLanguage, ProgrammingLanguageAdmin)
admin.site.register(Topic, TopicAdmin)
admin.site.register(Prompt, PromptAdmin)
