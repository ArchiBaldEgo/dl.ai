"""Форма проверки решения на признаки использования ИИ."""

import codecs
import re
from time import monotonic

from django import forms
from django.db import transaction
from django.http import HttpResponseForbidden, JsonResponse
from django.template.response import TemplateResponse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from ..check_ai_results import is_suspicious_result
from ..constants import CHECK_AI_TITLE
from ..models import AICheckBatch, AICheckBatchFile
from ..services.pangram_client import PangramError, check_text
from .permissions import can_access_check_ai
from .site import ai_admin_site

MAX_INPUT_MIB = 1
MAX_INPUT_LABEL = f"{MAX_INPUT_MIB} МиБ"
SHORT_INPUT_WORDS = 50


def _decode_uploaded_text(content: bytes) -> str:
    if content.startswith(codecs.BOM_UTF8):
        return content.decode("utf-8-sig")
    # Метка UTF-32 LE начинается с тех же байтов, что и метка UTF-16 LE.
    if content.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        return content.decode("utf-32")
    if content.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return content.decode("utf-16")
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return content.decode("cp1251")


def check_name_field():
    return forms.CharField(
        label="Название проверки",
        required=False,
        max_length=AICheckBatch.NAME_MAX_LENGTH,
        widget=forms.TextInput(attrs={"placeholder": "Необязательно"}),
        error_messages={
            "max_length": f"Название проверки должно быть не длиннее {AICheckBatch.NAME_MAX_LENGTH} символов.",
            "null_characters_not_allowed": "Название содержит недопустимый нулевой символ.",
        },
    )


class CheckAiForm(forms.Form):
    max_input_mib = MAX_INPUT_MIB

    name = check_name_field()
    text = forms.CharField(
        label="Текст решения",
        required=False,
        strip=False,
        widget=forms.Textarea(attrs={"rows": 6}),
        error_messages={
            "null_characters_not_allowed": "Текст содержит недопустимый нулевой символ.",
        },
    )
    file = forms.FileField(
        label="Файл решения",
        required=False,
        help_text=(
            f"Текстовый файл или исходный код до {MAX_INPUT_LABEL}. "
            "Поддерживаются UTF-8, Windows-1251, а также UTF-16 и UTF-32 с меткой кодировки."
        ),
        error_messages={
            "empty": "Загруженный файл пуст.",
            "invalid": "Не удалось принять файл. Выберите файл заново.",
            "missing": "Файл не удалось получить. Выберите его заново.",
            "contradiction": "Выберите один файл для проверки.",
            "max_length": "Имя файла слишком длинное.",
        },
    )

    def clean(self):
        data = super().clean()
        if self.errors:
            return data

        text = data["text"]
        upload = data["file"]
        if text.strip() and upload:
            raise forms.ValidationError("Выберите один способ: вставьте текст или загрузите файл.")
        max_bytes = self.max_input_mib * 1024 * 1024
        if upload:
            if upload.size > max_bytes:
                raise forms.ValidationError(f"Максимальный размер файла — {self.max_input_mib} МиБ.")
            try:
                text = _decode_uploaded_text(upload.read())
            except UnicodeDecodeError as exc:
                raise forms.ValidationError(
                    "Не удалось прочитать кодировку файла. Пересохраните его в UTF-8."
                ) from exc

        if not text.strip():
            raise forms.ValidationError("Введите текст или выберите непустой текстовый файл.")
        # Бинарный файл может декодироваться как Windows-1251, но содержит управляющие символы.
        has_control_characters = upload and re.search(r"[\x01-\x08\x0e-\x1f\x7f-\x9f]", text)
        if "\x00" in text or has_control_characters:
            raise forms.ValidationError("Для проверки нужен обычный текст или исходный код.")
        if len(text.encode("utf-8")) > max_bytes:
            raise forms.ValidationError(f"Максимальный размер текста — {self.max_input_mib} МиБ.")
        data["text"] = text
        return data


def _record_single_check(user, data, started_at, duration_seconds, result, error):
    status = AICheckBatchFile.Status.DONE if result is not None else AICheckBatchFile.Status.ERROR
    with transaction.atomic():
        check = AICheckBatch.objects.create(
            kind=AICheckBatch.Kind.SINGLE,
            created_by=user,
            created_at=started_at,
            checker_name=user.get_full_name() or user.get_username(),
            name=data["name"],
        )
        check.files.create(
            relative_path=data["file"].name if data["file"] else "Текст решения",
            status=status,
            result=result or {},
            source_text=data["text"] if result and is_suspicious_result(result) else "",
            error=error or "",
            checked_at=timezone.now(),
            duration_seconds=duration_seconds,
        )


@require_http_methods(["GET", "POST"])
def admin_check_ai_view(request):
    if not can_access_check_ai(request):
        return HttpResponseForbidden("У вас нет доступа к проверке на ИИ.")

    form = CheckAiForm()
    status = 200
    result = None
    error = None
    if request.method == "POST":
        form = CheckAiForm(request.POST, request.FILES)
        if form.is_valid():
            text = form.cleaned_data["text"]
            started_at = timezone.now()
            started = monotonic()
            try:
                result = check_text(text)
                result["short_input"] = len(text.split()) < SHORT_INPUT_WORDS
            except PangramError as exc:
                status, error = exc.status, str(exc)
            _record_single_check(request.user, form.cleaned_data, started_at, monotonic() - started, result, error)
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
            "title": CHECK_AI_TITLE,
            "form": form,
            "result": result,
            "error": error,
        },
        status=status,
    )
