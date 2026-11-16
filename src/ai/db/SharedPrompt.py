# src/ai/db/SharedPrompt.py

from django.conf import settings
from django.db import models

from ai.i18n import get_localized_name, get_localized_text

from ai.db.ProgrammingLanguage import ProgrammingLanguage
from ai.db.utils import replace_placeholders

SHARED_PROMPT_MODE_CHOICES = (
    ("", "—"),
    ("chat", "Chat"),
    ("solve", "Solve"),
    ("find_error", "Find error"),
)


# Общий (shared) промпт - не привязан к конкретному языку программирования или теме.
# Текст может содержать placeholder {language}/{язык}, который заменяется на имя языка,
# и {topic}/{тема}, который заменяется на название темы при использовании.
class SharedPrompt(models.Model):
    """Общий (shared) промпт — не привязан к конкретному языку программирования или теме.

    Текст может содержать плейсхолдеры {language}/{язык} и {topic}/{тема},
    которые заменяются на имя языка и название темы при использовании.
    Если указан mode (chat/solve/find_error), промпт используется как
    системный шаблон по умолчанию для соответствующего режима.
    """
    prompt_name_ru = models.CharField(max_length=255, blank=True, default="", verbose_name="Название (RU)")
    prompt_name_en = models.CharField(max_length=255, blank=True, default="", verbose_name="Название (EN)")
    prompt_name_fr = models.CharField(max_length=255, blank=True, default="", verbose_name="Название (FR)")
    prompt_text_ru = models.TextField(
        blank=True,
        default="",
        help_text="Доступные плейсхолдеры: {language}/{язык} - язык программирования, {topic}/{тема} - тема.",
        verbose_name="Текст (RU)",
    )
    prompt_text_en = models.TextField(blank=True, default="", verbose_name="Текст (EN)")
    prompt_text_fr = models.TextField(blank=True, default="", verbose_name="Текст (FR)")
    # Языки, для которых этот общий промпт доступен (blank = для всех)
    programming_languages = models.ManyToManyField(
        ProgrammingLanguage, blank=True, related_name="shared_prompts",
        verbose_name="Языки программирования",
    )
    # Системный режим: если указан, промпт используется как default-шаблон для режима.
    mode = models.CharField(
        max_length=16,
        blank=True,
        choices=SHARED_PROMPT_MODE_CHOICES,
        null=True,
        verbose_name="Системный режим",
        help_text="Если указан, промпт используется как системный шаблон для соответствующего режима.",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Создан")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Обновлён")
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="owned_shared_prompts",
        verbose_name="Владелец",
    )
    editors = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        related_name="editable_shared_prompts",
        verbose_name="Редакторы",
    )

    def __str__(self):
        from ai.i18n import get_shared_prompt_prefix
        prefix = get_shared_prompt_prefix('')
        name = get_localized_name(self, '', 'prompt_name')
        return f"{prefix} {name}"

    def get_effective_text(self, ui_language="", programming_language_name="", topic_name="", message="", code=""):
        base = get_localized_text(self, ui_language, "prompt_text") or self.prompt_text_ru
        return replace_placeholders(base, programming_language_name, topic_name, message, code)

    class Meta:
        db_table = 'ai_sharedprompt'
        verbose_name = 'Общий промпт'
        verbose_name_plural = 'Общие промпты'
        constraints = [
            models.UniqueConstraint(
                fields=["mode"],
                condition=models.Q(mode__isnull=False) & ~models.Q(mode=""),
                name="unique_sharedprompt_mode_when_set",
            ),
        ]
