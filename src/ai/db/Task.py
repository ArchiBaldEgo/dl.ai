# ai/db/Task.py

from django.contrib.postgres.indexes import GinIndex, OpClass
from django.db import models
from django.db.models.functions import Upper

from ai.db.ProgrammingLanguage import ProgrammingLanguage
from ai.db.Topic import Topic

class Task(models.Model):
    """Локальная ссылка на задачу dl.gsu.by для batch-solve ARM.

    Оператор вводит DL ``node_id`` (уникальный идентификатор узла задачи на
    dl.gsu.by); название и условие тянутся из внешнего API через
    ``fetch_task_info`` (action ``refresh_from_dl`` в админке). Тему и язык
    программирования оператор назначает локально — они нужны для подстановки в
    solve-промпт и для группировки отчёта по темам. ``file_extension`` задаётся
    вручную (например ``.pas``/``.cpp``/``.py``), т.к. из локального
    ``ProgrammingLanguage`` (отображаемое имя) его не вывести, а он требуется для
    ``fetch_task_solution``.
    """

    node_id = models.PositiveIntegerField(
        unique=True, db_index=True, verbose_name="ID узла DL",
        help_text="Идентификатор узла задачи на dl.gsu.by (nodeId).",
    )
    task_id = models.PositiveIntegerField(
        null=True, blank=True, db_index=True, verbose_name="ID задачи DL",
        help_text="Заполняется из get-task-info (поле taskId).",
    )
    name = models.CharField(max_length=512, blank=True, default="", verbose_name="Название")
    statement = models.TextField(blank=True, default="", verbose_name="Условие")
    topic = models.ForeignKey(
        Topic, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="tasks", verbose_name="Тема",
    )
    programming_language = models.ForeignKey(
        ProgrammingLanguage, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="tasks", verbose_name="Язык программирования",
    )
    file_extension = models.CharField(
        max_length=16, blank=True, default="", verbose_name="Расширение файла",
        help_text="Например .pas, .cpp, .py — используется для get-solution.",
    )
    active = models.BooleanField(default=True, db_index=True, verbose_name="Активна")
    # Путь задачи в дереве задач DL (get-task-info → path; либо дерево
    # курса, закэшированное на форме /arm/solve/), напр.
    # «Программирование [Ассемблер i8086, C-MPA]\Одномерные массивы\Сумма цифр числа».
    # Нужен для честной подписи темы в отчёте пакетного решения: когда
    # локальная тема не определена, подписью служит папка DL, а не имя задачи.
    tree_path = models.CharField(
        max_length=512, blank=True, default="", verbose_name="Путь в дереве задач DL",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Создан")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Обновлён")

    class Meta:
        db_table = "ai_task"
        verbose_name = "Задача (DL)"
        verbose_name_plural = "Задачи (DL)"
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["created_at"], name="ai_task_created_idx"),
            # Поиск по названию/условию задачи (icontains) — триграммы, 0079.
            GinIndex(
                OpClass(Upper("name"), "gin_trgm_ops"),
                name="ai_task_name_trgm_idx",
            ),
            GinIndex(
                OpClass(Upper("statement"), "gin_trgm_ops"),
                name="ai_task_statement_trgm_idx",
            ),
        ]

    def __str__(self):
        return self.name or f"DL #{self.node_id}"


