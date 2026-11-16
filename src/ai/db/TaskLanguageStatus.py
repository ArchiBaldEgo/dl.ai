# src/ai/db/TaskLanguageStatus.py

from django.db import models

class TaskLanguageStatus(models.Model):
    """Флаг решённости задачи курса DL для конкретного языка (дерево ARM).

    Заполняется отдельным прогоном по DL ``POST /restapi/get-solutions``
    (см. ``ai/services/task_language_status.py``): агрегируются все посылки,
    доступные DLSID-сессии (админ DL видит всех пользователей курса).
    Дерево читает ТОЛЬКО эти флаги — запросов к DL при отрисовке/смене языка нет.

    Статусы:
      * 0 — посылок не было (или флаг ещё ни разу не заполнялся) — серый;
      * 1 — есть хотя бы одно решение с ``isCorrect=true`` — зелёный;
      * 2 — посылки были, но верных нет — красный.
    """

    STATUS_NOT_ATTEMPTED = 0
    STATUS_SOLVED = 1
    STATUS_ATTEMPTED = 2

    STATUS_CHOICES = (
        (STATUS_NOT_ATTEMPTED, "Не решалась"),
        (STATUS_SOLVED, "Решена"),
        (STATUS_ATTEMPTED, "Были попытки, не решена"),
    )

    course_id = models.IntegerField(db_index=True, verbose_name="ID курса DL")
    node_id = models.PositiveIntegerField(db_index=True, verbose_name="ID задачи DL")
    programming_language_id = models.IntegerField(
        db_index=True, verbose_name="ID языка программирования",
    )
    # Расширение на момент прогона: /get-solutions фильтрует именно по нему.
    file_extension = models.CharField(
        max_length=16, blank=True, default="", verbose_name="Расширение файла",
    )
    status = models.PositiveSmallIntegerField(
        choices=STATUS_CHOICES, default=STATUS_NOT_ATTEMPTED, verbose_name="Статус",
    )
    solutions_total = models.PositiveIntegerField(default=0, verbose_name="Всего посылок")
    solutions_correct = models.PositiveIntegerField(default=0, verbose_name="Верных посылок")
    refreshed_at = models.DateTimeField(
        null=True, blank=True, verbose_name="Обновлён по DL",
    )
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Обновлён")

    class Meta:
        db_table = "ai_task_language_status"
        verbose_name = "Статус задачи по языку"
        verbose_name_plural = "Статусы задач по языкам"
        ordering = ("course_id", "programming_language_id", "node_id")
        constraints = [
            models.UniqueConstraint(
                fields=["course_id", "node_id", "programming_language_id"],
                name="unique_task_lang_status_course_node_lang",
            ),
        ]
        indexes = [
            models.Index(
                fields=["course_id", "programming_language_id"],
                name="ai_task_lang_status_lookup_idx",
            ),
        ]

    def __str__(self):
        return (
            f"Курс {self.course_id}, задача {self.node_id}, "
            f"язык {self.programming_language_id}: {self.get_status_display()}"
        )
