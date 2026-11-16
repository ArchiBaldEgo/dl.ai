# src/ai/db/AICourseTreeCache.py

from django.db import models

class AICourseTreeCache(models.Model):
    """Сохранённое дерево курса dl.gsu.by (последняя успешная загрузка).

    get-node-tree у DL требует АКТИВНЫЙ курс в DL-сессии (courseID>0): у
    пользователя, который не «зашёл» в курс на dl.gsu.by, DL отвечает 403
    «Доступ запрещён» — и до вката бессрочного кэша дерево не грузилось НИ у
    кого. Redis-копия (dl_tree_stale) переживает TTL, но не переживает
    очистку Redis; эта таблица — последняя линия обороны: при успехе
    (load-tree) сюда пишется копия дерева, при отказе DL она отдаётся всем,
    поэтому «Пакетное решение» работает у всех после одной успешной загрузки.
    """

    course_id = models.PositiveIntegerField(
        unique=True, verbose_name="Курс DL (courseId)"
    )
    tree = models.JSONField(default=list, verbose_name="Дерево (JSON)")
    task_count = models.PositiveIntegerField(default=0, verbose_name="Задач в дереве")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Обновлено")

    class Meta:
        verbose_name = "Кэш дерева курса DL"
        verbose_name_plural = "Кэши деревьев курсов DL"

    def __str__(self):
        return f"course {self.course_id} · {self.task_count} задач"
