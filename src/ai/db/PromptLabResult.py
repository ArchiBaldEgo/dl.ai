# src/ai/db/.py

from django.db import models

from ai.db.PromptLabRun import PromptLabRun
from ai.db.FindErrorCase import FindErrorCase

class PromptLabResult(models.Model):
    """Результат пары «модель × кейс» в прогоне лаборатории."""

    STATUS_OK = "ok"
    STATUS_ERROR = "error"
    STATUS_CHOICES = ((STATUS_OK, "OK"), (STATUS_ERROR, "Ошибка"))

    run = models.ForeignKey(
        PromptLabRun, on_delete=models.CASCADE, related_name="results",
        verbose_name="Прогон",
    )
    case = models.ForeignKey(
        FindErrorCase, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="lab_results", verbose_name="Кейс",
    )
    case_ref = models.CharField(max_length=512, blank=True, default="", verbose_name="Ссылка кейса (снимок)")
    case_name = models.CharField(max_length=255, blank=True, default="", verbose_name="Имя кейса (снимок)")
    group_name = models.CharField(max_length=255, blank=True, default="", verbose_name="Группа (снимок)")
    model_key = models.CharField(max_length=128, verbose_name="Модель")
    model_title = models.CharField(max_length=255, blank=True, default="", verbose_name="Модель (подпись)")
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_OK, verbose_name="Статус",
    )
    error_message = models.TextField(blank=True, default="", verbose_name="Ошибка")
    message = models.TextField(blank=True, default="", verbose_name="Отправленное сообщение")
    raw_response = models.TextField(blank=True, default="", verbose_name="Ответ модели")
    parsed_codes = models.JSONField(default=list, blank=True, verbose_name="Найденные коды")
    expected_codes = models.JSONField(default=list, blank=True, verbose_name="Ожидаемые коды")
    matched_codes = models.JSONField(default=list, blank=True, verbose_name="Совпавшие коды")
    missed_codes = models.JSONField(default=list, blank=True, verbose_name="Пропущенные коды")
    extra_codes = models.JSONField(default=list, blank=True, verbose_name="Лишние коды")
    exact_match = models.BooleanField(default=False, verbose_name="Точное совпадение")
    precision = models.FloatField(null=True, blank=True, verbose_name="Precision")
    recall = models.FloatField(null=True, blank=True, verbose_name="Recall")
    f1 = models.FloatField(null=True, blank=True, verbose_name="F1")
    duration_seconds = models.FloatField(default=0.0, verbose_name="Длительность, с")
    tokens = models.PositiveIntegerField(default=0, verbose_name="Токены")
    tokens_prompt = models.PositiveIntegerField(default=0, verbose_name="Токены промпта")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Создан")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Обновлён")

    class Meta:
        db_table = "ai_prompt_lab_result"
        verbose_name = "Результат лаборатории промптов"
        verbose_name_plural = "Результаты лаборатории промптов"
        ordering = ("case_ref", "model_key")
        constraints = [
            models.UniqueConstraint(
                fields=("run", "model_key", "case"), name="ai_lab_result_run_model_case_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["run", "model_key"], name="ai_lab_result_run_model_idx"),
            models.Index(fields=["run", "case"], name="ai_lab_result_run_case_idx"),
        ]

    def __str__(self):
        return f"{self.model_key} × {self.case_name or self.case_ref}"

