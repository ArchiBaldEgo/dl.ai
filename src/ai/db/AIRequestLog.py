# src/ai/db/AIRequestLog.py

from django.conf import settings
from django.db import models
from django.db.models.functions import Upper
from django.contrib.postgres.indexes import GinIndex, OpClass

class AIRequestLog(models.Model):
    """Журнал запросов к AI-моделям через WebSocket или ARM.

    Фиксирует кто, когда, в каком режиме (chat/solve/find_error/arm),
    к какой модели обращался, сколько токенов потрачено, какой получен ответ
    и контекст запроса (язык программирования, тема, промпт, DL-задача).
    Используется для отчётов и отладки.
    """
    STATUS_SUCCESS = "success"
    STATUS_ERROR = "error"

    STATUS_CHOICES = (
        (STATUS_SUCCESS, "Success"),
        (STATUS_ERROR, "Error"),
    )

    SOURCE_WEBSOCKET = "websocket"
    SOURCE_ARM = "arm"
    SOURCE_HTTP = "http"

    SOURCE_CHOICES = (
        (SOURCE_WEBSOCKET, "WebSocket"),
        (SOURCE_ARM, "ARM"),
        (SOURCE_HTTP, "HTTP"),
    )

    MODE_CHAT = "chat"
    MODE_SOLVE = "solve"
    MODE_FIND_ERROR = "find_error"
    MODE_ARM = "arm"
    MODE_BATCH_SOLVE = "batch_solve"
    MODE_PROMPT_DEBUG = "prompt_debug"
    MODE_TESTING = "testing"

    MODE_CHOICES = (
        (MODE_CHAT, "Чат"),
        (MODE_SOLVE, "Решить задачу"),
        (MODE_FIND_ERROR, "Найти ошибку"),
        (MODE_ARM, "ARM"),
        (MODE_BATCH_SOLVE, "Пакетное решение"),
        (MODE_PROMPT_DEBUG, "Отладка промтов"),
        (MODE_TESTING, "Тестирование"),
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="ai_request_logs",
        verbose_name="Пользователь",
    )
    external_user_id = models.CharField(max_length=255, blank=True, db_index=True, verbose_name="ID пользователя dl.gsu.by")
    username = models.CharField(max_length=255, blank=True, verbose_name="Логин")
    user_full_name = models.CharField(max_length=500, blank=True, verbose_name="ФИО")
    client_id = models.CharField(max_length=255, blank=True, verbose_name="ID клиента")
    source = models.CharField(max_length=32, choices=SOURCE_CHOICES, default=SOURCE_WEBSOCKET, verbose_name="Источник")
    mode = models.CharField(max_length=16, choices=MODE_CHOICES, blank=True, default="", verbose_name="Режим")
    sent_at = models.DateTimeField(verbose_name="Отправлен")
    received_at = models.DateTimeField(null=True, blank=True, verbose_name="Получен")
    duration_seconds = models.FloatField(null=True, blank=True, verbose_name="Время ответа, с")
    model_names = models.JSONField(default=list, blank=True, verbose_name="Модели")
    message = models.TextField(blank=True, verbose_name="Запрос")
    response_text = models.TextField(blank=True, verbose_name="Ответ модели")
    tokens = models.PositiveIntegerField(null=True, blank=True, verbose_name="Токенов")
    # Токены ПРОМПТА (вход). «tokens» — completion (ответ модели). Итог =
    # tokens + tokens_prompt (см. ai/services/token_usage.py). Null = провайдер
    # не отдал счётчик (тогда честно 0, а не выдуманное число).
    tokens_prompt = models.PositiveIntegerField(null=True, blank=True, verbose_name="Токенов (промпт)")
    # ID batch-прогона для записей пакетного решения (индексированная замена
    # парсинга «Batch solve run <id>» из текста сообщения).
    batch_run_id = models.CharField(max_length=64, blank=True, default="", db_index=True, verbose_name="ID пакетного прогона")
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_ERROR, verbose_name="Статус")
    error_message = models.TextField(blank=True, verbose_name="Текст ошибки")

    # Context selected by the user (programming task pages and ARM)
    programming_language_id = models.IntegerField(null=True, blank=True, verbose_name="ID языка")
    programming_language_name = models.CharField(max_length=255, blank=True, verbose_name="Язык программирования")
    topic_id = models.IntegerField(null=True, blank=True, verbose_name="ID темы")
    topic_name = models.CharField(max_length=255, blank=True, verbose_name="Тема")
    prompt_id = models.IntegerField(null=True, blank=True, verbose_name="ID промпта")
    prompt_name = models.CharField(max_length=255, blank=True, verbose_name="Промпт")

    # DL task context (for solve / find-error modes with a DL task)
    task_node_id = models.PositiveIntegerField(null=True, blank=True, db_index=True, verbose_name="ID задачи DL")
    task_name = models.CharField(max_length=512, blank=True, default="", verbose_name="Задача")

    class Meta:
        db_table = "ai_airequestlog"
        verbose_name = "Журнал запросов к ИИ"
        verbose_name_plural = "Журнал запросов к ИИ"
        ordering = ("-sent_at",)
        indexes = [
            # Журнал растёт быстро, а читается всегда «по времени» (Meta.ordering
            # = -sent_at) и с фильтрами. Без индексов каждая загрузка журнала/
            # дневного отчёта/контроля токенов делала сортировку по всей
            # таблице. Составные индексы покрывают типовые фильтры
            # (режим / источник / статус) + срез по дате.
            models.Index(fields=["sent_at"], name="ai_request_log_sent_idx"),
            models.Index(fields=["mode", "sent_at"], name="ai_request_log_mode_sent_idx"),
            models.Index(fields=["source", "sent_at"], name="ai_request_log_source_sent_idx"),
            models.Index(fields=["status", "sent_at"], name="ai_request_log_status_sent_idx"),
            # Поиск по фамилии/логину/id — expression-индексы на UPPER(col)
            # с триграммами: Django генерирует для icontains предикат
            # UPPER(col::text) LIKE UPPER(%p%), и обычный индекс по колонке
            # его НЕ обслуживает. Postgres-специфичные (gin_trgm_ops) —
            # в БД создаёт миграция 0069 (на SQLite индексы не создаются).
            GinIndex(
                OpClass(Upper("user_full_name"), "gin_trgm_ops"),
                name="ai_log_uname_trgm_idx",
            ),
            GinIndex(
                OpClass(Upper("username"), "gin_trgm_ops"),
                name="ai_log_login_trgm_idx",
            ),
            GinIndex(
                OpClass(Upper("external_user_id"), "gin_trgm_ops"),
                name="ai_log_extid_trgm_idx",
            ),
            # Топ-модели пользователя (ai/views.py::_get_user_top_model_keys):
            # status='success' + user + срез по sent_at + ORDER BY -sent_at
            # LIMIT 200. Без составного индекса — скан всей истории юзера.
            models.Index(
                fields=["user", "status", "sent_at"],
                name="ai_log_user_status_sent_idx",
            ),
            # Фильтр журнала по модели (`model_names__contains=[model]`,
            # jsonb @> …): без GIN это seq-scan по всей таблице.
            GinIndex(
                OpClass("model_names", name="jsonb_path_ops"),
                name="ai_log_model_names_gin_idx",
            ),
        ]

    def __str__(self):
        return f"{self.sent_at} — {self.user_full_name or self.username or self.external_user_id}"

