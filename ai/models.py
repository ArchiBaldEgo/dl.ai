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


SHARED_PROMPT_MODE_CHOICES = (
    ("", "—"),
    ("chat", "Chat"),
    ("solve", "Solve"),
    ("find_error", "Find error"),
)


# Общий (shared) препромпт - не привязан к конкретному языку программирования или теме.
# Текст может содержать placeholder {language}/{язык}, который заменяется на имя языка,
# и {topic}/{тема}, который заменяется на название темы при использовании.
class SharedPrompt(models.Model):
    prompt_name = models.CharField(max_length=255)
    prompt_name_ru = models.CharField(max_length=255, blank=True, default="")
    prompt_name_en = models.CharField(max_length=255, blank=True, default="")
    prompt_name_fr = models.CharField(max_length=255, blank=True, default="")
    prompt_text = models.TextField(
        help_text="Доступные плейсхолдеры: {language}/{язык} - язык программирования, {topic}/{тема} - тема."
    )
    prompt_text_ru = models.TextField(blank=True, default="")
    prompt_text_en = models.TextField(blank=True, default="")
    prompt_text_fr = models.TextField(blank=True, default="")
    # Языки, для которых этот общий препромпт доступен (blank = для всех)
    programming_languages = models.ManyToManyField(
        ProgrammingLanguage, blank=True, related_name="shared_prompts"
    )
    # Системный режим: если указан, препромпт используется как default-шаблон для режима.
    mode = models.CharField(
        max_length=16,
        blank=True,
        choices=SHARED_PROMPT_MODE_CHOICES,
        null=True,
        verbose_name="Системный режим",
        help_text="Если указан, препромпт используется как системный шаблон для соответствующего режима.",
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

    def get_effective_text(self, ui_language="", programming_language_name="", topic_name="", message="", code=""):
        base = get_localized_text(self, ui_language, "prompt_text") or self.prompt_text
        if programming_language_name:
            base = base.replace("{language}", programming_language_name)
            base = base.replace("{язык}", programming_language_name)
        if topic_name:
            base = base.replace("{topic}", topic_name)
            base = base.replace("{тема}", topic_name)
        if "{message}" in base:
            base = base.replace("{message}", message or "")
        if "{code}" in base:
            base = base.replace("{code}", code or "")
        return base

    class Meta:
        db_table = 'ai_sharedprompt'
        verbose_name = 'Общий препромпт'
        verbose_name_plural = 'Общие препромпты'
        constraints = [
            models.UniqueConstraint(
                fields=["mode"],
                condition=models.Q(mode__isnull=False) & ~models.Q(mode=""),
                name="unique_sharedprompt_mode_when_set",
            ),
        ]

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
    # Ссылка на общий препромпт (если есть - текст берётся из него с подстановкой языка и темы)
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

    def get_effective_text(self, ui_language: str = "", programming_language_name: str = "", topic_name: str = "", message: str = "", code: str = ""):
        """Возвращает итоговый текст препромпта с учётом UI-языка, языка программирования и темы."""
        if self.prompt_text_override:
            base = self.prompt_text_override
        elif self.shared_prompt:
            base = self.shared_prompt.get_effective_text(ui_language, programming_language_name, topic_name, message, code)
        else:
            base = get_localized_text(self, ui_language, "prompt_text") or self.prompt_text
        if programming_language_name:
            base = base.replace("{language}", programming_language_name)
            base = base.replace("{язык}", programming_language_name)
        if topic_name:
            base = base.replace("{topic}", topic_name)
            base = base.replace("{тема}", topic_name)
        if "{message}" in base:
            base = base.replace("{message}", message or "")
        if "{code}" in base:
            base = base.replace("{code}", code or "")
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
    last_http_code = models.PositiveSmallIntegerField(null=True, blank=True)
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


class AIModelTokenBudget(models.Model):
    """Manually maintained token budget for an AI provider/account.

    The total limit and its issue date are set by an admin (the upstream APIs
    do not always expose them). Spent tokens are aggregated from
    ``AIRequestLog.tokens`` for logs with ``sent_at >= issued_at``; attribution
    per provider is approximate until a per-model/per-provider token field is
    introduced.
    """

    label = models.CharField(
        max_length=128,
        unique=True,
        help_text="Provider/account name, e.g. «SambaNova» or «DeepSeek».",
    )
    total_limit = models.PositiveBigIntegerField(help_text="Total token allowance.")
    issued_at = models.DateField(help_text="Date the allowance was issued/reset.")
    notes = models.TextField(blank=True, default="")

    class Meta:
        db_table = "ai_ai_model_token_budget"
        verbose_name = "AI model token budget"
        verbose_name_plural = "AI model token budgets"
        ordering = ("label",)

    def __str__(self):
        return f"{self.label} ({self.total_limit} от {self.issued_at})"


class AIModelTestRun(models.Model):
    """A persisted ARM multi-model run.

    The in-memory job dict in ``ai/arm_runner.py`` is still used for live
    progress, but this model is the source of truth for completed runs and
    powers the per-model / per-topic summary tables. One run corresponds to one
    prompt sent to one or more models (a batch-over-tasks runner would create
    one run per task and aggregate across runs).
    """

    STATUS_RUNNING = "running"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"

    STATUS_CHOICES = (
        (STATUS_RUNNING, "Running"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_FAILED, "Failed"),
    )

    run_id = models.CharField(max_length=64, unique=True, db_index=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="ai_model_test_runs",
    )
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_RUNNING)
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
    message = models.TextField(blank=True, default="")
    error_message = models.TextField(blank=True, default="")
    report = models.JSONField(default=dict, blank=True)
    total_models = models.PositiveSmallIntegerField(default=0)
    # Context selected by the user (mirrors AIRequestLog context fields).
    programming_language_id = models.IntegerField(null=True, blank=True)
    programming_language_name = models.CharField(max_length=255, blank=True, default="")
    topic_id = models.IntegerField(null=True, blank=True)
    topic_name = models.CharField(max_length=255, blank=True, default="")
    prompt_id = models.IntegerField(null=True, blank=True)
    prompt_name = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        db_table = "ai_ai_model_test_run"
        verbose_name = "AI model test run"
        verbose_name_plural = "AI model test runs"
        ordering = ("-started_at",)

    def __str__(self):
        return f"{self.run_id} ({self.status})"


class AIModelTestResult(models.Model):
    """Per-model result row within an `AIModelTestRun`.

    `status` is "ok"/"error" (matches the in-memory result_item shape); this is
    what the ARM summary table aggregates (percent solved, average response
    time) across runs.
    """

    STATUS_OK = "ok"
    STATUS_ERROR = "error"

    STATUS_CHOICES = (
        (STATUS_OK, "OK"),
        (STATUS_ERROR, "Error"),
    )

    run = models.ForeignKey(
        AIModelTestRun,
        on_delete=models.CASCADE,
        related_name="results",
    )
    model_key = models.CharField(max_length=128, db_index=True)
    model_title = models.CharField(max_length=255)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_OK)
    duration_seconds = models.FloatField(null=True, blank=True)
    tokens = models.PositiveIntegerField(null=True, blank=True)
    short_response = models.TextField(blank=True, default="")
    raw_response = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "ai_ai_model_test_result"
        verbose_name = "AI model test result"
        verbose_name_plural = "AI model test results"
        ordering = ("model_title",)
        constraints = [
            models.UniqueConstraint(
                fields=("run", "model_key"),
                name="ai_model_test_result_run_model_uniq",
            ),
        ]

    def __str__(self):
        return f"{self.run.run_id} / {self.model_title} — {self.status}"
