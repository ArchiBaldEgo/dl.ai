# src/ai/db/PromptLabRun.py

from django.conf import settings
from django.db import models

from ai.db.Prompt import Prompt

class PromptLabRun(models.Model):
    """Прогон лаборатории промптов: промпт × модели × набор кейсов.

    Отдельная от ``AIModelTestRun`` сущность: «Отладка промтов» правит промпт
    по провалам DL, а лаборатория измеряет промпт на размеченной базе
    ошибочных программ. Смешивать статистику нельзя.
    """

    STATUS_RUNNING = "running"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = (
        (STATUS_RUNNING, "Выполняется"),
        (STATUS_COMPLETED, "Завершён"),
        (STATUS_FAILED, "Ошибка"),
        (STATUS_CANCELLED, "Прерван"),
    )

    run_id = models.CharField(max_length=32, unique=True, db_index=True, verbose_name="ID прогона")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="prompt_lab_runs", verbose_name="Запустил",
    )
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_RUNNING,
        db_index=True, verbose_name="Статус",
    )
    prompt = models.ForeignKey(
        Prompt, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="lab_runs", verbose_name="Промпт",
    )
    prompt_id_snapshot = models.PositiveBigIntegerField(
        null=True, blank=True, verbose_name="ID промпта (снимок)",
    )
    prompt_name = models.CharField(max_length=255, blank=True, default="", verbose_name="Имя промпта (снимок)")
    prompt_text = models.TextField(blank=True, default="", verbose_name="Текст промпта (снимок)")
    base_prompt_text = models.TextField(blank=True, default="", verbose_name="Общий промпт режима (снимок)")
    model_keys = models.JSONField(default=list, verbose_name="Модели (ключи)")
    model_titles = models.JSONField(default=list, verbose_name="Модели (подписи)")
    case_ids = models.JSONField(default=list, verbose_name="Кейсы (id)")
    case_count = models.PositiveIntegerField(default=0, verbose_name="Кейсов")
    ui_language = models.CharField(max_length=8, default="ru", verbose_name="Язык интерфейса")
    include_eval_suffix = models.BooleanField(default=True, verbose_name="Добавлять строку КОДЫ")
    started_at = models.DateTimeField(auto_now_add=True, verbose_name="Начат")
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name="Завершён")
    error_message = models.TextField(blank=True, default="", verbose_name="Ошибка")
    report = models.JSONField(default=dict, blank=True, verbose_name="Отчёт")
    run_params = models.JSONField(default=dict, blank=True, verbose_name="Параметры запуска")

    class Meta:
        db_table = "ai_prompt_lab_run"
        verbose_name = "Прогон лаборатории промптов"
        verbose_name_plural = "Прогоны лаборатории промптов"
        ordering = ("-started_at",)
        indexes = [
            models.Index(fields=["status", "started_at"], name="ai_lab_run_status_started_idx"),
        ]

    def __str__(self):
        return f"{self.run_id} ({self.get_status_display()})"
