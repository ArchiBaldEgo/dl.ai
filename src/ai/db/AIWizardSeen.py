# src/ai/db/AIWizardSeen.py

from django.conf import settings
from django.contrib.postgres.indexes import GinIndex, OpClass
from django.db import models
from django.utils import timezone

class AIWizardSeen(models.Model):
    """Отметка «пользователь уже видел онбординг-wizard этой версии».

    Одна строка на (пользователь, scope): тур для страницы «chat»/«solve»/
    «find_error» или админки («admin», «admin_arm_solve», «admin_model_status»).
    Показывать заново, когда нет строки или version меньше WIZARD_VERSION
    (ai/constants.py) — так изменённые тексты тура показываются всем ещё раз.
    Решение «показывать ли» принимает ai/services/onboarding.py.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="wizard_seen",
        verbose_name="Пользователь",
    )
    scope = models.CharField(max_length=32, verbose_name="Страница (scope тура)")
    version = models.PositiveIntegerField(
        default=1, verbose_name="Версия текстов подсказок",
    )
    created_at = models.DateTimeField(default=timezone.now, verbose_name="Когда пройдено")

    class Meta:
        db_table = "ai_wizard_seen"
        verbose_name = "Пройденный онбординг"
        verbose_name_plural = "Пройденные онбординги"
        ordering = ("user_id", "scope")
        constraints = [
            models.UniqueConstraint(
                fields=("user", "scope"),
                name="ai_wizard_seen_user_scope_uniq",
            ),
        ]

    def __str__(self):
        return f"{self.user_id} / {self.scope} v{self.version}"
