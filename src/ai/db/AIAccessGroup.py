# src/ai/db/AIAccessGroup.py

from django.conf import settings
from django.db import models

from ai.db.AIAdminGroupPermission import AIAdminGroupPermission

class AIAccessGroup(models.Model):
    """Группа доступа: членам выдаются права ключами (AIAdminGroupPermission).

    Заменяет жёсткие проверки is_prompt_developer_user/Django-Group
    prompt_developer: все can_access_* резолвят ключи из групп юзера
    (ai/admin/permissions.py::user_access_keys); состав и права каждой
    группы редактируются суперюзером на странице «Группы доступа»
    (/ai/admin/ai-groups/, ai/admin/access_groups.py). Встроенные группы
    («Разработчик промптов», «Разработчик») сидируются миграцией; их нельзя
    удалить и переименовать, но права правятся на той же странице.
    """

    name = models.CharField(max_length=100, unique=True, verbose_name="Название")
    description = models.TextField(blank=True, default="", verbose_name="Описание")
    is_builtin = models.BooleanField(default=False, verbose_name="Встроенная")
    permissions = models.ManyToManyField(
        AIAdminGroupPermission, blank=True, related_name="access_groups",
        verbose_name="Права",
    )
    users = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True, related_name="ai_access_groups",
        verbose_name="Участники",
    )

    class Meta:
        verbose_name = "Группа доступа"
        verbose_name_plural = "Группы доступа"
        ordering = ("name",)

    def __str__(self):
        return self.name
