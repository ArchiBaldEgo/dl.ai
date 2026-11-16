# src/ai/db/AIModelStats.py

from django.db import models

from ai.db.ProgrammingLanguage import ProgrammingLanguage
from ai.db.Topic import Topic

class AIModelStats(models.Model):
    """Накопленная статистика (model_key, язык, тема) по прогонам /arm/solve/.

    Обновляется только по завершении пакетного прогона, запущенного с
    включённым чекбоксом «заносить результаты в БД» (доступен суперюзерам).
    Semantics совпадают с ``arm_runner._per_bucket``: в ``total_count``
    идут только решённые/проваленные пары, длительности — только не-skipped.

    Скоупы (2026-10): строка (модель, язык, тема) — пары этой темы; строка
    (модель, язык, NULL) — языковая строка («выбран только язык»); пара
    инкрементирует ОБЕ строки (без темы — только языковую). Строки с
    (NULL, NULL) — легаси-глобальный скоуп по model_key, новые приращения
    в него не пишутся, UI его не читает (оператор чистит reset_model_stats).
    """

    model_key = models.CharField(max_length=128, db_index=True, verbose_name="Ключ модели")
    model_title = models.CharField(max_length=255, blank=True, default="", verbose_name="Название модели")
    solved_count = models.PositiveIntegerField(default=0, verbose_name="Решено задач")
    total_count = models.PositiveIntegerField(default=0, verbose_name="Всего задач (решено+провалено)")
    duration_count = models.PositiveIntegerField(default=0, verbose_name="Измерений времени")
    duration_sum = models.FloatField(default=0.0, verbose_name="Сумма времени, сек")
    programming_language = models.ForeignKey(
        ProgrammingLanguage, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="model_stats", verbose_name="Язык программирования",
    )
    topic = models.ForeignKey(
        Topic, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="model_stats", verbose_name="Тема",
    )
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Обновлено")

    class Meta:
        db_table = "ai_ai_model_stats"
        verbose_name = "Статистика модели"
        verbose_name_plural = "Статистика моделей"
        ordering = ("-solved_count",)
        indexes = [
            # Скоуп-ключ чтения (model_key, язык, тема). Уникальность НЕ
            # навязана бд-констрейнтом: prod-PG 14 не умеет NULLS NOT DISTINCT
            # (нужен PG15+), а «двойная строка язык-скоупа» невозможна в
            # одиночном воркере финализации — честный upsert в model_stats.
            models.Index(
                fields=["model_key", "programming_language", "topic"],
                name="ai_model_stats_scope_idx",
            ),
            # Список статистики сортируется по -solved_count (Meta.ordering).
            models.Index(
                fields=["solved_count"],
                name="ai_model_stats_solved_idx",
            ),
        ]

    def __str__(self):
        return self.model_key

