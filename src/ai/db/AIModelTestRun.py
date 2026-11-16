# src/ai/db/AIModelTestRun.py

from django.conf import settings
from django.db import models
from django.utils import timezone

class AIModelTestRun(models.Model):
    """A persisted ARM multi-model run.

    The in-memory job dict in ``ai/arm_runner.py`` is still used for live
    progress, but this model is the source of truth for completed runs and
    powers the per-model / per-topic summary tables. ``run_type`` distinguishes
    the single-prompt find-error runner (``single``) from the batch-over-tasks
    solver (``batch``); the latter keeps one run for many (task, model) pairs and
    stores the per-task topic/language snapshot on each ``AIModelTestResult``.
    ``run_params`` — снимок параметров формы на момент запуска (batch: node_ids,
    model_keys, file_extension, prompt_id, dl_test, ui_language, course_id;
    single: model_keys, interface_language, programming_language, topic, prompt,
    task_text, code_text) — для восстановления состояния формы при возврате
    на страницу прогона (``?run_id=``). ``paused`` — кооперативная пауза
    batch-прогона (вступает на границе пар); осиротевший paused-прогон
    (нет живого job) помечается «Процесс потерян», как и running.
    """

    STATUS_RUNNING = "running"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"
    STATUS_CANCELLED = "cancelled"
    STATUS_PAUSED = "paused"

    STATUS_CHOICES = (
        (STATUS_RUNNING, "Running"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_FAILED, "Failed"),
        (STATUS_CANCELLED, "Cancelled"),
        (STATUS_PAUSED, "Paused"),
    )

    RUN_TYPE_SINGLE = "single"
    RUN_TYPE_BATCH = "batch"
    RUN_TYPE_PROMPT_DEBUG = "prompt_debug"

    RUN_TYPE_CHOICES = (
        (RUN_TYPE_SINGLE, "Single (find-error)"),
        (RUN_TYPE_BATCH, "Batch (solve)"),
        (RUN_TYPE_PROMPT_DEBUG, "Prompt debug"),
    )

    run_id = models.CharField(max_length=64, unique=True, db_index=True, verbose_name="ID прогона")
    run_type = models.CharField(
        max_length=16, choices=RUN_TYPE_CHOICES, default=RUN_TYPE_SINGLE, db_index=True,
        verbose_name="Тип прогона",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="ai_model_test_runs",
        verbose_name="Пользователь",
    )
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_RUNNING, verbose_name="Статус")
    started_at = models.DateTimeField(default=timezone.now, verbose_name="Начат")
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name="Завершён")
    message = models.TextField(blank=True, default="", verbose_name="Сообщение")
    error_message = models.TextField(blank=True, default="", verbose_name="Текст ошибки")
    report = models.JSONField(default=dict, blank=True, verbose_name="Отчёт")
    total_models = models.PositiveSmallIntegerField(default=0, verbose_name="Всего моделей")
    # Context selected by the user (mirrors AIRequestLog context fields).
    programming_language_id = models.IntegerField(null=True, blank=True, verbose_name="ID языка")
    programming_language_name = models.CharField(max_length=255, blank=True, default="", verbose_name="Язык программирования")
    topic_id = models.IntegerField(null=True, blank=True, verbose_name="ID темы")
    topic_name = models.CharField(max_length=255, blank=True, default="", verbose_name="Тема")
    prompt_id = models.IntegerField(null=True, blank=True, verbose_name="ID промпта")
    prompt_name = models.CharField(max_length=255, blank=True, default="", verbose_name="Промпт")
    # ID курса DL, по которому загружалось дерево задач для batch-прогона.
    # Сохраняется для отображения в Журнале запросов (режим «Пакетное решение»,
    # поле «id дерева») и для проброса в send-solution как courseId.
    course_id = models.IntegerField(null=True, blank=True, verbose_name="ID курса DL")
    # Снимок параметров формы на момент запуска — для восстановления состояния
    # формы при возврате на страницу прогона (?run_id=). См. докстринг модели.
    run_params = models.JSONField(default=dict, blank=True, verbose_name="Параметры запуска (форма)")
    # Ручное название прогона, задаваемое при запуске на /arm/solve/. Хранится
    # на самом прогоне — журнал и таблица «Последние пакетные решения» читают
    # его без дополнительных связок.
    run_name = models.CharField(max_length=255, blank=True, default="", verbose_name="Название прогона")

    class Meta:
        db_table = "ai_ai_model_test_run"
        verbose_name = "Прогон тестирования модели"
        verbose_name_plural = "Прогоны тестирования модели"
        ordering = ("-started_at",)
        indexes = [
            # Список прогонов всегда сортируется по -started_at; фильтр по
            # статусу («идущие/завершённые») — частый.
            models.Index(fields=["started_at"], name="ai_model_test_run_started_idx"),
            models.Index(fields=["status", "started_at"], name="ai_model_test_run_status_idx"),
            # Выборки «только batch-прогоны» (run_type) с сортировкой по дате.
            models.Index(fields=["run_type", "started_at"], name="ai_model_test_run_type_idx"),
        ]

    def __str__(self):
        return f"{self.run_id} ({self.status})"

