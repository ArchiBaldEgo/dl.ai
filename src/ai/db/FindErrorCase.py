# src/ai/db/FindErrorCase.py

from django.db import models
from django.conf import settings

from ai.db.FindErrorCaseGroup import FindErrorCaseGroup

class FindErrorCase(models.Model):
    """Одна ошибочная программа с размеченными ошибками (метки — FK-строки)."""

    STATUS_DRAFT = "draft"
    STATUS_READY = "ready"
    STATUS_VERIFIED = "verified"
    STATUS_DISABLED = "disabled"
    STATUS_CHOICES = (
        (STATUS_DRAFT, "Черновик"),
        (STATUS_READY, "Готова"),
        (STATUS_VERIFIED, "Проверена"),
        (STATUS_DISABLED, "Отключена"),
    )

    group = models.ForeignKey(
        FindErrorCaseGroup, on_delete=models.CASCADE, related_name="cases",
        verbose_name="Группа",
    )
    name = models.CharField(max_length=255, verbose_name="Название программы")
    source_ref = models.CharField(
        max_length=512, unique=True, verbose_name="Ссылка в датасете",
        help_text="Например: odnomerny_massiv/1. Отчисление/1",
    )
    code = models.TextField(verbose_name="Код программы")
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_READY,
        db_index=True, verbose_name="Статус",
    )
    source_hash = models.CharField(
        max_length=64, blank=True, default="", db_index=True,
        verbose_name="Хэш исходников",
        help_text="Считается импортом из кода и меток; ручная правка его не меняет.",
    )
    notes = models.TextField(blank=True, default="", verbose_name="Примечания")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="find_error_cases", verbose_name="Создал",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Создана")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Обновлена")

    class Meta:
        db_table = "ai_find_error_case"
        verbose_name = "Ошибочная программа"
        verbose_name_plural = "Ошибочные программы"
        ordering = ("group", "name")
        indexes = [
            models.Index(fields=["group", "status"], name="ai_fe_case_group_status_idx"),
            models.Index(fields=["status"], name="ai_fe_case_status_idx"),
        ]

    def __str__(self):
        return f"{self.group.name} / {self.name}" if self.group_id else self.name
