# src/ai/db/AIAdminGroupPermission.py

from django.db import models

class AIAdminGroupPermission(models.Model):
    """Один ключ доступа, выдающийся группам «Групп доступа» (AIAccessGroup).

    Ключи сидируются миграцией (см. ai/constants.py). Разделы секций — для
    группировки чекбоксов на странице «Группы доступа»: page — вкладки
    админки, docs — главы документации, action — действия на страницах.
    """

    SECTION_PAGE = "page"
    SECTION_DOCS = "docs"
    SECTION_ACTION = "action"

    key = models.SlugField(max_length=50, unique=True, verbose_name="Ключ")
    label = models.CharField(max_length=200, verbose_name="Название")
    section = models.CharField(
        max_length=20, default=SECTION_PAGE,
        choices=[
            (SECTION_PAGE, "Страницы"),
            (SECTION_DOCS, "Документация"),
            (SECTION_ACTION, "Действия"),
        ],
        verbose_name="Раздел",
    )

    class Meta:
        verbose_name = "Право доступа (группы)"
        verbose_name_plural = "Права доступа (группы)"
        ordering = ("section", "key")

    def __str__(self):
        return self.key
