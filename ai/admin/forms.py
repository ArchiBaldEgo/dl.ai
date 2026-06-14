"""Admin forms for AI models."""

from django import forms

from ..models import ProgrammingLanguage, Prompt, SharedPrompt, Topic


class PromptForm(forms.ModelForm):
    programming_language = forms.ModelChoiceField(
        queryset=ProgrammingLanguage.objects.none(),
        required=False,
        label="Programming language",
    )

    class Meta:
        model = Prompt
        fields = '__all__'
        widgets = {
            'prompt_text': forms.Textarea(attrs={
                'rows': 25,
                'style': 'width: 95%; font-family: monospace; line-height: 1.4; white-space: pre-wrap;'
            }),
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
        js = ("admin/js/prompt_language_topic.js",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if "programming_language" not in self.fields or "topic" not in self.fields:
            return

        self.fields["programming_language"].queryset = ProgrammingLanguage.objects.order_by("language_name")
        self.fields["programming_language"].widget.attrs["data-topics-url"] = "/ai/api/topics/"

        selected_language_id = self._resolve_selected_language_id()
        self.fields["topic"].queryset = Topic.objects.none()
        if selected_language_id:
            self.fields["topic"].queryset = Topic.objects.filter(
                programming_language_id=selected_language_id
            ).order_by("topic_name")
        elif self.instance.pk and self.instance.topic_id:
            self.fields["topic"].queryset = Topic.objects.filter(pk=self.instance.topic_id)
        else:
            self.fields["topic"].widget.attrs["disabled"] = "disabled"

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
        return None

    def clean(self):
        cleaned_data = super().clean()
        if "programming_language" not in self.fields or "topic" not in self.fields:
            return cleaned_data

        topic = cleaned_data.get("topic")
        programming_language = cleaned_data.get("programming_language")

        if topic and not programming_language:
            self.add_error("programming_language", "Выберите язык программирования.")
        if topic and programming_language and topic.programming_language_id != programming_language.id:
            self.add_error("topic", "Тема не относится к выбранному языку программирования.")
        return cleaned_data


class SharedPromptForm(forms.ModelForm):
    """Форма для общих (shared) препромптов."""
    class Meta:
        model = SharedPrompt
        fields = '__all__'
        widgets = {
            'prompt_text': forms.Textarea(attrs={
                'rows': 25,
                'style': 'width: 95%; font-family: monospace; line-height: 1.4; white-space: pre-wrap;'
            }),
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
