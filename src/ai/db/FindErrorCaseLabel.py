# src/ai/db/FindErrorCaseLabel.py

from django.db import models

from ai.db.FindErrorCase import FindErrorCase

class FindErrorCaseLabel(models.Model):
    """Метка ошибки кейса: код из каталога + пояснение из error_N.txt."""

    case = models.ForeignKey(
        FindErrorCase, on_delete=models.CASCADE, related_name="labels",
        verbose_name="Программа",
    )
    code = models.PositiveSmallIntegerField(verbose_name="Код ошибки")
    text = models.TextField(blank=True, default="", verbose_name="Текст ошибки")

    class Meta:
        db_table = "ai_find_error_case_label"
        verbose_name = "Метка ошибки"
        verbose_name_plural = "Метки ошибок"
        ordering = ("code",)
        constraints = [
            models.UniqueConstraint(
                fields=("case", "code"), name="ai_find_error_label_case_code_uniq",
            ),
        ]

    def __str__(self):
        return f"error_{self.code}"
