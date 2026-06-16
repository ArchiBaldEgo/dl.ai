from django.conf import settings
from django.db import models
from django.utils import timezone

from .i18n import get_localized_name, get_localized_text


class ExternalDLAccount(models.Model):
    """Link between Django User and external DL (dl.gsu.by) account."""
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='external_dl_account',
    )
    external_user_id = models.CharField(
        max_length=255,
        unique=True,
        db_index=True,
        help_text="User ID from dl.gsu.by API"
    )
    external_login = models.CharField(
        max_length=255,
        help_text="Last known login/nickname from dl.gsu.by"
    )
    external_first_name = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="First name from dl.gsu.by"
    )
    external_last_name = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Last name from dl.gsu.by"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "External DL Account"
        verbose_name_plural = "External DL Accounts"

    def __str__(self):
        return f"{self.user.username} (DL: {self.external_login})"


class ProgrammingLanguage(models.Model):
    language_name = models.CharField(max_length=255,)

    def __str__(self):
        return self.language_name


class Topic(models.Model):
    topic_name = models.CharField(max_length=255)
    topic_name_ru = models.CharField(max_length=255, blank=True, default="")
    topic_name_en = models.CharField(max_length=255, blank=True, default="")
    topic_name_fr = models.CharField(max_length=255, blank=True, default="")
    programming_language = models.ForeignKey(ProgrammingLanguage, on_delete=models.CASCADE, null = True)  # Добавляем связь с языком программирования

    def __str__(self):
        return get_localized_name(self, "", "topic_name")


# Общий (shared) препромпт - не привязан к конкретному языку программирования.
# Текст может содержать placeholder {language}, который заменяется на имя языка при использовании.
class SharedPrompt(models.Model):
    prompt_name = models.CharField(max_length=255)
    prompt_name_ru = models.CharField(max_length=255, blank=True, default="")
    prompt_name_en = models.CharField(max_length=255, blank=True, default="")
    prompt_name_fr = models.CharField(max_length=255, blank=True, default="")
    prompt_text = models.TextField()
    prompt_text_ru = models.TextField(blank=True, default="")
    prompt_text_en = models.TextField(blank=True, default="")
    prompt_text_fr = models.TextField(blank=True, default="")
    # Языки, для которых этот общий препромпт доступен (blank = для всех)
    programming_languages = models.ManyToManyField(
        ProgrammingLanguage, blank=True, related_name="shared_prompts"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="owned_shared_prompts",
    )
    editors = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        related_name="editable_shared_prompts",
    )

    def __str__(self):
        return f"[Общий] {get_localized_name(self, '', 'prompt_name')}"

    def get_effective_text(self, ui_language="", programming_language_name=""):
        base = get_localized_text(self, ui_language, "prompt_text") or self.prompt_text
        if programming_language_name:
            base = base.replace("{language}", programming_language_name)
            base = base.replace("{язык}", programming_language_name)
        return base

    class Meta:
        db_table = 'ai_sharedprompt'
        verbose_name = 'Общий препромпт'
        verbose_name_plural = 'Общие препромпты'

class Prompt(models.Model):
    topic = models.ForeignKey(Topic, on_delete=models.CASCADE, null=True, blank=True)
    prompt_text = models.TextField()
    prompt_text_ru = models.TextField(blank=True, default="")
    prompt_text_en = models.TextField(blank=True, default="")
    prompt_text_fr = models.TextField(blank=True, default="")
    prompt_name = models.CharField(max_length=255, null = True)
    prompt_name_ru = models.CharField(max_length=255, blank=True, default="")
    prompt_name_en = models.CharField(max_length=255, blank=True, default="")
    prompt_name_fr = models.CharField(max_length=255, blank=True, default="")
    # Ссылка на общий препромпт (если есть - текст берётся из него с подстановкой языка)
    shared_prompt = models.ForeignKey(
        SharedPrompt, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="language_prompts"
    )
    # Переопределение текста для конкретного языка (если null - используется shared_prompt.prompt_text)
    prompt_text_override = models.TextField(null=True, blank=True)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="owned_prompts",
    )
    editors = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        related_name="editable_prompts",
    )

    def get_effective_text(self, ui_language: str = "", programming_language_name: str = ""):
        """Возвращает итоговый текст препромпта с учётом UI-языка и языка программирования."""
        if self.prompt_text_override:
            base = self.prompt_text_override
        elif self.shared_prompt:
            base = self.shared_prompt.get_effective_text(ui_language, programming_language_name)
        else:
            base = get_localized_text(self, ui_language, "prompt_text") or self.prompt_text
        if programming_language_name:
            base = base.replace("{language}", programming_language_name)
            base = base.replace("{язык}", programming_language_name)
        return base

    def __str__(self):
        # Возвращаем локализованное имя промпта вместо полного текста
        name = get_localized_name(self, "", "prompt_name")
        return name if name else f"Prompt #{self.id}"

    class Meta:
        db_table = 'ai_prompt'



class AIAppSettings(models.Model):
    is_enabled = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "AI app setting"
        verbose_name_plural = "AI app settings"

    def save(self, *args, **kwargs):
        # Keep a single row for global app state.
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def get_solo(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return "AI app settings"


class AIModelHealthRun(models.Model):
    STATUS_RUNNING = "running"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"

    STATUS_CHOICES = (
        (STATUS_RUNNING, "Running"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_FAILED, "Failed"),
    )

    window_date = models.DateField(unique=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_RUNNING)
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True, default="")

    class Meta:
        verbose_name = "AI model health run"
        verbose_name_plural = "AI model health runs"
        ordering = ("-window_date",)

    def __str__(self):
        return f"{self.window_date} ({self.status})"


class AIModelAvailability(models.Model):
    model_key = models.CharField(max_length=128, db_index=True)
    model_title = models.CharField(max_length=255)
    is_available = models.BooleanField(default=False)
    window_date = models.DateField(db_index=True)
    checked_at = models.DateTimeField(auto_now=True)
    response_time_ms = models.PositiveIntegerField(null=True, blank=True)
    last_message = models.TextField(blank=True, default="")

    class Meta:
        verbose_name = "AI model availability"
        verbose_name_plural = "AI model availability"
        ordering = ("model_title",)
        constraints = [
            models.UniqueConstraint(
                fields=("model_key", "window_date"),
                name="ai_model_availability_key_window_uniq",
            )
        ]

    def __str__(self):
        return f"{self.model_key}: {'up' if self.is_available else 'down'}"


class AIRequestLog(models.Model):
    STATUS_SUCCESS = "success"
    STATUS_ERROR = "error"

    STATUS_CHOICES = (
        (STATUS_SUCCESS, "Success"),
        (STATUS_ERROR, "Error"),
    )

    SOURCE_WEBSOCKET = "websocket"
    SOURCE_ARM = "arm"

    SOURCE_CHOICES = (
        (SOURCE_WEBSOCKET, "WebSocket"),
        (SOURCE_ARM, "ARM"),
    )

    MODE_CHAT = "chat"
    MODE_SOLVE = "solve"
    MODE_FIND_ERROR = "find_error"
    MODE_ARM = "arm"

    MODE_CHOICES = (
        (MODE_CHAT, "Чат"),
        (MODE_SOLVE, "Решить задачу"),
        (MODE_FIND_ERROR, "Найти ошибку"),
        (MODE_ARM, "ARM"),
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="ai_request_logs",
    )
    external_user_id = models.CharField(max_length=255, blank=True, db_index=True)
    username = models.CharField(max_length=255, blank=True)
    user_full_name = models.CharField(max_length=500, blank=True)
    client_id = models.CharField(max_length=255, blank=True)
    source = models.CharField(max_length=32, choices=SOURCE_CHOICES, default=SOURCE_WEBSOCKET)
    mode = models.CharField(max_length=16, choices=MODE_CHOICES, blank=True, default="")
    sent_at = models.DateTimeField()
    received_at = models.DateTimeField(null=True, blank=True)
    duration_seconds = models.FloatField(null=True, blank=True)
    model_names = models.JSONField(default=list, blank=True)
    message = models.TextField(blank=True)
    response_text = models.TextField(blank=True)
    tokens = models.PositiveIntegerField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_SUCCESS)
    error_message = models.TextField(blank=True)

    # Context selected by the user (programming task pages and ARM)
    programming_language_id = models.IntegerField(null=True, blank=True)
    programming_language_name = models.CharField(max_length=255, blank=True)
    topic_id = models.IntegerField(null=True, blank=True)
    topic_name = models.CharField(max_length=255, blank=True)
    prompt_id = models.IntegerField(null=True, blank=True)
    prompt_name = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = "ai_airequestlog"
        verbose_name = "AI request log"
        verbose_name_plural = "AI request logs"
        ordering = ("-sent_at",)

    def __str__(self):
        return f"{self.sent_at} — {self.user_full_name or self.username or self.external_user_id}"
