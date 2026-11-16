# src/ai/db/TaskSolution.py

from django.conf import settings
from django.contrib.postgres.indexes import GinIndex, OpClass
from django.db import models
from django.db.models.functions import Upper

class TaskSolution(models.Model):
    """Кэш решённых задач «Реши задачу» (узел DL + язык → проверенный код).

    Записи создаются только когда пользователь сам отправил сгенерированный
    код на тестирование со страницы «Реши задачу» (HTTP send-solution; ARM и
    прочие прогоны кэш не пишут). После успешного тестирования (verdict =
    passed) код отдаётся из кэша всем, кто повторно запросит ту же задачу на
    том же языке, — без вызова модели. ``queue_id`` — активный poll DL
    (send-solution → get-solution-result); после финального вердикта
    сбрасывается. ``model_key`` — модель, сгенерировавшая код (из
    AIRequestLog последней успешной генерации для узла).
    """

    VERDICT_PENDING = "pending"
    VERDICT_PASSED = "passed"
    VERDICT_FAILED = "failed"

    VERDICT_CHOICES = (
        (VERDICT_PENDING, "Тестируется"),
        (VERDICT_PASSED, "Тестирование пройдено"),
        (VERDICT_FAILED, "Тестирование не пройдено"),
    )

    task_node_id = models.PositiveIntegerField(db_index=True, verbose_name="ID задачи DL")
    # Курс, в контексте которого задача тестировалась (из send-solution).
    # Нужен для пользовательской ссылки на задачу: /task.jsp?nid=…&cid=…
    # (admin-вьювер fullTaskviewer не используется — просмотр от имени админа).
    course_id = models.IntegerField(null=True, blank=True, verbose_name="ID курса DL")
    # Путь задачи в дереве задач DL (get-task-info → path при известном
    # курсе), напр. «Программирование\Циклы\Сумма». Заполняется best-effort
    # при отправке решения на тестирование; старые записи получают путь при
    # повторном решении той же задачи.
    tree_path = models.CharField(
        max_length=512, blank=True, default="", verbose_name="Путь в дереве задач DL",
    )
    programming_language_id = models.IntegerField(null=True, blank=True, verbose_name="ID языка программирования")
    file_extension = models.CharField(max_length=16, blank=True, default="", verbose_name="Расширение файла")
    code = models.TextField(verbose_name="Код решения")
    verdict = models.CharField(max_length=16, choices=VERDICT_CHOICES, default=VERDICT_PENDING, verbose_name="Вердикт")
    dl_comment = models.TextField(blank=True, default="", verbose_name="Комментарий DL")
    model_key = models.CharField(max_length=128, blank=True, default="", verbose_name="Ключ модели")
    model_title = models.CharField(max_length=255, blank=True, default="", verbose_name="Название модели")
    # Контекст последней генерации решения (как в AIRequestLog — id + имя).
    topic_id = models.IntegerField(null=True, blank=True, verbose_name="ID темы")
    topic_name = models.CharField(max_length=255, blank=True, default="", verbose_name="Тема")
    prompt_id = models.IntegerField(null=True, blank=True, verbose_name="ID промпта")
    prompt_name = models.CharField(max_length=255, blank=True, default="", verbose_name="Промпт")
    queue_id = models.PositiveIntegerField(null=True, blank=True, db_index=True, verbose_name="Queue ID DL")
    test_log = models.ForeignKey(
        "AIRequestLog", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="task_solutions", verbose_name="Запись журнала тестирования",
    )
    submitted_at = models.DateTimeField(null=True, blank=True, verbose_name="Отправлен на тестирование")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="task_solutions", verbose_name="Пользователь",
    )
    external_user_id = models.CharField(max_length=255, blank=True, default="", verbose_name="ID пользователя dl.gsu.by")
    times_used = models.PositiveIntegerField(default=0, verbose_name="Сколько раз выдавался из кэша")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Создан")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Обновлён")

    class Meta:
        db_table = "ai_task_solution"
        verbose_name = "Решённая задача (кэш кода)"
        verbose_name_plural = "Решённые задачи (кэш кода)"
        ordering = ("-updated_at",)
        indexes = [
            models.Index(fields=["updated_at"], name="ai_task_solution_updated_idx"),
            # Поиск по id/модели/теме/промпту (icontains) — триграммы, 0079.
            GinIndex(
                OpClass(Upper("external_user_id"), "gin_trgm_ops"),
                name="ai_tsol_extid_trgm_idx",
            ),
            GinIndex(
                OpClass(Upper("model_key"), "gin_trgm_ops"),
                name="ai_tsol_model_trgm_idx",
            ),
            GinIndex(
                OpClass(Upper("topic_name"), "gin_trgm_ops"),
                name="ai_tsol_topic_trgm_idx",
            ),
            GinIndex(
                OpClass(Upper("prompt_name"), "gin_trgm_ops"),
                name="ai_tsol_prompt_trgm_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["task_node_id", "programming_language_id"],
                name="unique_task_solution_node_lang",
            ),
        ]

    def __str__(self):
        lang = self.programming_language_id or "-"
        return f"Задача {self.task_node_id} ({lang}): {self.verdict}"

