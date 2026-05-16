from django.conf import settings
from django.db import models
from django.utils import timezone


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
    programming_language = models.ForeignKey(ProgrammingLanguage, on_delete=models.CASCADE, null = True)  # Добавляем связь с языком программирования

    def __str__(self):
        return self.topic_name
    

# Модель препромпта
class Prompt(models.Model):
    topic = models.ForeignKey(Topic, on_delete=models.CASCADE, null = True)
    prompt_text = models.TextField()
    prompt_name = models.CharField(max_length=255, null = True)
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

    def __str__(self):
        # Возвращаем только имя промпта вместо полного текста
        return self.prompt_name if self.prompt_name else f"Prompt #{self.id}"    


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
