# src/ai/db/.py

from django.conf import settings
from django.db import models
from django.utils import timezone

from ai.db.AIRequestLog import AIRequestLog

class AIPinnedBatchRun(models.Model):
    """Закреплённый завершённый пакетный прогон (персонально для пользователя).

    Хранит пару (пользователь, запись журнала batch-solve) — ссылку на
    завершённый прогон «Пакетного решения» (source=arm), закреплённую кнопкой
    в журнале запросов или на «Настройке ИИ-приложения». Список закреплённых
    показывается на странице «Закреплённые пакетные решения» (пункт навигации
    под «Настройкой ИИ-приложения»); результаты прогона — та же ленивая
    развёртка (batch-snapshot), что в журнале.
    """
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="pinned_batch_runs",
        verbose_name="Кто закрепил",
    )
    log = models.ForeignKey(
        AIRequestLog,
        on_delete=models.CASCADE,
        related_name="pinned_by",
        verbose_name="Запись журнала (прогон)",
    )
    created_at = models.DateTimeField(default=timezone.now, verbose_name="Закреплён")

    class Meta:
        verbose_name = "Закреплённый пакетный прогон"
        verbose_name_plural = "Закреплённые пакетные прогоны"
        ordering = ("-created_at", "-id")
        constraints = [
            models.UniqueConstraint(
                fields=("user", "log"),
                name="ai_pinned_batch_run_user_log_uniq",
            )
        ]

    def __str__(self):
        return f"{self.user_id} → log {self.log_id}"
