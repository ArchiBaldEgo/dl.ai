# src/ai/db/AIModelTestResult.py

from django.conf import settings
from django.contrib.postgres.indexes import GinIndex, OpClass
from django.db import models
from django.db.models.functions import Upper
from django.utils import timezone

from ai.db.AIModelTestRun import AIModelTestRun
from ai.db.Task import Task

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
            # Лёгкий снапшот прогона: run + ORDER BY -updated_at, -pk (индекс
            # читается в обратном порядке) — поллинг /arm/solve/ каждые 2 с.
            models.Index(fields=["run", "updated_at", "id"], name="ai_mtr_run_updated_idx"),
            # Счётчики пар по прогону (solved/failed/skipped) — COUNT по
            # (run, verdict).
            models.Index(fields=["run", "verdict"], name="ai_mtr_run_verdict_idx"),
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

