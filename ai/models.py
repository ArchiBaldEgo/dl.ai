from django.conf import settings
from django.contrib.postgres.indexes import GinIndex, OpClass
from django.db import models
from django.db.models.functions import Upper
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
    # Учебные данные студента из post/get-user-info (dl-rest-api.json): top-level
    # email + вложенный блок education. Обновляются при каждом входе
    # пользователя; массовый бэкфилл невозможен — endpoint требует sessionId
    # самого пользователя.
    dl_email = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Email из dl.gsu.by (get-user-info)",
        verbose_name="Email dl.gsu.by",
    )
    education_form = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Форма обучения (education.form)",
        verbose_name="Форма обучения",
    )
    education_school_id = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="ID школы (education.schoolId)",
        verbose_name="Школа: ID",
    )
    education_school_kind = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Тип школы (education.schoolKind)",
        verbose_name="Школа: тип",
    )
    education_school_no = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Номер школы (education.schoolNo)",
        verbose_name="Школа: номер",
    )
    education_group_mask_id = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Группа (education.groupMaskId)",
        verbose_name="Группа (groupMaskId)",
    )
    education_form_letter = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Класс/буква (education.formLetter)",
        verbose_name="Класс (буква)",
    )
    # Названия учебного заведения и группы присылает сам DL
    # (dl-rest-api.json, EducationInfo.schoolName/formName/groupName) —
    # с версии 1.4.2 REST API отдаёт их вместе с ID. Раньше названий в API
    # не было, поэтому их вели вручную в справочнике DLEducationName.
    education_school_name = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Название учебного заведения (education.schoolName)",
        verbose_name="Школа/вуз: название",
    )
    education_form_name = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Название класса/курса (education.formName)",
        verbose_name="Класс/курс: название",
    )
    education_group_name = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Название группы (education.groupName)",
        verbose_name="Группа: название",
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

    Мультиязычные названия (ru/en/fr); ``topic_name_ru`` — базовый вариант и
    fallback цепочки локализации (get_localized_name). Используется для
    подстановки плейсхолдера {topic} в промпты и для группировки задач в
    ARM-отчётах.
    """
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
    # Путь задачи в дереве задач DL (get-task-info → path; либо дерево
    # курса, закэшированное на форме /arm/solve/), напр.
    # «Программирование [Ассемблер i8086, C-MPA]\Одномерные массивы\Сумма цифр числа».
    # Нужен для честной подписи темы в отчёте пакетного решения: когда
    # локальная тема не определена, подписью служит папка DL, а не имя задачи.
    tree_path = models.CharField(
        max_length=512, blank=True, default="", verbose_name="Путь в дереве задач DL",
    )
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
        from .i18n import get_shared_prompt_prefix
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


class ArmPromptBinding(models.Model):
    """Привязка «промпт по умолчанию» для ARM: (язык, тема, вид ARM) → промпт.

    Суперюзерский инструмент «Промпты по умолчанию»: на /arm/solve/ и
    /arm/find-error/ после выбора темы промпт подтягивается автоматически,
    чтобы пользователю не приходилось выбирать один и тот же промпт каждый
    раз. Промпт редактируется по обычным правилам (владелец/редактор/
    суперюзер) — привязка тянет актуальный текст по FK, ничего не копируем.

    ``topic`` может быть NULL — привязка «на весь язык» (ЯП = формат: язык
    эквивалентен своему расширению, промпт по умолчанию на весь Python
    покрывает все .py-задачи). Нужно там, где у языка нет тем (Python, C++),
    и как fallback, когда точная привязка (язык+тема) не задана.
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
        Topic, on_delete=models.CASCADE, null=True, blank=True,
        related_name="arm_prompt_bindings",
        verbose_name="Тема",
    )
    mode = models.CharField(max_length=16, choices=ARM_MODE_CHOICES, verbose_name="Вид ARM")
    prompt = models.ForeignKey(
        Prompt, on_delete=models.CASCADE,
        related_name="arm_bindings",
        verbose_name="Промпт",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Создана")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Обновлена")

    class Meta:
        db_table = "ai_arm_prompt_binding"
        verbose_name = "Привязка промпта ARM"
        verbose_name_plural = "Привязки промптов ARM"
        constraints = [
            models.UniqueConstraint(
                fields=["programming_language", "topic", "mode"],
                name="unique_arm_prompt_binding",
            ),
            # Привязка «на весь язык»: одна на (язык, вид). Частичный индекс —
            # обычный UNIQUE не ловит дубликаты NULL-темы в Postgres.
            models.UniqueConstraint(
                fields=["programming_language", "mode"],
                condition=models.Q(topic__isnull=True),
                name="unique_arm_prompt_binding_language",
            ),
        ]

    @classmethod
    def resolve(cls, *, programming_language_id, topic_id, mode):
        """Привязка для прогона: точная (язык+тема) → иначе «на весь язык».

        Возвращает ArmPromptBinding (select_related("prompt")) или None —
        нет ни одной → прогон без промпта.
        """
        base = cls.objects.filter(
            mode=mode, programming_language_id=programming_language_id,
        ).select_related("prompt")
        if topic_id:
            exact = base.filter(topic_id=topic_id).first()
            if exact:
                return exact
        return base.filter(topic__isnull=True).first()

    def __str__(self):
        topic = self.topic if self.topic is not None else "(весь язык)"
        return f"{self.get_mode_display()}: {self.programming_language} / {topic} → {self.prompt}"


class TaskSolution(models.Model):
    """Кэш решённых задач «Реши задачу» (узел DL + язык → проверенный код).

    Записи создаются только когда пользователь сам отправил сгенерированный
    код на тестирование со страницы «Реши задачу» (HTTP send-solution; ARM и
    прочие прогоны кэш не пишут). После успешного тестирования (verdict =
    passed) код отдаётся из кэша всем, кто повторно запросит ту же задачу на
    том же языке, — без вызова модели. ``queue_id`` — активный poll DL
    (send-solution → get-solution-result); после финального вердикта
    сбрасывается. ``model_key`` — модель, сгенерировавшая код (из
    AIRequestLog последней успешной генерации для узла).
    """

    VERDICT_PENDING = "pending"
    VERDICT_PASSED = "passed"
    VERDICT_FAILED = "failed"

    VERDICT_CHOICES = (
        (VERDICT_PENDING, "Тестируется"),
        (VERDICT_PASSED, "Тестирование пройдено"),
        (VERDICT_FAILED, "Тестирование не пройдено"),
    )

    task_node_id = models.PositiveIntegerField(db_index=True, verbose_name="ID задачи DL")
    # Курс, в контексте которого задача тестировалась (из send-solution).
    # Нужен для пользовательской ссылки на задачу: /task.jsp?nid=…&cid=…
    # (admin-вьювер fullTaskviewer не используется — просмотр от имени админа).
    course_id = models.IntegerField(null=True, blank=True, verbose_name="ID курса DL")
    # Путь задачи в дереве задач DL (get-task-info → path при известном
    # курсе), напр. «Программирование\Циклы\Сумма». Заполняется best-effort
    # при отправке решения на тестирование; старые записи получают путь при
    # повторном решении той же задачи.
    tree_path = models.CharField(
        max_length=512, blank=True, default="", verbose_name="Путь в дереве задач DL",
    )
    programming_language_id = models.IntegerField(null=True, blank=True, verbose_name="ID языка программирования")
    file_extension = models.CharField(max_length=16, blank=True, default="", verbose_name="Расширение файла")
    code = models.TextField(verbose_name="Код решения")
    verdict = models.CharField(max_length=16, choices=VERDICT_CHOICES, default=VERDICT_PENDING, verbose_name="Вердикт")
    dl_comment = models.TextField(blank=True, default="", verbose_name="Комментарий DL")
    model_key = models.CharField(max_length=128, blank=True, default="", verbose_name="Ключ модели")
    model_title = models.CharField(max_length=255, blank=True, default="", verbose_name="Название модели")
    # Контекст последней генерации решения (как в AIRequestLog — id + имя).
    topic_id = models.IntegerField(null=True, blank=True, verbose_name="ID темы")
    topic_name = models.CharField(max_length=255, blank=True, default="", verbose_name="Тема")
    prompt_id = models.IntegerField(null=True, blank=True, verbose_name="ID промпта")
    prompt_name = models.CharField(max_length=255, blank=True, default="", verbose_name="Промпт")
    queue_id = models.PositiveIntegerField(null=True, blank=True, db_index=True, verbose_name="Queue ID DL")
    test_log = models.ForeignKey(
        "AIRequestLog", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="task_solutions", verbose_name="Запись журнала тестирования",
    )
    submitted_at = models.DateTimeField(null=True, blank=True, verbose_name="Отправлен на тестирование")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="task_solutions", verbose_name="Пользователь",
    )
    external_user_id = models.CharField(max_length=255, blank=True, default="", verbose_name="ID пользователя dl.gsu.by")
    times_used = models.PositiveIntegerField(default=0, verbose_name="Сколько раз выдавался из кэша")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Создан")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Обновлён")

    class Meta:
        db_table = "ai_task_solution"
        verbose_name = "Решённая задача (кэш кода)"
        verbose_name_plural = "Решённые задачи (кэш кода)"
        ordering = ("-updated_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["task_node_id", "programming_language_id"],
                name="unique_task_solution_node_lang",
            ),
        ]

    def __str__(self):
        lang = self.programming_language_id or "-"
        return f"Задача {self.task_node_id} ({lang}): {self.verdict}"


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
        ]

    def __str__(self):
        return f"{self.sent_at} — {self.user_full_name or self.username or self.external_user_id}"


class AIPinnedBatchRun(models.Model):
    """Закреплённый завершённый пакетный прогон (персонально для пользователя).

    Хранит пару (пользователь, запись журнала batch-solve) — ссылку на
    завершённый прогон «Пакетного решения» (source=arm), закреплённую кнопкой
    в журнале запросов или на «Настройке ИИ-приложения». Список закреплённых
    показывается на странице «Закреплённые пакетные решения» (пункт навигации
    под «Настройкой ИИ-приложения»); результаты прогона — та же ленивая
    развёртка (batch-snapshot), что в журнале.
    """
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="pinned_batch_runs",
        verbose_name="Кто закрепил",
    )
    log = models.ForeignKey(
        AIRequestLog,
        on_delete=models.CASCADE,
        related_name="pinned_by",
        verbose_name="Запись журнала (прогон)",
    )
    created_at = models.DateTimeField(default=timezone.now, verbose_name="Закреплён")

    class Meta:
        verbose_name = "Закреплённый пакетный прогон"
        verbose_name_plural = "Закреплённые пакетные прогоны"
        ordering = ("-created_at", "-id")
        constraints = [
            models.UniqueConstraint(
                fields=("user", "log"),
                name="ai_pinned_batch_run_user_log_uniq",
            )
        ]

    def __str__(self):
        return f"{self.user_id} → log {self.log_id}"


class AIDevTask(models.Model):
    """Таск на доске задач для разработчиков (страница /ai/admin/tasks/).

    Два типа (kind): FRONT «Фронт работы» и WISH «Хотелки» — у каждого СВОЯ
    сквозная нумерация (priority_no) с №1, уникальная внутри вида
    (констрейнт по (kind, priority_no)); МЕНЬШЕ = ВАЖНЕЕ; выдаётся сервером
    при создании (max+1 внутри вида), менять порядок можно
    перетаскиванием строк (superuser/action.task_reorder → /tasks/reorder/
    перенумеровывает 1..N в пределах вида). Создаётся (название, описание,
    срок — ТЕКСТОМ: «До 2 недель» — никаких расчётных дат) тем, у кого есть
    action.task_create; закрыть — увод с доски (closed_at/closed_by,
    priority_no сбрасывается): таск ОСТАЁТСЯ в БД и попадает в архив
    (вкладка «Закрытые»), откуда суперюзер/action.task_close может
    вернуть его на доску (reopen). Разработчик забирает таск («Взять»):
    taken_by/taken_at пишутся атомарно
    (условный UPDATE, «первый успел»), у взятого таска в таблице — ФИО +
    группа + дата взятия, повторно взять нельзя. ModelAdmin не
    регистрируется — доска целиком кастомная страница (ai/admin/tasks.py).
    """

    class Kind(models.TextChoices):
        FRONT = "front", "Фронт работы"
        WISH = "wish", "Хотелки"

    title = models.CharField(max_length=200, verbose_name="Название")
    description = models.TextField(
        blank=True, default="", verbose_name="Описание"
    )
    estimate = models.CharField(
        max_length=100, blank=True, default="", verbose_name="Срок",
        help_text="Текстом: «До 2 недель», «До 4 дней»",
    )
    kind = models.CharField(
        max_length=10, choices=Kind.choices, default=Kind.FRONT,
        verbose_name="Тип",
    )
    # Номер присваивается всем видам (max+1 внутри вида, миграция 0064);
    # NULL остаётся легаси-запасом. Уникальность — по (kind, priority_no),
    # частичный UniqueConstraint из Meta.constraints.
    priority_no = models.PositiveIntegerField(
        null=True, blank=True, verbose_name="Номер приоритета",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="created_dev_tasks",
        verbose_name="Кто создал",
    )
    taken_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="claimed_dev_tasks",
        verbose_name="Кто взял",
    )
    taken_at = models.DateTimeField(null=True, blank=True, verbose_name="Взят")
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="closed_dev_tasks",
        verbose_name="Кто закрыл",
    )
    closed_at = models.DateTimeField(null=True, blank=True, verbose_name="Закрыт")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Создан")

    class Meta:
        verbose_name = "Таск (доска задач)"
        verbose_name_plural = "Таски (доска задач)"
        ordering = ("kind", "priority_no", "-created_at", "-id")
        constraints = [
            # «Цифры повторяться не могут» — уникальный номер ВНУТРИ вида
            # (фронт и хотелки нумеруются независимо, оба с №1).
            models.UniqueConstraint(
                fields=["kind", "priority_no"],
                condition=models.Q(priority_no__isnull=False),
                name="ai_dev_task_priority_no_uniq",
            ),
        ]

    def __str__(self):
        if self.priority_no is not None:
            number = f"№{self.priority_no}"
        else:
            number = f"{self.kind} без номера"
        suffix = f" (взял {self.taken_by_id})" if self.taken_by_id else ""
        return f"[{number}] {self.title}{suffix}"


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


class AIAccessGroup(models.Model):
    """Группа доступа: членам выдаются права ключами (AIAdminGroupPermission).

    Заменяет жёсткие проверки is_prompt_developer_user/Django-Group
    prompt_developer: все can_access_* резолвят ключи из групп юзера
    (ai/admin/permissions.py::user_access_keys); состав и права каждой
    группы редактируются суперюзером на странице «Группы доступа»
    (/ai/admin/ai-groups/, ai/admin/access_groups.py). Встроенные группы
    («Разработчик промптов», «Разработчик») сидируются миграцией; их нельзя
    удалить и переименовать, но права правятся на той же странице.
    """

    name = models.CharField(max_length=100, unique=True, verbose_name="Название")
    description = models.TextField(blank=True, default="", verbose_name="Описание")
    is_builtin = models.BooleanField(default=False, verbose_name="Встроенная")
    permissions = models.ManyToManyField(
        AIAdminGroupPermission, blank=True, related_name="access_groups",
        verbose_name="Права",
    )
    users = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True, related_name="ai_access_groups",
        verbose_name="Участники",
    )

    class Meta:
        verbose_name = "Группа доступа"
        verbose_name_plural = "Группы доступа"
        ordering = ("name",)

    def __str__(self):
        return self.name


class AICourseTreeCache(models.Model):
    """Сохранённое дерево курса dl.gsu.by (последняя успешная загрузка).

    get-node-tree у DL требует АКТИВНЫЙ курс в DL-сессии (courseID>0): у
    пользователя, который не «зашёл» в курс на dl.gsu.by, DL отвечает 403
    «Доступ запрещён» — и до вката бессрочного кэша дерево не грузилось НИ у
    кого. Redis-копия (dl_tree_stale) переживает TTL, но не переживает
    очистку Redis; эта таблица — последняя линия обороны: при успехе
    (load-tree) сюда пишется копия дерева, при отказе DL она отдаётся всем,
    поэтому «Пакетное решение» работает у всех после одной успешной загрузки.
    """

    course_id = models.PositiveIntegerField(
        unique=True, verbose_name="Курс DL (courseId)"
    )
    tree = models.JSONField(default=list, verbose_name="Дерево (JSON)")
    task_count = models.PositiveIntegerField(default=0, verbose_name="Задач в дереве")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Обновлено")

    class Meta:
        verbose_name = "Кэш дерева курса DL"
        verbose_name_plural = "Кэши деревьев курсов DL"

    def __str__(self):
        return f"course {self.course_id} · {self.task_count} задач"


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
    на страницу прогона (``?run_id=``). ``paused`` — кооперативная пауза
    batch-прогона (вступает на границе пар); осиротевший paused-прогон
    (нет живого job) помечается «Процесс потерян», как и running.
    """

    STATUS_RUNNING = "running"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"
    STATUS_CANCELLED = "cancelled"
    STATUS_PAUSED = "paused"

    STATUS_CHOICES = (
        (STATUS_RUNNING, "Running"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_FAILED, "Failed"),
        (STATUS_CANCELLED, "Cancelled"),
        (STATUS_PAUSED, "Paused"),
    )

    RUN_TYPE_SINGLE = "single"
    RUN_TYPE_BATCH = "batch"
    RUN_TYPE_PROMPT_DEBUG = "prompt_debug"

    RUN_TYPE_CHOICES = (
        (RUN_TYPE_SINGLE, "Single (find-error)"),
        (RUN_TYPE_BATCH, "Batch (solve)"),
        (RUN_TYPE_PROMPT_DEBUG, "Prompt debug"),
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
    # Ручное название прогона, задаваемое при запуске на /arm/solve/. Хранится
    # на самом прогоне — журнал и таблица «Последние пакетные решения» читают
    # его без дополнительных связок.
    run_name = models.CharField(max_length=255, blank=True, default="", verbose_name="Название прогона")

    class Meta:
        db_table = "ai_ai_model_test_run"
        verbose_name = "Прогон тестирования модели"
        verbose_name_plural = "Прогоны тестирования модели"
        ordering = ("-started_at",)
        indexes = [
            # Список прогонов всегда сортируется по -started_at; фильтр по
            # статусу («идущие/завершённые») — частый.
            models.Index(fields=["started_at"], name="ai_model_test_run_started_idx"),
            models.Index(fields=["status", "started_at"], name="ai_model_test_run_status_idx"),
        ]

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

    # Ортогональная ось «почему пара не решена» (orthogonal to ``verdict``:
    # verdict отвечает «прошёл ли DL», failure_reason — «почему нет»).
    # solved ⇒ пустая строка; skipped ⇒ "skipped"; нераспознанный провал DL ⇒
    # "undefined" (честно: причина не опознана, а не «успех»).
    FR_MODEL_ERROR = "model_error"
    FR_NO_CODE = "no_code"
    FR_COMPILE_ERROR = "compile_error"
    FR_RUNTIME_ERROR = "runtime_error"
    FR_WRONG_ANSWER = "wrong_answer"
    FR_UNDEFINED = "undefined"
    FR_SKIPPED = "skipped"
    # DL отклонил отправку решения: сессия DLSID истекла/невалидна (HTTP 401).
    # Отдельная причина, а не «не распознана»: лечится обновлением DLSID.
    FR_DL_UNAUTHORIZED = "dl_unauthorized"

    FAILURE_REASON_CHOICES = (
        (FR_MODEL_ERROR, "Сбой ответа модели"),
        (FR_NO_CODE, "Не выкушен код"),
        (FR_COMPILE_ERROR, "Ошибка компиляции"),
        (FR_RUNTIME_ERROR, "Ошибка исполнения программы"),
        (FR_WRONG_ANSWER, "Неправильный ответ"),
        (FR_UNDEFINED, "Причина не распознана"),
        (FR_SKIPPED, "Пропущено"),
        (FR_DL_UNAUTHORIZED, "Ошибка DL: не авторизован"),
    )

    FAILURE_REASON_LABELS = dict(FAILURE_REASON_CHOICES)

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
    # Причина провала (см. FAILURE_REASON_CHOICES). Пустая строка = solved.
    failure_reason = models.CharField(
        max_length=16, choices=FAILURE_REASON_CHOICES, null=True, blank=True,
        db_index=True, default="", verbose_name="Причина сбоя",
    )
    duration_seconds = models.FloatField(null=True, blank=True, verbose_name="Время ответа, с")
    tokens = models.PositiveIntegerField(null=True, blank=True, verbose_name="Токенов")
    # Токены промпта (вход) — «tokens» остаётся completion.
    tokens_prompt = models.PositiveIntegerField(null=True, blank=True, verbose_name="Токенов (промпт)")
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
    # Имя файла решения, отправленного в DL (контракт fileName): по умолчанию
    # подписанное — <модель>_<task_id>.<ext> («Gemma4_12345.mcp»), где task_id —
    # ID задачи DL из get-task-info (fallback — node_id узла дерева).
    # null=True и БЕЗ default — на SQLite это обычный ALTER TABLE ADD COLUMN:
    # пересоздание таблицы (default/не-null) падало бы на postgres-only
    # GinIndex модели (gin_trgm_ops) — см. миграцию 0071.
    dl_file_name = models.CharField(max_length=200, null=True, blank=True, verbose_name="DL: имя файла")
    # Входные данные и эталонный ответ проваленного теста (JSON-строка:
    # {"input": ..., "expected_output": ...}). Заполняется только для НЕ
    # решённых пар, которым DL разрешил выдачу теста (canGetData).
    dl_test_data = models.TextField(null=True, blank=True, verbose_name="DL: данные теста")
    # Машина (Дельта), на которой DL проверял решение: id и имя из
    # get-solution-result (deltaId/deltaName). Пусто — машина не назначена
    # или проверка не дошла до DL.
    delta_id = models.IntegerField(null=True, blank=True, verbose_name="DL: машина (id)")
    delta_name = models.CharField(max_length=255, blank=True, default="", verbose_name="DL: машина")
    # Снимок расширения файла задачи (для имени файла скачивания).
    file_extension_snapshot = models.CharField(max_length=16, blank=True, default="", verbose_name="Расширение (снимок)")
    # Snapshot of the task's topic / programming language at run time (batch runs).
    topic_id_snapshot = models.IntegerField(null=True, blank=True, verbose_name="ID темы (снимок)")
    topic_name_snapshot = models.CharField(max_length=255, blank=True, default="", verbose_name="Тема (снимок)")
    prog_lang_snapshot = models.CharField(max_length=255, blank=True, default="", verbose_name="Язык программирования (снимок)")
    # Снимок фактически использованного промпта пары (при старте «по
    # привязке» у задачи свой промпт; легаси-строки и прогоны без привязок —
    # NULL/пусто, отображается «—»).
    prompt_id = models.IntegerField(null=True, blank=True, verbose_name="ID промпта (снимок)")
    prompt_name = models.CharField(max_length=255, blank=True, default="", verbose_name="Промпт (снимок)")
    # Ответ модели оборван потолком генерации провайдера (Ollama num_predict /
    # SambaNova max_tokens — стрим остановлен ровно на лимите;
    # is_output_truncated). Ретрай с припиской «сразу код» уже сделан во время
    # прогона; пара помечается чинно, потому что часть ответа реально потеряна.
    truncated = models.BooleanField(default=False, verbose_name="Ответ обрезан")
    # Отладка промтов (`run.run_type == "prompt_debug"`): попытки пары,
    # дополнения промпта, факт коммита и результат проверочного прохода.
    # Контракт — ai/services/prompt_debug.py::empty_payload.
    # Nullable и БЕЗ Python-default: на SQLite AddField с default пересоздаёт
    # таблицу, а у модели есть postgres-only GinIndex (gin_trgm_ops) —
    # пересоздание падает на "gin_trgm_ops" (та же причина, что у updated_at).
    # Читатели всегда берут ``... or {}``.
    prompt_debug = models.JSONField(
        null=True, blank=True, verbose_name="Отладка промпта",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Создан")
    # Время последнего обновления строки. Повтор пары перезаписывает её через
    # update_or_create → updated_at меняется, created_at остаётся прежним.
    # По updated_at результаты сортируются «свежие сверху» (см.
    # arm_runner._sort_results_fresh_first).
    #
    # Пишется ЯВНО (не auto_now): на SQLite добавление auto_now-поля
    # пересоздаёт таблицу, а у этой модели в Meta.indexes есть
    # postgres-only GinIndex (gin_trgm_ops) — пересоздание падало бы с
    # «near "gin_trgm_ops": syntax error». Пишет писатель строки
    # (arm_runner._write_pair_row).
    updated_at = models.DateTimeField(null=True, blank=True, verbose_name="Обновлён")

    class Meta:
        db_table = "ai_ai_model_test_result"
        verbose_name = "Результат тестирования модели"
        verbose_name_plural = "Результаты тестирования модели"
        ordering = ("model_title",)
        indexes = [
            # Сортировка результатов по названию модели (Meta.ordering) и
            # выборка «результаты одного прогона» — обе без индекса
            # сортировали весь результат прогона на каждой загрузке.
            models.Index(fields=["model_title"], name="ai_model_test_result_title_idx"),
            models.Index(fields=["run", "model_title"], name="ai_mtr_run_title_idx"),
            # Поиск пар по названию модели (icontains) — expression-индекс
            # на UPPER(model_title) с триграммами (см. AIRequestLog: предикат
            # UPPER(col::text) LIKE UPPER(%p%) не обслуживается обычным
            # индексом по колонке). Создаёт миграция 0069 (только Postgres).
            GinIndex(
                OpClass(Upper("model_title"), "gin_trgm_ops"),
                name="ai_mtr_model_title_trgm_idx",
            ),
        ]
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


class AIModelStats(models.Model):
    """Накопленная статистика (model_key, язык, тема) по прогонам /arm/solve/.

    Обновляется только по завершении пакетного прогона, запущенного с
    включённым чекбоксом «заносить результаты в БД» (доступен суперюзерам).
    Semantics совпадают с ``arm_runner._per_bucket``: в ``total_count``
    идут только решённые/проваленные пары, длительности — только не-skipped.

    Скоупы (2026-10): строка (модель, язык, тема) — пары этой темы; строка
    (модель, язык, NULL) — языковая строка («выбран только язык»); пара
    инкрементирует ОБЕ строки (без темы — только языковую). Строки с
    (NULL, NULL) — легаси-глобальный скоуп по model_key, новые приращения
    в него не пишутся, UI его не читает (оператор чистит reset_model_stats).
    """

    model_key = models.CharField(max_length=128, db_index=True, verbose_name="Ключ модели")
    model_title = models.CharField(max_length=255, blank=True, default="", verbose_name="Название модели")
    solved_count = models.PositiveIntegerField(default=0, verbose_name="Решено задач")
    total_count = models.PositiveIntegerField(default=0, verbose_name="Всего задач (решено+провалено)")
    duration_count = models.PositiveIntegerField(default=0, verbose_name="Измерений времени")
    duration_sum = models.FloatField(default=0.0, verbose_name="Сумма времени, сек")
    programming_language = models.ForeignKey(
        ProgrammingLanguage, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="model_stats", verbose_name="Язык программирования",
    )
    topic = models.ForeignKey(
        Topic, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="model_stats", verbose_name="Тема",
    )
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Обновлено")

    class Meta:
        db_table = "ai_ai_model_stats"
        verbose_name = "Статистика модели"
        verbose_name_plural = "Статистика моделей"
        ordering = ("-solved_count",)
        indexes = [
            # Скоуп-ключ чтения (model_key, язык, тема). Уникальность НЕ
            # навязана бд-констрейнтом: prod-PG 14 не умеет NULLS NOT DISTINCT
            # (нужен PG15+), а «двойная строка язык-скоупа» невозможна в
            # одиночном воркере финализации — честный upsert в model_stats.
            models.Index(
                fields=["model_key", "programming_language", "topic"],
                name="ai_model_stats_scope_idx",
            ),
            # Список статистики сортируется по -solved_count (Meta.ordering).
            models.Index(
                fields=["solved_count"],
                name="ai_model_stats_solved_idx",
            ),
        ]

    def __str__(self):
        return self.model_key


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


class DLEducationName(models.Model):
    """Справочник DL «id → человекочитаемое название» (вуз/школа, группа/класс).

    REST get-user-info отдаёт только коды: schoolId (строка), schoolKind,
    schoolNo и groupMaskId — настоящих названий вузов/школ и групп там нет.
    Ведётся вручную суперюзером (по образцу AuthorAlias); отображение
    (дневной отчёт журнала) резолвится при рендере — правка записи
    ретроактивно чинит прошлые строки. Читатель — единственный:
    ai/services/education_directory.py.
    """
    KIND_SCHOOL = "school"
    KIND_GROUP = "group"
    KIND_CHOICES = (
        (KIND_SCHOOL, "Школа/вуз (schoolId)"),
        (KIND_GROUP, "Группа/класс (groupMaskId)"),
    )
    kind = models.CharField(
        max_length=10, choices=KIND_CHOICES, verbose_name="Тип записи",
    )
    key = models.CharField(max_length=100, verbose_name="ID в DL")
    title = models.CharField(max_length=255, verbose_name="Название")
    # Вуз/школа, к которой относится группа/класс: key соответствующей
    # school-строки этого же справочника (groupMaskId — строка без структуры,
    # DL не сообщает заведение группы). Заполняется ТОЛЬКО у kind="group";
    # читатель — education_directory (groups_school), фолбэк в дневном отчёте
    # показывает школу студента от привязки его группы.
    school = models.CharField(
        max_length=100, blank=True, default="",
        verbose_name="Вуз/школа (key из schoolId)",
    )

    class Meta:
        db_table = "ai_dl_education_name"
        verbose_name = "Учебное название DL (id → название)"
        verbose_name_plural = "Учебные названия DL (вуз/школа, группа)"
        ordering = ("kind", "title")
        constraints = [
            models.UniqueConstraint(
                fields=("kind", "key"),
                name="ai_dl_education_name_kind_key_uniq",
            ),
        ]

    def __str__(self):
        return f"{self.title} [{self.key}]"


class UpdateLog(models.Model):
    """Журнал обновлений проекта.

    Записи добавляются перед git add/git push. Каждая запись — описание
    изменений на русском языке, дата и автор коммита.
    """
    commit_date = models.DateField(verbose_name="Дата")
    description = models.TextField(verbose_name="Содержание обновления")
    author = models.CharField(max_length=255, verbose_name="Автор")
    commit_hash = models.CharField(max_length=40, blank=True, default="", verbose_name="Хэш коммита")
    # Скрытые строки — коммиты, отклонённые при интерактивной синхронизации
    # (sync_update_log --interactive). Не видны на «Обновлениях», видны только
    # в UpdateLogAdmin — там выбираются публикацией/скрытием.
    hidden = models.BooleanField(default=False, verbose_name="Скрыто")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Создан")

    class Meta:
        db_table = "ai_update_log"
        verbose_name = "Обновление"
        verbose_name_plural = "Обновления"
        ordering = ("-commit_date", "-created_at", "-id")

    def __str__(self):
        return f"{self.commit_date} — {self.author} — {self.description[:80]}"


class AIWizardSeen(models.Model):
    """Отметка «пользователь уже видел онбординг-wizard этой версии».

    Одна строка на (пользователь, scope): тур для страницы «chat»/«solve»/
    «find_error» или админки («admin», «admin_arm_solve», «admin_model_status»).
    Показывать заново, когда нет строки или version меньше WIZARD_VERSION
    (ai/constants.py) — так изменённые тексты тура показываются всем ещё раз.
    Решение «показывать ли» принимает ai/services/onboarding.py.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="wizard_seen",
        verbose_name="Пользователь",
    )
    scope = models.CharField(max_length=32, verbose_name="Страница (scope тура)")
    version = models.PositiveIntegerField(
        default=1, verbose_name="Версия текстов подсказок",
    )
    created_at = models.DateTimeField(default=timezone.now, verbose_name="Когда пройдено")

    class Meta:
        db_table = "ai_wizard_seen"
        verbose_name = "Пройденный онбординг"
        verbose_name_plural = "Пройденные онбординги"
        ordering = ("user_id", "scope")
        constraints = [
            models.UniqueConstraint(
                fields=("user", "scope"),
                name="ai_wizard_seen_user_scope_uniq",
            ),
        ]

    def __str__(self):
        return f"{self.user_id} / {self.scope} v{self.version}"


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

    def __str__(self) -> str:
        return f"{self.title} ({self.model_id}) — {self.user_id}"


class AIDLApiRequest(models.Model):
    """Запрос пользователя на создание API к DL (dl.gsu.by).

    Страница «Запросы по DL API»: две вкладки — «актуальные» (STATUS_PENDING,
    API ещё не создан) и «неактуальные» (API сделано — STATUS_DONE).
    Актуальный запрос провисает, когда API создан: админ жмёт «API создано»,
    и строка переезжает в «неактуальные».
    """

    STATUS_PENDING = "pending"
    STATUS_DONE = "done"

    STATUS_CHOICES = (
        (STATUS_PENDING, "Актуальный"),
        (STATUS_DONE, "Неактуальный"),
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="ai_dl_api_requests",
        verbose_name="Кто попросил",
    )
    external_user_id = models.CharField(max_length=255, blank=True, db_index=True, verbose_name="ID на dl.gsu.by")
    user_full_name = models.CharField(max_length=500, blank=True, default="", verbose_name="ФИО")
    username = models.CharField(max_length=255, blank=True, default="", verbose_name="Логин")
    description = models.TextField(blank=True, default="", verbose_name="Описание")
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True,
        verbose_name="Статус",
    )
    created_at = models.DateTimeField(default=timezone.now, db_index=True, verbose_name="Когда попросил")
    resolved_at = models.DateTimeField(null=True, blank=True, verbose_name="Когда сделано")
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="ai_dl_api_requests_resolved",
        verbose_name="Кто сделал",
    )
    comment = models.TextField(blank=True, default="", verbose_name="Комментарий")

    class Meta:
        db_table = "ai_ai_dl_api_request"
        verbose_name = "Запрос API к DL"
        verbose_name_plural = "Запросы API к DL"
        ordering = ("-created_at",)

    def __str__(self) -> str:
        who = self.user_full_name or self.username or self.external_user_id or "—"
        return f"{who}: {self.description[:60]}"
