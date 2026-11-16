# src/ai/db/AIUserModel.py

from django.conf import settings
from django.db import models

class AIUserModel(models.Model):
    """Личная AI-модель пользователя (провайдер + модель + свои ключи).

    Пользователь сам приносит провайдера и модель (OpenAI-совместимый
    endpoint). По умолчанию модель доступна ТОЛЬКО владельцу: чужие
    пользователи её не видят (селектор моделей), не могут вызвать (проверка
    доступа в ai/services/user_models.py) и не могут отредактировать.
    Владелец может открыть доступ адресно (``shared_with``) или всем
    пользователям сразу (``is_public``) — но только на пользовательских
    страницах, не в ARM.

    Безопасность:
    - ``base_url`` — только https, без приватных/локальных адресов (SSRF-защита
      проверяется в ai/services/user_models.py::validate_base_url);
    - ``api_key`` хранится шифром (Fernet, ключ — ``AI_USER_MODEL_SECRET_KEY``
      в .env); в открытом виде не выводится никогда;
    - URL-путь и host валидируются, редиректы не следуются.

    Поля model_key НЕ попадают в глобальный реестр моделей
    (ai/model_clients/registry.py) — поэтому такие модели не участвуют в
    health-check, ARM-прогонах и общей статистике.
    """

    PROVIDER_OPENAI = "openai"
    PROVIDER_ANTHROPIC = "anthropic"
    PROVIDER_OLLAMA = "ollama"
    PROVIDER_GROQ = "groq"
    PROVIDER_OPENROUTER = "openrouter"
    PROVIDER_SAMBANOVA = "sambanova"
    PROVIDER_CUSTOM = "custom"

    PROVIDER_CHOICES = (
        (PROVIDER_OPENAI, "OpenAI"),
        (PROVIDER_ANTHROPIC, "Anthropic"),
        (PROVIDER_OLLAMA, "Ollama"),
        (PROVIDER_GROQ, "Groq"),
        (PROVIDER_OPENROUTER, "OpenRouter"),
        (PROVIDER_SAMBANOVA, "SambaNova"),
        (PROVIDER_CUSTOM, "Другой (OpenAI-совместимый)"),
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="ai_user_models",
        verbose_name="Владелец",
    )
    title = models.CharField(max_length=255, verbose_name="Название модели")
    provider = models.CharField(
        max_length=32, choices=PROVIDER_CHOICES, default=PROVIDER_OPENAI,
        verbose_name="Провайдер",
    )
    model_id = models.CharField(max_length=255, verbose_name="ID модели у провайдера")
    base_url = models.CharField(
        max_length=500, blank=True, default="",
        verbose_name="Базовый URL (для совместимых провайдеров)",
    )
    # Шифротекст ключа (Fernet). Пусто = без ключа (локальный endpoint).
    api_key_encrypted = models.TextField(blank=True, default="", verbose_name="Ключ (зашифрован)")
    max_tokens = models.PositiveIntegerField(default=8000, verbose_name="Потолок ответа")
    supports_vision = models.BooleanField(default=False, verbose_name="Умеет картинки")
    supports_think = models.BooleanField(default=False, verbose_name="Умеет think")
    is_active = models.BooleanField(default=True, verbose_name="Активна")
    is_public = models.BooleanField(
        default=False,
        verbose_name="Доступна всем",
        help_text="Видна и доступна всем пользователям на страницах чата, "
                  "«Реши задачу» и «В чём ошибка?» (в ARM не попадает).",
    )
    shared_with = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        related_name="ai_shared_user_models",
        verbose_name="Пользователи с доступом",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Добавлена")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Изменена")

    class Meta:
        db_table = "ai_ai_user_model"
        verbose_name = "Личная AI-модель"
        verbose_name_plural = "Личные AI-модели"
        ordering = ("title",)
        indexes = [
            # Селектор моделей на каждой странице: «свои активные» и
            # «публичные активные» — две выборки по этим предикатам.
            models.Index(fields=["user", "is_active"], name="ai_user_model_owner_idx"),
            models.Index(fields=["is_public", "is_active"], name="ai_user_model_public_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.title} ({self.model_id}) — {self.user_id}"

