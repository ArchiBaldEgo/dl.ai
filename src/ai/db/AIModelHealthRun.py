# src/ai/db/AIModelHealthRun.py

from django.db import models
from django.utils import timezone

class AIModelHealthRun(models.Model):
    """Один прогон проверки доступности всех AI-моделей за конкретную дату.

    Запускается планировщиком в ai/model_health.py (ежедневно в 04:00 МСК).
    Содержит сводный статус и временные метки начала/окончания.
    """
    STATUS_RUNNING = "running"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"

    STATUS_CHOICES = (
        (STATUS_RUNNING, "Running"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_FAILED, "Failed"),
    )

    window_date = models.DateField(unique=True, verbose_name="Окно проверки")
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_RUNNING, verbose_name="Статус")
    started_at = models.DateTimeField(default=timezone.now, verbose_name="Начат")
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name="Завершён")
    error_message = models.TextField(blank=True, default="", verbose_name="Текст ошибки")

    class Meta:
        verbose_name = "Прогон проверки моделей"
        verbose_name_plural = "Прогоны проверки моделей"
        ordering = ("-window_date",)

    def __str__(self):
        return f"{self.window_date} ({self.status})"
