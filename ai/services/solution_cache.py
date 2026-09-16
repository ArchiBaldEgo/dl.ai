"""Кэш решённых задач «Реши задачу» (узел DL + язык → проверенный код).

Все операции с ``TaskSolution`` — здесь (SRP). Кэш пишется только когда
пользователь сам отправил сгенерированный код на тестирование со страницы
(HTTP send-solution); ARM и прочие прогоны его не пишут. После вердикта
passed код отдаётся из кэша без вызова модели.
"""

import logging

from django.db.models import F
from django.utils import timezone

from ..model_clients import registry
from ..models import AIRequestLog, TaskSolution

logger = logging.getLogger(__name__)

# Сообщение журнала AIRequestLog при выдаче решения из кэша (consumers.py
# _serve_cached_solution). Такие записи не являются генерацией решения —
# их нужно исключать при поиске контекста последней генерации.
CACHE_SERVE_LOG_MESSAGE = "Повторный запрос задачи — выдано сохранённое решение (кэш)."


def find_passed_solution(node_id, programming_language_id):
    """Пройденное решение для узла+языка или ``None`` (verdict == passed)."""
    if not node_id:
        return None
    return TaskSolution.objects.filter(
        task_node_id=node_id,
        programming_language_id=programming_language_id or None,
        verdict=TaskSolution.VERDICT_PASSED,
    ).first()


def record_submission(node_id, code, *, programming_language_id=None, file_extension="",
                      course_id=None, identity=None, queue_id=None, test_log=None,
                      session_id=None):
    """Upsert-запись кэша при отправке кода на тестирование со страницы.

    ``identity`` — словарь ``get_user_identity_for_log``; ``test_log`` —
    запись AIRequestLog (mode=testing) этого тестирования; ``course_id`` —
    курс DL из send-solution (для пользовательской ссылки на задачу). Не
    поднимает исключений: сбой кэша не должен ломать отправку решения.
    """
    try:
        model_key, model_title = last_solve_model(node_id)
        identity = identity or {}
        defaults = {
            "code": code,
            "file_extension": file_extension or "",
            "verdict": TaskSolution.VERDICT_PENDING,
            "dl_comment": "",
            "model_key": model_key,
            "model_title": model_title,
            "external_user_id": identity.get("external_user_id", ""),
            "created_by": identity.get("user"),
            "queue_id": queue_id,
            "course_id": course_id or None,
            "submitted_at": timezone.now(),
            "test_log": test_log,
        }
        # Тема/препромпт — из журнала последней генерации решения; когда
        # контекста нет (пустые), существующие значения не затираем.
        log = last_solve_log(node_id)
        if log is not None:
            if log.topic_id or log.topic_name:
                defaults["topic_id"] = log.topic_id
                defaults["topic_name"] = log.topic_name
            if log.prompt_id or log.prompt_name:
                defaults["prompt_id"] = log.prompt_id
                defaults["prompt_name"] = log.prompt_name
        solution, _created = TaskSolution.objects.update_or_create(
            task_node_id=node_id,
            programming_language_id=programming_language_id or None,
            defaults=defaults,
        )
        # Путь в дереве задач DL — best-effort уже после записи: сбой/таймаут
        # DL не должен ни ломать отправку решения, ни задерживать её (кэш уже
        # записан). Старые записи получают путь при повторном решении.
        _update_tree_path(solution, session_id=session_id, course_id=course_id)
        return solution
    except Exception:
        logger.exception("Failed to record TaskSolution submission for node %s", node_id)
        return None


def _update_tree_path(solution, *, session_id=None, course_id=None):
    """Дописать ``tree_path`` (путь задачи в дереве задач DL) к записи кэша.

    Источник — get-task-info (поле ``path``, возвращается только при
    известном course_id — см. dl_api_client.fetch_task_info). Без сессии,
    курса или при ошибке DL путь не трогаем: приписка не критична. Пишет
    напрямую через .update() — не трогает updated_at (порядок списка).
    """
    if not session_id or not course_id:
        return
    try:
        from ..dl_api_client import fetch_task_info
        data = fetch_task_info(
            solution.task_node_id, session_id=session_id,
            remove_html_tags=True, course_id=course_id,
        )
    except Exception:
        logger.info("tree_path fetch skipped for node %s", solution.task_node_id)
        return
    path = (data or {}).get("path") or ""
    if path and path != solution.tree_path:
        TaskSolution.objects.filter(pk=solution.pk).update(tree_path=path)


def record_result(queue_id, comment, *, finished=True):
    """Финальный вердикт по ``queue_id``: обновляет кэш и возвращает строку.

    Возвращает ``(TaskSolution | None, verdict_str | None, solved: bool)``.
    Незавершённый poll (``finished=False``) ничего не меняет.
    """
    if not queue_id or not finished:
        return None, None, False
    solution = TaskSolution.objects.filter(
        queue_id=queue_id, verdict=TaskSolution.VERDICT_PENDING,
    ).select_related("test_log").first()
    if solution is None:
        return None, None, False

    from ..dl_api_client import DL_VERDICT_FAILED, DL_VERDICT_SOLVED, dl_verdict_from_comment

    dl_verdict = dl_verdict_from_comment(comment)
    solution.verdict = (
        TaskSolution.VERDICT_PASSED if dl_verdict == DL_VERDICT_SOLVED else TaskSolution.VERDICT_FAILED
    )
    solution.dl_comment = comment or ""
    solution.queue_id = None
    solution.save(update_fields=["verdict", "dl_comment", "queue_id", "updated_at"])

    # Статистика модели (AIModelStats, селектор моделей) — только по
    # завершённым тестам, семантика совпадает с arm_runner._per_bucket.
    duration = None
    log = solution.test_log
    if log is not None:
        duration = (timezone.now() - (log.sent_at or solution.submitted_at or timezone.now())).total_seconds()
        log.status = AIRequestLog.STATUS_SUCCESS
        log.received_at = timezone.now()
        log.duration_seconds = duration
        if log.response_text:
            log.response_text = f"{log.response_text}\nDL: {solution.dl_comment}"[:20000]
        log.save(update_fields=["status", "received_at", "duration_seconds", "response_text"])

    from .model_stats import record_batch_solve_stats
    record_batch_solve_stats([{
        "model_key": solution.model_key,
        "model_title": solution.model_title,
        "verdict": DL_VERDICT_SOLVED if dl_verdict == DL_VERDICT_SOLVED else DL_VERDICT_FAILED,
        "duration": duration,
    }])

    return solution, dl_verdict, dl_verdict == DL_VERDICT_SOLVED


def mark_cache_used(solution):
    """Учёт выдачи решения из кэша (не поднимает исключений)."""
    try:
        TaskSolution.objects.filter(pk=solution.pk).update(times_used=F("times_used") + 1)
    except Exception:
        logger.exception("Failed to increment times_used for TaskSolution %s", solution.pk)


def last_solve_log(node_id):
    """Журнал последней успешной генерации решения для узла (mode=solve).

    Записи выдачи из кэша (CACHE_SERVE_LOG_MESSAGE) исключаются — они тоже
    mode=solve/status=success, но не являются генерацией и не несут контекста.
    """
    return AIRequestLog.objects.filter(
        mode=AIRequestLog.MODE_SOLVE,
        task_node_id=node_id,
        status=AIRequestLog.STATUS_SUCCESS,
    ).exclude(message=CACHE_SERVE_LOG_MESSAGE).order_by("-sent_at").first()


def last_solve_model(node_id):
    """(model_key, model_title) последней успешной генерации решения для узла.

    Модель, сгенерировавшая код, известна из журнала WS-генерации (mode=solve,
    status=success) — клиент модель на тестирование не передаёт.
    """
    log = last_solve_log(node_id)
    if log is None:
        return "", ""
    key = (log.model_names or [""])[0] if log.model_names else ""
    title = ""
    entry = registry.get(key) if key else None
    if entry:
        title = entry.get("title") or key
    return key, title