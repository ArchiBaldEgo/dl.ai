# src/ai/db/Prompt.py

from django.conf import settings
from django.contrib.postgres.indexes import GinIndex, OpClass
from django.db import models
from django.db.models.functions import Upper

from ai.i18n import get_localized_name, get_localized_text

from ai.db.ProgrammingLanguage import ProgrammingLanguage
from ai.db.Topic import Topic
from ai.db.SharedPrompt import SharedPrompt

from ai.db.utils import replace_placeholders

class Prompt(models.Model):
    """Промпт, привязанный к конкретной теме.

    Может ссылаться на SharedPrompt (shared_prompt) — в этом случае итоговый текст
    берётся из общего промпта с подстановкой языка и темы. Если задан
    prompt_text_override, он переопределяет текст общего промпта.
    Поддерживает мультиязычные названия и тексты (ru/en/fr).

    mode — режим чата, в котором промпт предлагается пользователю
    (solve = «Реши задачу», find_error = «В чём ошибка»).
    """
    MODE_SOLVE = "solve"
    MODE_FIND_ERROR = "find_error"
    PROMPT_MODE_CHOICES = (
        (MODE_SOLVE, "Реши задачу"),
        (MODE_FIND_ERROR, "В чём ошибка"),
    )
    mode = models.CharField(
        max_length=16,
        choices=PROMPT_MODE_CHOICES,
        default=MODE_SOLVE,
        db_index=True,
        verbose_name="Режим",
        help_text="В каком режиме страницы предлагается этот промпт.",
    )
    topic = models.ForeignKey(
        Topic, on_delete=models.CASCADE, null=True, blank=True,
        verbose_name="Тема",
    )
    # Собственный язык промпта. Раньше язык выводился только из темы, из-за
    # чего у «общих» промптов (без темы, напр. «ASM i86 - общий») язык нельзя
    # было задать: селект в форме был виртуальным и не сохранялся. Теперь
    # промпт может быть привязан к языку целиком (тема пустая) — как
    # ArmPromptBinding («пустая тема — привязка на весь язык»). Если заданы и
    # тема, и язык — форма следит, чтобы тема принадлежала языку.
    programming_language = models.ForeignKey(
        ProgrammingLanguage, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="prompts",
        verbose_name="Язык программирования",
        help_text="Без темы — промпт действует на весь язык.",
    )
    prompt_text_ru = models.TextField(blank=True, default="", verbose_name="Текст (RU)")
    prompt_text_en = models.TextField(blank=True, default="", verbose_name="Текст (EN)")
    prompt_text_fr = models.TextField(blank=True, default="", verbose_name="Текст (FR)")
    prompt_name_ru = models.CharField(max_length=255, blank=True, default="", verbose_name="Название (RU)")
    prompt_name_en = models.CharField(max_length=255, blank=True, default="", verbose_name="Название (EN)")
    prompt_name_fr = models.CharField(max_length=255, blank=True, default="", verbose_name="Название (FR)")
    # Ссылка на общий промпт (если есть - текст берётся из него с подстановкой языка и темы)
    shared_prompt = models.ForeignKey(
        SharedPrompt, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="language_prompts",
        verbose_name="Общий промпт",
    )
    # Переопределение текста для конкретного языка (если null - используется shared_prompt.prompt_text)
    prompt_text_override = models.TextField(null=True, blank=True, verbose_name="Переопределение текста")
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="owned_prompts",
        verbose_name="Владелец",
    )
    editors = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        related_name="editable_prompts",
        verbose_name="Редакторы",
    )

    def get_effective_text(self, ui_language: str = "", programming_language_name: str = "", topic_name: str = "", message: str = "", code: str = ""):
        """Возвращает итоговый текст промпта с учётом UI-языка, языка программирования и темы."""
        if self.prompt_text_override:
            base = self.prompt_text_override
        elif self.shared_prompt:
            base = self.shared_prompt.get_effective_text(ui_language, programming_language_name, topic_name, message, code)
        else:
            base = get_localized_text(self, ui_language, "prompt_text") or self.prompt_text_ru
        return replace_placeholders(base, programming_language_name, topic_name, message, code)

    def __str__(self):
        # Возвращаем локализованное имя промпта вместо полного текста
        name = get_localized_name(self, "", "prompt_name")
        return name if name else f"Prompt #{self.id}"

    class Meta:
        db_table = 'ai_prompt'
        verbose_name = "Промпт"
        verbose_name_plural = "Промпты"
        indexes = [
            # Поиск по названию/тексту промпта в админке (icontains) —
            # триграммы, создаются на Postgres миграцией 0079.
            GinIndex(
                OpClass(Upper("prompt_name_ru"), "gin_trgm_ops"),
                name="ai_prompt_name_trgm_idx",
            ),
            GinIndex(
                OpClass(Upper("prompt_text_ru"), "gin_trgm_ops"),
                name="ai_prompt_text_trgm_idx",
            ),
        ]
