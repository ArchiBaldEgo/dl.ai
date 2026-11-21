"""Форма проверки решения на признаки использования ИИ."""

from django import forms
from django.http import HttpResponseForbidden, JsonResponse
from django.template.response import TemplateResponse
from django.views.decorators.http import require_http_methods

from ..services.pangram_client import PangramError, check_text
from .permissions import can_access_check_ai
from .site import ai_admin_site

MAX_INPUT_MIB = 1
MAX_INPUT_BYTES = MAX_INPUT_MIB * 1024 * 1024
MAX_INPUT_LABEL = f"{MAX_INPUT_MIB} МиБ"


class CheckAiForm(forms.Form):
    text = forms.CharField(
        label="Текст решения",
        required=False,
        strip=False,
        widget=forms.Textarea(attrs={
    "style": (
        "display: block; clear: both; width: 100%; box-sizing: border-box; "
        "height: 55vh; resize: vertical;"
    ),
}),
    )
    file = forms.FileField(
        label="Файл решения",
        required=False,
        help_text=f"Текстовый файл или исходный код в UTF-8, до {MAX_INPUT_LABEL}.",
    )

    def clean(self):
        data = super().clean()
        if self.errors:
            return data

        text = data["text"]
        upload = data["file"]
        if text.strip() and upload:
            raise forms.ValidationError("Выберите один способ: вставьте текст или загрузите файл.")
        if upload:
            if upload.size > MAX_INPUT_BYTES:
                raise forms.ValidationError(f"Максимальный размер файла — {MAX_INPUT_LABEL}.")
            try:
                # utf-8-sig убирает BOM, который бывает в текстовых файлах Windows.
                text = upload.read().decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise forms.ValidationError("Сохраните файл как текст в UTF-8.") from exc

        if not text.strip():
            raise forms.ValidationError("Введите текст или выберите непустой текстовый файл.")
        if "\x00" in text:
            raise forms.ValidationError("Для проверки нужен обычный текст или исходный код.")
        if len(text.encode("utf-8")) > MAX_INPUT_BYTES:
            raise forms.ValidationError(f"Максимальный размер текста — {MAX_INPUT_LABEL}.")
        data["text"] = text
        return data


@require_http_methods(["GET", "POST"])
def admin_check_ai_view(request):
    if not can_access_check_ai(request):
        return HttpResponseForbidden("Нет доступа к Check AI.")

    form = CheckAiForm()
    status = 200
    result = None
    error = None
    if request.method == "POST":
        form = CheckAiForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                result = check_text(form.cleaned_data["text"])
            except PangramError as exc:
                status, error = exc.status, str(exc)
        else:
            status = 400

        # Та же проверка доступна программе через POST с параметром format=json.
        if request.GET.get("format") == "json":
            if result is not None:
                return JsonResponse(result)
            if error:
                return JsonResponse({"error": error}, status=status)
            return JsonResponse({"errors": form.errors.get_json_data()}, status=status)

    return TemplateResponse(
        request,
        "admin/ai/check_ai.html",
        {
            **ai_admin_site.each_context(request),
            "title": "Check AI",
            "form": form,
            "result": result,
            "error": error,
        },
        status=status,
    )
