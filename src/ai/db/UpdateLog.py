# src/ai/db/.py

from django.contrib.postgres.indexes import GinIndex, OpClass
from django.db import models
from django.db.models.functions import Upper

class UpdateLog(models.Model):
    """Журнал обновлений проекта.

    Записи добавляются перед git add/git push. Каждая запись — описание
    изменений на русском языке, дата и автор коммита.
    """
    commit_date = models.DateField(verbose_name="Дата")
    description = models.TextField(verbose_name="Содержание обновления")
    author = models.CharField(max_length=255, verbose_name="Автор")
    commit_hash = models.CharField(max_length=40, blank=True, default="", verbose_name="Хэш коммита")
    # Скрытые строки — коммиты, отклонённые при интерактивной синхронизации
    # (sync_update_log --interactive). Не видны на «Обновлениях», видны только
    # в UpdateLogAdmin — там выбираются публикацией/скрытием.
    hidden = models.BooleanField(default=False, verbose_name="Скрыто")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Создан")

    class Meta:
        db_table = "ai_update_log"
        verbose_name = "Обновление"
        verbose_name_plural = "Обновления"
        ordering = ("-commit_date", "-created_at", "-id")
        indexes = [
            # Список «Обновлений» всегда отсортирован по Meta.ordering и
            # фильтруется по hidden — один составной индекс вместо сортировки
            # всей таблицы на каждой загрузке.
            models.Index(
                fields=["hidden", "commit_date", "created_at", "id"],
                name="ai_update_log_order_idx",
            ),
            # Поиск по описанию/автору (icontains) — триграммы, см. 0079.
            GinIndex(
                OpClass(Upper("description"), "gin_trgm_ops"),
                name="ai_update_desc_trgm_idx",
            ),
            GinIndex(
                OpClass(Upper("author"), "gin_trgm_ops"),
                name="ai_update_author_trgm_idx",
            ),
        ]

    def __str__(self):
        return f"{self.commit_date} — {self.author} — {self.description[:80]}"
