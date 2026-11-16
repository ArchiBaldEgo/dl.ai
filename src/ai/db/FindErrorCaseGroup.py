# src/ai/db/FindErrorCaseGroup.py

from django.db import models

from ai.db.ProgrammingLanguage import ProgrammingLanguage

class FindErrorCaseGroup(models.Model):
    """Группа ошибочных программ: одна задача/папка датасета «В чём ошибка».

    Условие задачи хранится на группе, а не на каждой программе: в датасете
    все программы одной папки относятся к одной задаче DL. ``dl_node_id`` /
    ``dl_course_id`` позволяют обновлять условие из DL (``fetch_task_info``);
    ``info.txt`` — офлайн-фолбэк. Группа не связана с ``Task`` жёстко, чтобы
    контент-владельцы могли пополнять базу без заведения DL-задач.
    """

    dataset = models.CharField(
        max_length=128, default="odnomerny_massiv", db_index=True,
        verbose_name="Датасет",
    )
    name = models.CharField(max_length=255, verbose_name="Название группы")
    source_ref = models.CharField(
        max_length=512, unique=True, verbose_name="Ссылка в датасете",
        help_text="Например: odnomerny_massiv/1. Отчисление",
    )
    programming_language = models.ForeignKey(
        ProgrammingLanguage, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="find_error_case_groups", verbose_name="Язык программирования",
    )
    topic_name = models.CharField(
        max_length=255, blank=True, default="", verbose_name="Тема (подпись)",
    )
    dl_node_id = models.PositiveBigIntegerField(
        null=True, blank=True, db_index=True, verbose_name="ID узла DL",
    )
    dl_course_id = models.PositiveBigIntegerField(
        null=True, blank=True, verbose_name="ID курса DL",
    )
    statement = models.TextField(blank=True, default="", verbose_name="Условие задачи")
    statement_source = models.CharField(
        max_length=16, blank=True, default="",
        choices=(
            ("dl", "DL"),
            ("task", "Локальная задача"),
            ("info_txt", "info.txt"),
            ("manual", "Вручную"),
        ),
        verbose_name="Источник условия",
    )
    statement_updated_at = models.DateTimeField(
        null=True, blank=True, verbose_name="Условие обновлено",
    )
    notes = models.TextField(blank=True, default="", verbose_name="Примечания")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Создана")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Обновлена")

    class Meta:
        db_table = "ai_find_error_case_group"
        verbose_name = "Группа ошибочных программ"
        verbose_name_plural = "Группы ошибочных программ"
        ordering = ("dataset", "name")
        constraints = [
            models.UniqueConstraint(
                fields=("dataset", "name"), name="ai_find_error_group_dataset_name_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["programming_language", "topic_name"], name="ai_fe_group_lang_topic_idx"),
        ]

    def __str__(self):
        return self.name or f"Группа #{self.pk}"

