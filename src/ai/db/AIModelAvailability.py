# src/ai/db/AIModelAvailability.py

from django.db import models

class AIModelAvailability(models.Model):
    """Запись о доступности конкретной AI-модели за определённую дату.

    Создаётся в рамках AIModelHealthRun. Хранит флаг доступности, время отклика,
    HTTP-код последней проверки и сообщение об ошибке (если есть).
    Одна запись на (model_key, window_date).
    """
    model_key = models.CharField(max_length=128, db_index=True, verbose_name="Ключ модели")
    model_title = models.CharField(max_length=255, verbose_name="Модель")
    is_available = models.BooleanField(default=False, verbose_name="Доступна")
    window_date = models.DateField(db_index=True, verbose_name="Окно проверки")
    checked_at = models.DateTimeField(auto_now=True, verbose_name="Проверена")
    response_time_ms = models.PositiveIntegerField(null=True, blank=True, verbose_name="Время ответа, мс")
    last_http_code = models.PositiveSmallIntegerField(null=True, blank=True, verbose_name="Код ответа")
    last_message = models.TextField(blank=True, default="", verbose_name="Сообщение")

    class Meta:
        verbose_name = "Доступность модели"
        verbose_name_plural = "Доступность моделей"
        ordering = ("model_title",)
        constraints = [
            models.UniqueConstraint(
                fields=("model_key", "window_date"),
                name="ai_model_availability_key_window_uniq",
            )
        ]

    def __str__(self):
        return f"{self.model_key}: {'up' if self.is_available else 'down'}"
