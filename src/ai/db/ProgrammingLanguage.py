# src/ai/db/ProgrammingLanguage.py

from django.db import models

class ProgrammingLanguage(models.Model):
    """Язык программирования, используемый в темах и задачах.

    Связан с Topic (один ко многим) и используется для подстановки плейсхолдера
    {language} в тексты промптов.
    """
    language_name = models.CharField(max_length=255, verbose_name="Название")

    class Meta:
        verbose_name = "Язык программирования"
        verbose_name_plural = "Языки программирования"

    def __str__(self):
        return self.language_name

