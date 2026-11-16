# src/ai/db/AIDevTask.py

from django.conf import settings
from django.db import models

class AIDevTask(models.Model):
    """Таск на доске задач для разработчиков (страница /ai/admin/tasks/).

    Два типа (kind): FRONT «Фронт работы» и WISH «Хотелки» — у каждого СВОЯ
    сквозная нумерация (priority_no) с №1, уникальная внутри вида
    (констрейнт по (kind, priority_no)); МЕНЬШЕ = ВАЖНЕЕ; выдаётся сервером
    при создании (max+1 внутри вида), менять порядок можно
    перетаскиванием строк (superuser/action.task_reorder → /tasks/reorder/
    перенумеровывает 1..N в пределах вида). Создаётся (название, описание,
    срок — ТЕКСТОМ: «До 2 недель» — никаких расчётных дат) тем, у кого есть
    action.task_create; закрыть — увод с доски (closed_at/closed_by,
    priority_no сбрасывается): таск ОСТАЁТСЯ в БД и попадает в архив
    (вкладка «Закрытые»), откуда суперюзер/action.task_close может
    вернуть его на доску (reopen). Разработчик забирает таск («Взять»):
    taken_by/taken_at пишутся атомарно
    (условный UPDATE, «первый успел»), у взятого таска в таблице — ФИО +
    группа + дата взятия, повторно взять нельзя. ModelAdmin не
    регистрируется — доска целиком кастомная страница (ai/admin/tasks.py).
    """

    class Kind(models.TextChoices):
        FRONT = "front", "Фронт работы"
        WISH = "wish", "Хотелки"

    title = models.CharField(max_length=200, verbose_name="Название")
    description = models.TextField(
        blank=True, default="", verbose_name="Описание"
    )
    estimate = models.CharField(
        max_length=100, blank=True, default="", verbose_name="Срок",
        help_text="Текстом: «До 2 недель», «До 4 дней»",
    )
    kind = models.CharField(
        max_length=10, choices=Kind.choices, default=Kind.FRONT,
        verbose_name="Тип",
    )
    # Номер присваивается всем видам (max+1 внутри вида, миграция 0064);
    # NULL остаётся легаси-запасом. Уникальность — по (kind, priority_no),
    # частичный UniqueConstraint из Meta.constraints.
    priority_no = models.PositiveIntegerField(
        null=True, blank=True, verbose_name="Номер приоритета",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="created_dev_tasks",
        verbose_name="Кто создал",
    )
    taken_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="claimed_dev_tasks",
        verbose_name="Кто взял",
    )
    taken_at = models.DateTimeField(null=True, blank=True, verbose_name="Взят")
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="closed_dev_tasks",
        verbose_name="Кто закрыл",
    )
    closed_at = models.DateTimeField(null=True, blank=True, verbose_name="Закрыт")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Создан")

    class Meta:
        verbose_name = "Таск (доска задач)"
        verbose_name_plural = "Таски (доска задач)"
        ordering = ("kind", "priority_no", "-created_at", "-id")
        indexes = [
            # Доска «Таски» сортирует по Meta.ordering и фильтрует по kind.
            models.Index(
                fields=["kind", "priority_no", "created_at", "id"],
                name="ai_dev_task_order_idx",
            ),
        ]
        constraints = [
            # «Цифры повторяться не могут» — уникальный номер ВНУТРИ вида
            # (фронт и хотелки нумеруются независимо, оба с №1).
            models.UniqueConstraint(
                fields=["kind", "priority_no"],
                condition=models.Q(priority_no__isnull=False),
                name="ai_dev_task_priority_no_uniq",
            ),
        ]

    def __str__(self):
        if self.priority_no is not None:
            number = f"№{self.priority_no}"
        else:
            number = f"{self.kind} без номера"
        suffix = f" (взял {self.taken_by_id})" if self.taken_by_id else ""
        return f"[{number}] {self.title}{suffix}"
