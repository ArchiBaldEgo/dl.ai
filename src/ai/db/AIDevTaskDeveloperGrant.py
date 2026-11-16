# src/ai/db/AIDevTaskDeveloperGrant.py

from django.conf import settings
from django.db import models

class AIDevTaskDeveloperGrant(models.Model):
    """Маркер: членство в группе «Разработчик» выдано СИСТЕМОЙ при поручении.

    Суперюзер может поручить таск любому активному пользователю; чтобы
    исполнитель увидел доску, он автоматически зачисляется в группу
    «Разработчик». Эта строка запоминает, что членство выдано автоматически,
    а не вручную через «Группы доступа» — только такие членства снимаются,
    когда у человека не осталось активных тасков
    (``ai/services/task_developers.py`` — единственный владелец правила).
    Нет строки = членство ручное либо его нет: автоматика не вмешивается.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="ai_task_developer_grant",
        verbose_name="Пользователь",
    )
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="+",
        verbose_name="Кто поручил",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Выдано")

    class Meta:
        verbose_name = "Авто-роль «Разработчик» (таски)"
        verbose_name_plural = "Авто-роли «Разработчик» (таски)"

    def __str__(self):
        return f"авто-роль Разработчик: user {self.user_id}"
