# src/ai/db/ArmPromptBinding.py

from django.conf import settings
from django.contrib.postgres.indexes import GinIndex, OpClass
from django.db import models

from ai.db.ProgrammingLanguage import ProgrammingLanguage
from ai.db.Topic import Topic
from ai.db.Prompt import Prompt

class ArmPromptBinding(models.Model):
    """Привязка «промпт по умолчанию» для ARM: (язык, тема, вид ARM) → промпт.

    Суперюзерский инструмент «Промпты по умолчанию»: на /arm/solve/ и
    /arm/find-error/ после выбора темы промпт подтягивается автоматически,
    чтобы пользователю не приходилось выбирать один и тот же промпт каждый
    раз. Промпт редактируется по обычным правилам (владелец/редактор/
    суперюзер) — привязка тянет актуальный текст по FK, ничего не копируем.

    ``topic`` может быть NULL — привязка «на весь язык» (ЯП = формат: язык
    эквивалентен своему расширению, промпт по умолчанию на весь Python
    покрывает все .py-задачи). Нужно там, где у языка нет тем (Python, C++),
    и как fallback, когда точная привязка (язык+тема) не задана.
    """
    MODE_SOLVE = "solve"
    MODE_FIND_ERROR = "find_error"
    ARM_MODE_CHOICES = [
        (MODE_SOLVE, "Пакетное решение"),
        (MODE_FIND_ERROR, "Поиск ошибки"),
    ]

    programming_language = models.ForeignKey(
        ProgrammingLanguage, on_delete=models.CASCADE,
        related_name="arm_prompt_bindings",
        verbose_name="Язык программирования",
    )
    topic = models.ForeignKey(
        Topic, on_delete=models.CASCADE, null=True, blank=True,
        related_name="arm_prompt_bindings",
        verbose_name="Тема",
    )
    mode = models.CharField(max_length=16, choices=ARM_MODE_CHOICES, verbose_name="Вид ARM")
    prompt = models.ForeignKey(
        Prompt, on_delete=models.CASCADE,
        related_name="arm_bindings",
        verbose_name="Промпт",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Создана")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Обновлена")

    class Meta:
        db_table = "ai_arm_prompt_binding"
        verbose_name = "Привязка промпта ARM"
        verbose_name_plural = "Привязки промптов ARM"
        constraints = [
            models.UniqueConstraint(
                fields=["programming_language", "topic", "mode"],
                name="unique_arm_prompt_binding",
            ),
            # Привязка «на весь язык»: одна на (язык, вид). Частичный индекс —
            # обычный UNIQUE не ловит дубликаты NULL-темы в Postgres.
            models.UniqueConstraint(
                fields=["programming_language", "mode"],
                condition=models.Q(topic__isnull=True),
                name="unique_arm_prompt_binding_language",
            ),
        ]

    @classmethod
    def resolve(cls, *, programming_language_id, topic_id, mode):
        """Привязка для прогона: точная (язык+тема) → иначе «на весь язык».

        Возвращает ArmPromptBinding (select_related("prompt")) или None —
        нет ни одной → прогон без промпта.
        """
        base = cls.objects.filter(
            mode=mode, programming_language_id=programming_language_id,
        ).select_related("prompt")
        if topic_id:
            exact = base.filter(topic_id=topic_id).first()
            if exact:
                return exact
        return base.filter(topic__isnull=True).first()

    def __str__(self):
        topic = self.topic if self.topic is not None else "(весь язык)"
        return f"{self.get_mode_display()}: {self.programming_language} / {topic} → {self.prompt}"
