from django.conf import settings
from django.db import models
from django.utils import timezone

from .i18n import get_localized_name, get_localized_text

"""Модели данных AI-приложения.

Содержит модели для:
- Промптов (SharedPrompt, Prompt) с поддержкой мультиязычности и плейсхолдеров.
- Тем и языков программирования (Topic, ProgrammingLanguage).
- Задач dl.gsu.by (Task) для batch-solve ARM.
- Журнала запросов к AI-моделям (AIRequestLog).
- Проверки доступности моделей (AIModelHealthRun, AIModelAvailability).
- Результатов тестирования моделей (AIModelTestRun, AIModelTestResult).
- Регрессионных тестов промптов (PromptTestCase, PromptTestRun, PromptTestResult).
- Глобальных настроек приложения (AIAppSettings).
- Связи пользователей с внешними аккаунтами dl.gsu.by (ExternalDLAccount).
"""


def replace_placeholders(base, language="", topic="", message="", code=""):
    """Подстановка плейсхолдеров в текст промпта.

    Заменяет {language}/{язык} на имя языка программирования,
    {topic}/{тема} на название темы, {message} на сообщение пользователя,
    {code} на код для анализа. Используется и для Prompt, и для SharedPrompt.
    """
    # Всегда заменяем плейсхолдеры, даже пустыми значениями — иначе в тексте
    # промпта остаются буквальные {topic}/{language}, которые сбивают модель.
    base = base.replace("{language}", language or "")
    base = base.replace("{язык}", language or "")
    base = base.replace("{topic}", topic or "")
    base = base.replace("{тема}", topic or "")
    if "{message}" in base:
        base = base.replace("{message}", message or "")
    if "{code}" in base:
        base = base.replace("{code}", code or "")
    return base


class ExternalDLAccount(models.Model):
    """Link between Django User and external DL (dl.gsu.by) account."""
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='external_dl_account',
        verbose_name="Пользователь",
    )
    external_user_id = models.CharField(
        max_length=255,
        unique=True,
        db_index=True,
        help_text="ID пользователя из API dl.gsu.by",
        verbose_name="ID пользователя dl.gsu.by",
    )
    external_login = models.CharField(
        max_length=255,
        help_text="Последний известный логин dl.gsu.by",
        verbose_name="Логин dl.gsu.by",
    )
    external_first_name = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Имя из dl.gsu.by",
        verbose_name="Имя",
    )
    external_last_name = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Фамилия из dl.gsu.by",
        verbose_name="Фамилия",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Создан")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Обновлён")

    class Meta:
        verbose_name = "Внешняя учётная запись DL"
        verbose_name_plural = "Внешние учётные записи DL"

    def __str__(self):
        return f"{self.user.username} (DL: {self.external_login})"


class ProgrammingLanguage(models.Model):
    """Язык программирования, используемый в темах и задачах.

    Связан с Topic (один ко многим) и используется для подстановки плейсхолдера
    {language} в тексты промптов.
    """
    language_name = models.CharField(max_length=255, verbose_name="Название")

    class Meta:
        verbose_name = "Язык программирования"
        verbose_name_plural = "Языки программирования"

    def __str__(self):
        return self.language_name


class Topic(models.Model):
    """Тема (раздел) в рамках языка программирования.

    Поддерживает мультиязычные названия (ru/en/fr). Используется для подстановки
    плейсхолдера {topic} в промпты и для группировки задач в ARM-отчётах.
    """
    topic_name = models.CharField(max_length=255, verbose_name="Название")
    topic_name_ru = models.CharField(max_length=255, blank=True, default="", verbose_name="Название (RU)")
    topic_name_en = models.CharField(max_length=255, blank=True, default="", verbose_name="Название (EN)")
    topic_name_fr = models.CharField(max_length=255, blank=True, default="", verbose_name="Название (FR)")
    programming_language = models.ForeignKey(
        ProgrammingLanguage, on_delete=models.CASCADE, null=True,
        verbose_name="Язык программирования",
    )  # Добавляем связь с языком программирования

    class Meta:
        verbose_name = "Тема"
        verbose_name_plural = "Темы"

    def __str__(self):
        return get_localized_name(self, "", "topic_name")


class Task(models.Model):
    """Локальная ссылка на задачу dl.gsu.by для batch-solve ARM.

    Оператор вводит DL ``node_id`` (уникальный идентификатор узла задачи на
    dl.gsu.by); название и условие тянутся из внешнего API через
    ``fetch_task_info`` (action ``refresh_from_dl`` в админке). Тему и язык
    программирования оператор назначает локально — они нужны для подстановки в
    solve-промпт и для группировки отчёта по темам. ``file_extension`` задаётся
    вручную (например ``.pas``/``.cpp``/``.py``), т.к. из локального
    ``ProgrammingLanguage`` (отображаемое имя) его не вывести, а он требуется для
    ``fetch_task_solution``.
    """

    node_id = models.PositiveIntegerField(
        unique=True, db_index=True, verbose_name="ID узла DL",
        help_text="Идентификатор узла задачи на dl.gsu.by (nodeId).",
    )
    task_id = models.PositiveIntegerField(
        null=True, blank=True, db_index=True, verbose_name="ID задачи DL",
        help_text="Заполняется из get-task-info (поле taskId).",
    )
    name = models.CharField(max_length=512, blank=True, default="", verbose_name="Название")
    statement = models.TextField(blank=True, default="", verbose_name="Условие")
    topic = models.ForeignKey(
        Topic, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="tasks", verbose_name="Тема",
    )
    programming_language = models.ForeignKey(
        ProgrammingLanguage, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="tasks", verbose_name="Язык программирования",
    )
    file_extension = models.CharField(
        max_length=16, blank=True, default="", verbose_name="Расширение файла",
        help_text="Например .pas, .cpp, .py — используется для get-solution.",
    )
    active = models.BooleanField(default=True, db_index=True, verbose_name="Активна")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Создан")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Обновлён")

    class Meta:
        db_table = "ai_task"
        verbose_name = "Задача (DL)"
        verbose_name_plural = "Задачи (DL)"
        ordering = ("-created_at",)

    def __str__(self):
        return self.name or f"DL #{self.node_id}"


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
    """Общий (shared) препромпт — не привязан к конкретному языку программирования или теме.

    Текст может содержать плейсхолдеры {language}/{язык} и {topic}/{тема},
    которые заменяются на имя языка и название темы при использовании.
    Если указан mode (chat/solve/find_error), препромпт используется как
    системный шаблон по умолчанию для соответствующего режима.
    """
    prompt_name = models.CharField(max_length=255, verbose_name="Название")
    prompt_name_ru = models.CharField(max_length=255, blank=True, default="", verbose_name="Название (RU)")
    prompt_name_en = models.CharField(max_length=255, blank=True, default="", verbose_name="Название (EN)")
    prompt_name_fr = models.CharField(max_length=255, blank=True, default="", verbose_name="Название (FR)")
    prompt_text = models.TextField(
        help_text="Доступные плейсхолдеры: {language}/{язык} - язык программирования, {topic}/{тема} - тема.",
        verbose_name="Текст",
    )
    prompt_text_ru = models.TextField(blank=True, default="", verbose_name="Текст (RU)")
    prompt_text_en = models.TextField(blank=True, default="", verbose_name="Текст (EN)")
    prompt_text_fr = models.TextField(blank=True, default="", verbose_name="Текст (FR)")
    # Языки, для которых этот общий препромпт доступен (blank = для всех)
    programming_languages = models.ManyToManyField(
        ProgrammingLanguage, blank=True, related_name="shared_prompts",
        verbose_name="Языки программирования",
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
        from .i18n import get_shared_prompt_prefix
        prefix = get_shared_prompt_prefix('')
        name = get_localized_name(self, '', 'prompt_name')
        return f"{prefix} {name}"

    def get_effective_text(self, ui_language="", programming_language_name="", topic_name="", message="", code=""):
        base = get_localized_text(self, ui_language, "prompt_text") or self.prompt_text
        return replace_placeholders(base, programming_language_name, topic_name, message, code)

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
    """Промпт, привязанный к конкретной теме.

    Может ссылаться на SharedPrompt (shared_prompt) — в этом случае итоговый текст
    берётся из общего препромпта с подстановкой языка и темы. Если задан
    prompt_text_override, он переопределяет текст общего препромпта.
    Поддерживает мультиязычные названия и тексты (ru/en/fr).
    """
    topic = models.ForeignKey(
        Topic, on_delete=models.CASCADE, null=True, blank=True,
        verbose_name="Тема",
    )
    prompt_text = models.TextField(verbose_name="Текст")
    prompt_text_ru = models.TextField(blank=True, default="", verbose_name="Текст (RU)")
    prompt_text_en = models.TextField(blank=True, default="", verbose_name="Текст (EN)")
    prompt_text_fr = models.TextField(blank=True, default="", verbose_name="Текст (FR)")
    prompt_name = models.CharField(max_length=255, null=True, verbose_name="Название")
    prompt_name_ru = models.CharField(max_length=255, blank=True, default="", verbose_name="Название (RU)")
    prompt_name_en = models.CharField(max_length=255, blank=True, default="", verbose_name="Название (EN)")
    prompt_name_fr = models.CharField(max_length=255, blank=True, default="", verbose_name="Название (FR)")
    # Ссылка на общий препромпт (если есть - текст берётся из него с подстановкой языка и темы)
    shared_prompt = models.ForeignKey(
        SharedPrompt, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="language_prompts",
        verbose_name="Общий препромпт",
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
        """Возвращает итоговый текст препромпта с учётом UI-языка, языка программирования и темы."""
        if self.prompt_text_override:
            base = self.prompt_text_override
        elif self.shared_prompt:
            base = self.shared_prompt.get_effective_text(ui_language, programming_language_name, topic_name, message, code)
        else:
            base = get_localized_text(self, ui_language, "prompt_text") or self.prompt_text
        return replace_placeholders(base, programming_language_name, topic_name, message, code)

    def __str__(self):
        # Возвращаем локализованное имя промпта вместо полного текста
        name = get_localized_name(self, "", "prompt_name")
        return name if name else f"Prompt #{self.id}"

    class Meta:
        db_table = 'ai_prompt'
        verbose_name = "Промпт"
        verbose_name_plural = "Промпты"


class ArmPromptBinding(models.Model):
    """Привязка «препромпт по умолчанию» для ARM: (язык, тема, вид ARM) → промпт.

    Суперюзерский инструмент «Препромпты по умолчанию»: на /arm/solve/ и
    /arm/find-error/ после выбора темы препромпт подтягивается автоматически,
    чтобы пользователю не приходилось выбирать один и тот же препромпт каждый
    раз. Промпт редактируется по обычным правилам (владелец/редактор/
    суперюзер) — привязка тянет актуальный текст по FK, ничего не копируем.
    """
    MODE_SOLVE = "solve"
    MODE_FIND_ERROR = "find_error"
    ARM_MODE_CHOICES = [
        (MODE_SOLVE, "Пакетное решение"),
        (MODE_FIND_ERROR, "Поиск ошибки"),
    ]

    programming_language = models.ForeignKey(
        ProgrammingLanguage, on_delete=models.CASCADE,
        related_name="arm_prompt_bindings",
        verbose_name="Язык программирования",
    )
    topic = models.ForeignKey(
        Topic, on_delete=models.CASCADE,
        related_name="arm_prompt_bindings",
        verbose_name="Тема",
    )
    mode = models.CharField(max_length=16, choices=ARM_MODE_CHOICES, verbose_name="Вид ARM")
    prompt = models.ForeignKey(
        Prompt, on_delete=models.CASCADE,
        related_name="arm_bindings",
        verbose_name="Препромпт",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Создана")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Обновлена")

    class Meta:
        db_table = "ai_arm_prompt_binding"
        verbose_name = "Привязка препромпта ARM"
        verbose_name_plural = "Привязки препромптов ARM"
        constraints = [
            models.UniqueConstraint(
                fields=["programming_language", "topic", "mode"],
                name="unique_arm_prompt_binding",
            ),
        ]

    def __str__(self):
        return f"{self.get_mode_display()}: {self.programming_language} / {self.topic} → {self.prompt}"


class AIAppSettings(models.Model):
    """Глобальная настройка приложения AI (singleton-модель).

    Хранит флаг включения/выключения AI-функциональности.
    Модель имеет фиксированный pk=1 — только одна строка в таблице.
    """
    is_enabled = models.BooleanField(default=True, verbose_name="Включено")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Обновлён")
    # Дата-отсечка для счётчика «фаворитов» (топ-2 моделей по частоте использования).
    # Успешные AIRequestLog старше этой даты не учитываются при ранжировании,
    # что даёт разовый сброс фаворитов для всех пользователей (нет записей новее
    # epoch → строгий алфавит) без удаления логов; новые запросы снова набирают
    # топ-2. См. ai/views.py::_get_user_top_model_keys и management-команду
    # reset_favorites_epoch.
    favorites_epoch = models.DateTimeField(
        null=True, blank=True, default=timezone.now,
        verbose_name="Дата отсечки фаворитов",
    )

    class Meta:
        verbose_name = "Настройки ИИ-приложения"
        verbose_name_plural = "Настройки ИИ-приложения"

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
    """Один прогон проверки доступности всех AI-моделей за конкретную дату.

    Запускается планировщиком в ai/model_health.py (ежедневно в 04:00 МСК).
    Содержит сводный статус и временные метки начала/окончания.
    """
    STATUS_RUNNING = "running"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"

    STATUS_CHOICES = (
        (STATUS_RUNNING, "Running"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_FAILED, "Failed"),
    )

    window_date = models.DateField(unique=True, verbose_name="Окно проверки")
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_RUNNING, verbose_name="Статус")
    started_at = models.DateTimeField(default=timezone.now, verbose_name="Начат")
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name="Завершён")
    error_message = models.TextField(blank=True, default="", verbose_name="Текст ошибки")

    class Meta:
        verbose_name = "Прогон проверки моделей"
        verbose_name_plural = "Прогоны проверки моделей"
        ordering = ("-window_date",)

    def __str__(self):
        return f"{self.window_date} ({self.status})"


class AIModelAvailability(models.Model):
    """Запись о доступности конкретной AI-модели за определённую дату.

    Создаётся в рамках AIModelHealthRun. Хранит флаг доступности, время отклика,
    HTTP-код последней проверки и сообщение об ошибке (если есть).
    Одна запись на (model_key, window_date).
    """
    model_key = models.CharField(max_length=128, db_index=True, verbose_name="Ключ модели")
    model_title = models.CharField(max_length=255, verbose_name="Модель")
    is_available = models.BooleanField(default=False, verbose_name="Доступна")
    window_date = models.DateField(db_index=True, verbose_name="Окно проверки")
    checked_at = models.DateTimeField(auto_now=True, verbose_name="Проверена")
    response_time_ms = models.PositiveIntegerField(null=True, blank=True, verbose_name="Время ответа, мс")
    last_http_code = models.PositiveSmallIntegerField(null=True, blank=True, verbose_name="Код ответа")
    last_message = models.TextField(blank=True, default="", verbose_name="Сообщение")

    class Meta:
        verbose_name = "Доступность модели"
        verbose_name_plural = "Доступность моделей"
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

    SOURCE_CHOICES = (
        (SOURCE_WEBSOCKET, "WebSocket"),
        (SOURCE_ARM, "ARM"),
    )

    MODE_CHAT = "chat"
    MODE_SOLVE = "solve"
    MODE_FIND_ERROR = "find_error"
    MODE_ARM = "arm"
    MODE_BATCH_SOLVE = "batch_solve"

    MODE_CHOICES = (
        (MODE_CHAT, "Чат"),
        (MODE_SOLVE, "Решить задачу"),
        (MODE_FIND_ERROR, "Найти ошибку"),
        (MODE_ARM, "ARM"),
        (MODE_BATCH_SOLVE, "Пакетное решение"),
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

    def __str__(self):
        return f"{self.sent_at} — {self.user_full_name or self.username or self.external_user_id}"


class AIModelTestRun(models.Model):
    """A persisted ARM multi-model run.

    The in-memory job dict in ``ai/arm_runner.py`` is still used for live
    progress, but this model is the source of truth for completed runs and
    powers the per-model / per-topic summary tables. ``run_type`` distinguishes
    the single-prompt find-error runner (``single``) from the batch-over-tasks
    solver (``batch``); the latter keeps one run for many (task, model) pairs and
    stores the per-task topic/language snapshot on each ``AIModelTestResult``.
    ``run_params`` — снимок параметров формы на момент запуска (batch: node_ids,
    model_keys, file_extension, prompt_id, dl_test, ui_language, course_id;
    single: model_keys, interface_language, programming_language, topic, prompt,
    task_text, code_text) — для восстановления состояния формы при возврате
    на страницу прогона (``?run_id=``).
    """

    STATUS_RUNNING = "running"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"
    STATUS_CANCELLED = "cancelled"

    STATUS_CHOICES = (
        (STATUS_RUNNING, "Running"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_FAILED, "Failed"),
        (STATUS_CANCELLED, "Cancelled"),
    )

    RUN_TYPE_SINGLE = "single"
    RUN_TYPE_BATCH = "batch"

    RUN_TYPE_CHOICES = (
        (RUN_TYPE_SINGLE, "Single (find-error)"),
        (RUN_TYPE_BATCH, "Batch (solve)"),
    )

    run_id = models.CharField(max_length=64, unique=True, db_index=True, verbose_name="ID прогона")
    run_type = models.CharField(
        max_length=16, choices=RUN_TYPE_CHOICES, default=RUN_TYPE_SINGLE, db_index=True,
        verbose_name="Тип прогона",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="ai_model_test_runs",
        verbose_name="Пользователь",
    )
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_RUNNING, verbose_name="Статус")
    started_at = models.DateTimeField(default=timezone.now, verbose_name="Начат")
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name="Завершён")
    message = models.TextField(blank=True, default="", verbose_name="Сообщение")
    error_message = models.TextField(blank=True, default="", verbose_name="Текст ошибки")
    report = models.JSONField(default=dict, blank=True, verbose_name="Отчёт")
    total_models = models.PositiveSmallIntegerField(default=0, verbose_name="Всего моделей")
    # Context selected by the user (mirrors AIRequestLog context fields).
    programming_language_id = models.IntegerField(null=True, blank=True, verbose_name="ID языка")
    programming_language_name = models.CharField(max_length=255, blank=True, default="", verbose_name="Язык программирования")
    topic_id = models.IntegerField(null=True, blank=True, verbose_name="ID темы")
    topic_name = models.CharField(max_length=255, blank=True, default="", verbose_name="Тема")
    prompt_id = models.IntegerField(null=True, blank=True, verbose_name="ID промпта")
    prompt_name = models.CharField(max_length=255, blank=True, default="", verbose_name="Промпт")
    # ID курса DL, по которому загружалось дерево задач для batch-прогона.
    # Сохраняется для отображения в Журнале запросов (режим «Пакетное решение»,
    # поле «id дерева») и для проброса в send-solution как courseId.
    course_id = models.IntegerField(null=True, blank=True, verbose_name="ID курса DL")
    # Снимок параметров формы на момент запуска — для восстановления состояния
    # формы при возврате на страницу прогона (?run_id=). См. докстринг модели.
    run_params = models.JSONField(default=dict, blank=True, verbose_name="Параметры запуска (форма)")

    class Meta:
        db_table = "ai_ai_model_test_run"
        verbose_name = "Прогон тестирования модели"
        verbose_name_plural = "Прогоны тестирования модели"
        ordering = ("-started_at",)

    def __str__(self):
        return f"{self.run_id} ({self.status})"


class AIModelTestResult(models.Model):
    """Per-model result row within an `AIModelTestRun`.

    `status` is "ok"/"error" (matches the in-memory result_item shape); this is
    what the ARM summary table aggregates (percent solved, average response
    time) across runs. For batch-solve runs (`run.run_type == "batch"`) `task`
    links the row to a `Task`, `verdict` is the grading result
    ("solved"/"failed"/"skipped"), and the `*_snapshot` fields freeze the task's
    topic/programming-language at run time (the operator may reassign them
    later). `verdict` is NULL for legacy single find-error rows.
    """

    STATUS_OK = "ok"
    STATUS_ERROR = "error"

    STATUS_CHOICES = (
        (STATUS_OK, "OK"),
        (STATUS_ERROR, "Error"),
    )

    VERDICT_SOLVED = "solved"
    VERDICT_FAILED = "failed"
    VERDICT_SKIPPED = "skipped"

    VERDICT_CHOICES = (
        (VERDICT_SOLVED, "Решено"),
        (VERDICT_FAILED, "Не решено"),
        (VERDICT_SKIPPED, "Пропущено"),
    )

    run = models.ForeignKey(
        AIModelTestRun,
        on_delete=models.CASCADE,
        related_name="results",
        verbose_name="Прогон",
    )
    task = models.ForeignKey(
        Task,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="test_results",
        verbose_name="Задача",
    )
    model_key = models.CharField(max_length=128, db_index=True, verbose_name="Ключ модели")
    model_title = models.CharField(max_length=255, verbose_name="Модель")
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_OK, verbose_name="Статус")
    verdict = models.CharField(
        max_length=8, choices=VERDICT_CHOICES, null=True, blank=True, db_index=True,
        verbose_name="Вердикт",
    )
    duration_seconds = models.FloatField(null=True, blank=True, verbose_name="Время ответа, с")
    tokens = models.PositiveIntegerField(null=True, blank=True, verbose_name="Токенов")
    short_response = models.TextField(blank=True, default="", verbose_name="Краткий ответ")
    raw_response = models.TextField(blank=True, default="", verbose_name="Полный ответ")
    # Извлечённый чистый код модели (содержимое файла программы) для batch-solve.
    code = models.TextField(blank=True, default="", verbose_name="Код программы")
    # Итог DL-тестирования: полный comment из get-solution-result.
    dl_comment = models.TextField(blank=True, default="", verbose_name="DL: итог тестов")
    # Ошибка отправки/опроса DL (send-solution / get-solution-result).
    dl_error = models.TextField(blank=True, default="", verbose_name="DL: ошибка")
    # DL queue id из send-solution (0 — отправка не удалась).
    dl_queue_id = models.IntegerField(null=True, blank=True, verbose_name="DL: queueId")
    # Снимок расширения файла задачи (для имени файла скачивания).
    file_extension_snapshot = models.CharField(max_length=16, blank=True, default="", verbose_name="Расширение (снимок)")
    # Snapshot of the task's topic / programming language at run time (batch runs).
    topic_id_snapshot = models.IntegerField(null=True, blank=True, verbose_name="ID темы (снимок)")
    topic_name_snapshot = models.CharField(max_length=255, blank=True, default="", verbose_name="Тема (снимок)")
    prog_lang_snapshot = models.CharField(max_length=255, blank=True, default="", verbose_name="Язык программирования (снимок)")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Создан")

    class Meta:
        db_table = "ai_ai_model_test_result"
        verbose_name = "Результат тестирования модели"
        verbose_name_plural = "Результаты тестирования модели"
        ordering = ("model_title",)
        constraints = [
            # One row per (run, model, task) for batch runs.
            models.UniqueConstraint(
                fields=("run", "model_key", "task"),
                name="ai_model_test_result_run_model_task_uniq",
            ),
            # Legacy single find-error rows have task IS NULL — keep them unique
            # per (run, model_key). Postgres treats NULLs as distinct, so this
            # partial constraint only applies to the legacy shape.
            models.UniqueConstraint(
                fields=("run", "model_key"),
                condition=models.Q(task__isnull=True),
                name="ai_model_test_result_run_model_uniq",
            ),
        ]

    def __str__(self):
        return f"{self.run.run_id} / {self.model_title} — {self.status}"


# ---------------------------------------------------------------------------
# Prompt regression tests (golden-master suite).
#
# A `PromptTestCase` is a fixed fixture: an input (task statement / code /
# message) plus the expected (golden) model reaction. A `PromptTestRun` is one
# pass of a single model over a set of cases with a chosen prompt under test;
# `PromptTestResult` stores the per-case actual response and the deterministic
# comparison verdict against the golden text. This mirrors the ARM run/result
# pair but is keyed by (run, test_case) and driven by an editable prompt.
# ---------------------------------------------------------------------------

PROMPT_TEST_MODE_CHOICES = (
    ("solve", "Solve"),
    ("find_error", "Find error"),
    ("chat", "Chat"),
)

PROMPT_TEST_COMPARATOR_CHOICES = (
    ("ratio", "ratio (difflib)"),
    ("contains_all", "contains_all (все строки эталона)"),
    ("exact", "exact (нормализованное равенство)"),
    ("set", "set (равенство множеств строк)"),
)


class PromptTestCase(models.Model):
    """Один тест-кейс регрессионного набора промпта: ввод + эталон + компаратор.

    ``input_text`` — условие задачи (solve), код с ошибкой (find_error) или
    сообщение пользователя (chat). ``expected_text`` — образцовая реакция
    модели: решение / ошибки по одной на строку (contains_all) / ожидаемый
    ответ. ``comparator`` задаёт способ детерминированного сравнения (см.
    ``ai/grading.py``). Тема и язык программирования нужны для подстановки
    плейсхолдеров в тестируемый промпт.
    """

    name = models.CharField(max_length=255, verbose_name="Название")
    mode = models.CharField(
        max_length=16, choices=PROMPT_TEST_MODE_CHOICES, db_index=True,
        verbose_name="Режим",
    )
    programming_language = models.ForeignKey(
        ProgrammingLanguage, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="prompt_test_cases", verbose_name="Язык программирования",
    )
    topic = models.ForeignKey(
        Topic, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="prompt_test_cases", verbose_name="Тема",
    )
    input_text = models.TextField(verbose_name="Ввод (условие / код / сообщение)")
    expected_text = models.TextField(blank=True, default="", verbose_name="Эталон")
    comparator = models.CharField(
        max_length=16, choices=PROMPT_TEST_COMPARATOR_CHOICES, default="ratio",
        verbose_name="Компаратор",
    )
    match_threshold = models.FloatField(
        null=True, blank=True, verbose_name="Порог ratio",
        help_text="Для компаратора ratio (по умолчанию 0.85).",
    )
    ui_language = models.CharField(max_length=16, default="Русский", verbose_name="Язык интерфейса")
    active = models.BooleanField(default=True, db_index=True, verbose_name="Активен")
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="owned_prompt_test_cases", verbose_name="Владелец",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Создан")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Обновлён")

    class Meta:
        db_table = "ai_prompttestcase"
        verbose_name = "Тест-кейс промпта"
        verbose_name_plural = "Тест-кейсы промптов"
        ordering = ("-created_at",)

    def __str__(self):
        return self.name or f"Тест-кейс #{self.id}"


class PromptTestRun(models.Model):
    """Один прогон регрессионных тестов: одна модель × набор кейсов × промпт.

    Живой прогресс хранится in-memory в ``ai/prompt_test_runner.py``; эта модель
    — источник правды для завершённых/вытесненных прогонов и основа отчётов.
    ``run_params`` — снимок параметров формы на момент запуска (``case_ids``,
    фактически разрешённые id кейсов; пустой список = «все активные») — для
    восстановления состояния формы при возврате на страницу прогона (``?run_id=``).
    """

    STATUS_RUNNING = "running"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"

    STATUS_CHOICES = (
        (STATUS_RUNNING, "Running"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_FAILED, "Failed"),
    )

    run_id = models.CharField(max_length=64, unique=True, db_index=True, verbose_name="ID прогона")
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_RUNNING, db_index=True, verbose_name="Статус")
    model_key = models.CharField(max_length=128, db_index=True, verbose_name="Ключ модели")
    model_title = models.CharField(max_length=255, blank=True, default="", verbose_name="Модель")
    prompt_id = models.IntegerField(null=True, blank=True, db_index=True, verbose_name="ID промпта")
    prompt_name = models.CharField(max_length=255, blank=True, default="", verbose_name="Промпт")
    ui_language = models.CharField(max_length=16, default="Русский", verbose_name="Язык интерфейса")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="prompt_test_runs",
        verbose_name="Пользователь",
    )
    started_at = models.DateTimeField(default=timezone.now, verbose_name="Начат")
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name="Завершён")
    error_message = models.TextField(blank=True, default="", verbose_name="Текст ошибки")
    report = models.JSONField(default=dict, blank=True, verbose_name="Отчёт")
    total_cases = models.PositiveSmallIntegerField(default=0, verbose_name="Всего кейсов")
    # Снимок параметров формы на момент запуска — для восстановления состояния
    # формы при возврате на страницу прогона (?run_id=). См. докстринг модели.
    run_params = models.JSONField(default=dict, blank=True, verbose_name="Параметры запуска (форма)")

    class Meta:
        db_table = "ai_prompttest_run"
        verbose_name = "Прогон регрессионных тестов промпта"
        verbose_name_plural = "Прогоны регрессионных тестов промптов"
        ordering = ("-started_at",)

    def __str__(self):
        return f"{self.run_id} ({self.status})"


class PromptTestResult(models.Model):
    """Строка на (прогон × кейс): фактический ответ и verdict vs эталона."""

    STATUS_OK = "ok"
    STATUS_ERROR = "error"

    STATUS_CHOICES = (
        (STATUS_OK, "OK"),
        (STATUS_ERROR, "Error"),
    )

    VERDICT_MATCH = "match"
    VERDICT_MISMATCH = "mismatch"
    VERDICT_SKIPPED = "skipped"

    VERDICT_CHOICES = (
        (VERDICT_MATCH, "Совпадает"),
        (VERDICT_MISMATCH, "Отклонение"),
        (VERDICT_SKIPPED, "Пропущен"),
    )

    run = models.ForeignKey(
        PromptTestRun, on_delete=models.CASCADE, related_name="results",
        verbose_name="Прогон",
    )
    test_case = models.ForeignKey(
        PromptTestCase, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="test_results",
        verbose_name="Тест-кейс",
    )
    model_key = models.CharField(max_length=128, db_index=True, verbose_name="Ключ модели")
    model_title = models.CharField(max_length=255, blank=True, default="", verbose_name="Модель")
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_OK, verbose_name="Статус")
    verdict = models.CharField(
        max_length=16, choices=VERDICT_CHOICES, default=VERDICT_MISMATCH, db_index=True,
        verbose_name="Вердикт",
    )
    actual_response = models.TextField(blank=True, default="", verbose_name="Ответ")
    expected_snapshot = models.TextField(blank=True, default="", verbose_name="Эталон (снимок)")
    diff_hint = models.CharField(max_length=255, blank=True, default="", verbose_name="Подсказка различий")
    duration_seconds = models.FloatField(null=True, blank=True, verbose_name="Время ответа, с")
    tokens = models.PositiveIntegerField(null=True, blank=True, verbose_name="Токенов")
    # Snapshot of the case at run time (operator may reassign topic/lang later).
    case_name_snapshot = models.CharField(max_length=255, blank=True, default="", verbose_name="Название кейса (снимок)")
    mode_snapshot = models.CharField(max_length=16, blank=True, default="", verbose_name="Режим (снимок)")
    topic_name_snapshot = models.CharField(max_length=255, blank=True, default="", verbose_name="Тема (снимок)")
    prog_lang_snapshot = models.CharField(max_length=255, blank=True, default="", verbose_name="Язык программирования (снимок)")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Создан")

    class Meta:
        db_table = "ai_prompt_test_result"
        verbose_name = "Результат регрессионного теста промпта"
        verbose_name_plural = "Результаты регрессионных тестов промптов"
        ordering = ("case_name_snapshot",)
        constraints = [
            models.UniqueConstraint(
                fields=("run", "test_case"),
                name="ai_prompt_test_result_run_case_uniq",
            ),
        ]

    def __str__(self):
        return f"{self.run.run_id} / {self.case_name_snapshot or self.model_title} — {self.verdict}"


class UpdateLog(models.Model):
    """Журнал обновлений проекта.

    Записи добавляются перед git add/git push. Каждая запись — описание
    изменений на русском языке, дата и автор коммита.
    """
    commit_date = models.DateField(verbose_name="Дата")
    description = models.TextField(verbose_name="Содержание обновления")
    author = models.CharField(max_length=255, verbose_name="Автор")
    commit_hash = models.CharField(max_length=40, blank=True, default="", verbose_name="Хэш коммита")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Создан")

    class Meta:
        db_table = "ai_update_log"
        verbose_name = "Обновление"
        verbose_name_plural = "Обновления"
        ordering = ("-commit_date", "-created_at", "-id")

    def __str__(self):
        return f"{self.commit_date} — {self.author} — {self.description[:80]}"
