# ai/db/Topic.py

from django.db import models

from ai.db.ProgrammingLanguage import ProgrammingLanguage

from ai.i18n import get_localized_name

class Topic(models.Model):
    """Тема (раздел) в рамках языка программирования.

    Мультиязычные названия (ru/en/fr); ``topic_name_ru`` — базовый вариант и
    fallback цепочки локализации (get_localized_name). Используется для
    подстановки плейсхолдера {topic} в промпты и для группировки задач в
    ARM-отчётах.
    """
    topic_name_ru = models.CharField(max_length=255, blank=True, default="", verbose_name="Название (RU)")
    topic_name_en = models.CharField(max_length=255, blank=True, default="", verbose_name="Название (EN)")
    topic_name_fr = models.CharField(max_length=255, blank=True, default="", verbose_name="Название (FR)")
    programming_language = models.ForeignKey(
        ProgrammingLanguage, on_delete=models.CASCADE, null=True,
        verbose_name="Язык программирования",
    )  # Добавляем связь с языком программирования

    class Meta:
        verbose_name = "Тема"
        verbose_name_plural = "Темы"

    def __str__(self):
        return get_localized_name(self, "", "topic_name")

