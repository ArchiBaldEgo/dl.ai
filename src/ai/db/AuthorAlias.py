# src/ai/db/AuthorAlias.py

from django.db import models

class AuthorAlias(models.Model):
    """Справочник авторов git-коммитов: ник → ФИО + группа.

    В UpdateLog.author хранится сырой ник из git (%an); ФИО/группа подставляются
    ПРИ РЕНДЕРЕ страницы «Обновления» (правка справочника ретроактивно чинит
    все прошлые строки). Ведётся вручную через /ai/admin/.
    """
    nick = models.CharField(max_length=255, unique=True, verbose_name="Ник в git")
    full_name = models.CharField(max_length=255, verbose_name="ФИО")
    group = models.CharField(max_length=255, blank=True, default="", verbose_name="Группа")

    class Meta:
        db_table = "ai_author_alias"
        verbose_name = "Автор коммитов (справочник)"
        verbose_name_plural = "Авторы коммитов (справочник)"
        ordering = ("full_name",)

    def __str__(self):
        return f"{self.nick} — {self.display_label}"

    @property
    def display_label(self):
        return f"{self.full_name} ({self.group})" if self.group else self.full_name

    @classmethod
    def display_map(cls):
        """{nick: display_label} — один запрос, единственный источник для рендера."""
        return {alias.nick: alias.display_label for alias in cls.objects.all()}

