"""Формы для админки AI-приложения.

PromptForm: форма для редактирования промпта с каскадным выбором
язык программирования → тема (через prompt_language_topic.js).
SharedPromptForm: форма для общих промптов.
ExternalDLAccountAdminForm: карточка DL-аккаунта + право раздавать группы
доступа связанному локальному пользователю (поле гейтится в админке).
"""

from django import forms
from django.contrib.admin.widgets import FilteredSelectMultiple

from ..models import AIAccessGroup, ExternalDLAccount, ProgrammingLanguage, Prompt, SharedPrompt, Topic


class TopicForm(forms.ModelForm):
    """Форма темы: табы RU/EN/FR через общий prompt_translate_tabs.js."""

    class Meta:
        model = Topic
        fields = '__all__'
        widgets = {}

    class Media:
        js = ("admin/js/prompt_translate_tabs.js",)


class PromptForm(forms.ModelForm):
    # ``programming_language`` — FK-поле модели Prompt (миграция 0048). Явная
    # декларация нужна для упорядоченного queryset и атрибута data-topics-url
    # для каскада «язык → темы» (prompt_language_topic.js). Язык может быть
    # задан БЕЗ темы («общий» промпт на весь язык); если заданы оба — тема
    # обязана принадлежать языку (clean). Раньше поле было виртуальным
    # (язык выводился только из темы) — и не сохранялось.
    programming_language = forms.ModelChoiceField(
        queryset=ProgrammingLanguage.objects.none(),
        required=False,
        label="Язык программирования",
    )

    class Meta:
        model = Prompt
        fields = '__all__'
        widgets = {
            'mode': forms.RadioSelect,
            'prompt_text_ru': forms.Textarea(attrs={
                'rows': 25,
                'style': 'width: 95%; font-family: monospace; line-height: 1.4; white-space: pre-wrap;'
            }),
            'prompt_text_en': forms.Textarea(attrs={
                'rows': 25,
                'style': 'width: 95%; font-family: monospace; line-height: 1.4; white-space: pre-wrap;'
            }),
            'prompt_text_fr': forms.Textarea(attrs={
                'rows': 25,
                'style': 'width: 95%; font-family: monospace; line-height: 1.4; white-space: pre-wrap;'
            }),
            'prompt_text_override': forms.Textarea(attrs={
                'rows': 25,
                'style': 'width: 95%; font-family: monospace; line-height: 1.4; white-space: pre-wrap;'
            }),
        }

    class Media:
        js = ("admin/js/prompt_language_topic.js", "admin/js/prompt_translate_tabs.js",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if "programming_language" not in self.fields or "topic" not in self.fields:
            return

        self.fields["programming_language"].queryset = ProgrammingLanguage.objects.order_by("language_name")
        self.fields["programming_language"].widget.attrs["data-topics-url"] = "/ai/api/topics/"

        selected_language_id = self._resolve_selected_language_id()
        if selected_language_id:
            self.fields["topic"].queryset = Topic.objects.filter(
                programming_language_id=selected_language_id
            ).order_by("topic_name_ru")
        elif self.instance.pk and self.instance.topic_id:
            self.fields["topic"].queryset = Topic.objects.filter(pk=self.instance.topic_id)
        else:
            # No language chosen yet. Offer every topic (not none()+disabled)
            # so the field stays usable without JS: prompt_language_topic.js
            # filters client-side on language change, and clean() validates
            # topic<->language consistency server-side. Hard-disabling here
            # made topic assignment impossible if the JS failed to load.
            self.fields["topic"].queryset = Topic.objects.all().order_by("topic_name_ru")

        if not self.is_bound and selected_language_id:
            self.fields["programming_language"].initial = selected_language_id

    def _resolve_selected_language_id(self):
        if self.is_bound:
            language_id = self.data.get(self.add_prefix("programming_language"))
            if language_id:
                return language_id

            topic_id = self.data.get(self.add_prefix("topic"))
            if topic_id:
                return Topic.objects.filter(pk=topic_id).values_list("programming_language_id", flat=True).first()
            return None

        if self.instance.pk and self.instance.topic_id:
            return self.instance.topic.programming_language_id
        # Темы нет — берём собственный язык промпта (промпт «на весь язык»).
        return self.instance.programming_language_id

    def clean(self):
        cleaned_data = super().clean()
        if "programming_language" not in self.fields or "topic" not in self.fields:
            return cleaned_data

        topic = cleaned_data.get("topic")
        programming_language = cleaned_data.get("programming_language")

        # A topic always belongs to exactly one programming language, so if
        # the user picked a topic but left the language <select> empty (e.g.
        # the cascade JS did not load / did not set it), infer the language
        # from the topic instead of rejecting a semantically valid submission.
        if topic and not programming_language:
            if topic.programming_language_id:
                cleaned_data["programming_language"] = topic.programming_language
                programming_language = topic.programming_language
            else:
                self.add_error("programming_language", "Выберите язык программирования.")
        if topic and programming_language and topic.programming_language_id != programming_language.id:
            self.add_error("topic", "Тема не относится к выбранному языку программирования.")
        return cleaned_data


class SharedPromptForm(forms.ModelForm):
    """Форма для общих (shared) промптов."""
    class Media:
        js = ("admin/js/prompt_translate_tabs.js",)

    class Meta:
        model = SharedPrompt
        fields = '__all__'
        widgets = {
            'prompt_text_ru': forms.Textarea(attrs={
                'rows': 25,
                'style': 'width: 95%; font-family: monospace; line-height: 1.4; white-space: pre-wrap;'
            }),
            'prompt_text_en': forms.Textarea(attrs={
                'rows': 25,
                'style': 'width: 95%; font-family: monospace; line-height: 1.4; white-space: pre-wrap;'
            }),
            'prompt_text_fr': forms.Textarea(attrs={
                'rows': 25,
                'style': 'width: 95%; font-family: monospace; line-height: 1.4; white-space: pre-wrap;'
            }),
        }


class ExternalDLAccountAdminForm(forms.ModelForm):
    """Карточка DL-аккаунта: право раздавать группы доступа связанному юзеру.

    Поле ВИРТУАЛЬНОЕ (M2M живёт на локальном User, не на аккаунте):
    queryset — «Группы доступа», initial — группы account.user; персистят
    его в ExternalDLAccountAdmin.save_model (ModelForm.save() посторонние
    M2M не пишет). Gating поля — get_form админки (can_edit_access_groups),
    здесь его нет: форма всегда честная (initial от текущего состояния).
    """

    user_ai_access_groups = forms.ModelMultipleChoiceField(
        queryset=AIAccessGroup.objects.all(),
        required=False,
        widget=FilteredSelectMultiple("Группы доступа", is_stacked=False),
        label="Группы доступа пользователя",
        help_text="Права админки локального пользователя выдаются только "
                  "этими группами (не стоковыми «Группами» Django).",
    )

    class Meta:
        model = ExternalDLAccount
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # raw_id у user: bound-форме доверяем присланному значению, unbound —
        # instance.user (add-страница с пустым raw_id даст None — поле потом
        # всё равно уберётся в get_form админки пока юзер не выбран).
        if self.instance.user_id and not self.is_bound:
            self.fields["user_ai_access_groups"].initial = (
                self.instance.user.ai_access_groups.all()
            )
