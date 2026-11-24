"""Фоновая очередь пакетной проверки исходников."""

import logging
import threading
from time import sleep

from django.db import close_old_connections, connection
from django.utils import timezone

from ..models import AICheckBatchFile
from .pangram_client import PangramError, check_text

logger = logging.getLogger(__name__)
Status = AICheckBatchFile.Status
_QUEUE_LOCK_ID = 867_085_001
_local_lock = threading.Lock()


def _process_file():
    # Если процесс оборвался во время запроса, результат неизвестен. Не
    # отправляем тот же файл повторно, чтобы не списывать кредиты дважды.
    AICheckBatchFile.objects.filter(status=Status.RUNNING).update(
        status=Status.ERROR, source_text="", checked_at=timezone.now(),
        error="Проверка прервалась при перезапуске сервера. Загрузите файл повторно.",
    )
    item = AICheckBatchFile.objects.filter(status=Status.QUEUED).order_by("id").first()
    if item is None:
        return False

    item.status = Status.RUNNING
    item.save(update_fields=["status"])
    try:
        item.result.update(check_text(item.source_text, wait_seconds=120))
        item.status = Status.DONE
    except PangramError as exc:
        item.status, item.error = Status.ERROR, str(exc)
    except Exception:
        logger.exception("Ошибка проверки файла %s", item.pk)
        item.status, item.error = Status.ERROR, "Не удалось проверить файл."
    item.source_text = ""
    item.checked_at = timezone.now()
    item.save(update_fields=["source_text", "checked_at", "status", "result", "error"])
    return True


def process_next_file():
    if not AICheckBatchFile.objects.filter(status__in=[Status.QUEUED, Status.RUNNING]).exists():
        return False
    if connection.vendor != "postgresql":
        # Для локальных установок с SQLite очередь выполняется в одном процессе.
        with _local_lock:
            return _process_file()

    # Блокировка принадлежит соединению и защищает очередь и при нескольких
    # процессах Django. Держать транзакцию открытой во время HTTP-запроса не нужно.
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [_QUEUE_LOCK_ID])
        if not cursor.fetchone()[0]:
            return False
        try:
            return _process_file()
        finally:
            cursor.execute("SELECT pg_advisory_unlock(%s)", [_QUEUE_LOCK_ID])


def _run_worker():
    sleep(2)
    while True:
        try:
            close_old_connections()
            if not process_next_file():
                sleep(2)
        except Exception:
            logger.exception("Ошибка очереди пакетной проверки на ИИ")
            connection.close()
            sleep(15)


def start_check_ai_worker():
    threading.Thread(target=_run_worker, name="ai-check-queue", daemon=True).start()
