"""Auto-registration of DL tasks solved via the chat page.

When a user solves a DL task on the ``/ai/solve-problem/`` page, the WebSocket
consumer calls :func:`ensure_task` so the task appears in the local ``Task``
table and becomes available (once the operator fills ``file_extension`` and
activates it) for batch-solve ARM. The DL field extraction is shared with
``TaskAdmin.refresh_from_dl`` via :func:`apply_dl_task_info` (DRY).
"""

import logging

from ..dl_api_client import DLApiError, fetch_task_info
from ..models import Task

logger = logging.getLogger(__name__)


def apply_dl_task_info(task, data):
    """Fill DL-owned fields (taskId/name/statement) from a get-task-info response.

    Shared by :func:`ensure_task` and ``TaskAdmin.refresh_from_dl`` so the DL
    response-key mapping lives in one place. Only overwrites a field when the
    new value is truthy, matching the previous ``... or task.field`` behaviour.
    """
    if data.get("taskId"):
        task.task_id = data["taskId"]
    name = (data.get("name") or "")[:512]
    if name:
        task.name = name
    if data.get("statement"):
        task.statement = data["statement"]


_LANG_TO_EXTENSION = {
    "python": ".py",
    "cmpa": ".cmpa",
    "ассемблер i86": ".asm",
    "pascal": ".pas",
    "verilog": ".v",
    "c++": ".cpp",
    "c": ".c",
}


def _guess_extension(prog_lang_name: str) -> str:
    """Best-effort file extension from a programming language name."""
    if not prog_lang_name:
        return ""
    low = prog_lang_name.lower().strip()
    return _LANG_TO_EXTENSION.get(low, "")


def ensure_task(node_id, *, programming_language_id=None, topic_id=None, session_id=None):
    """Get-or-create a ``Task`` row for a DL node id; best-effort DL fill on create.

    Used by the chat consumer when a user solves a DL task. Never raises —
    registration must not break the chat. ``file_extension`` is intentionally
    left blank (it cannot be derived from ``ProgrammingLanguage``'s display
    name and is required for ``fetch_task_solution``); the operator fills it
    and activates the task. Auto-created tasks are ``active=False`` so they do
    not clutter batch-solve "all active" runs while still ungradeable.
    """
    try:
        # Auto-determine file_extension from programming language if provided.
        file_ext = ""
        if programming_language_id is not None:
            from ..models import ProgrammingLanguage
            try:
                pl = ProgrammingLanguage.objects.get(pk=programming_language_id)
                file_ext = _guess_extension(pl.language_name)
            except ProgrammingLanguage.DoesNotExist:
                pass

        task, created = Task.objects.get_or_create(
            node_id=node_id,
            defaults={
                "programming_language_id": programming_language_id,
                "topic_id": topic_id,
                "active": False,
                "file_extension": file_ext,
            },
        )
        if not created:
            dirty = False
            if programming_language_id is not None and task.programming_language_id != programming_language_id:
                task.programming_language_id = programming_language_id
                dirty = True
            if topic_id is not None and task.topic_id != topic_id:
                task.topic_id = topic_id
                dirty = True
            if dirty:
                task.save(update_fields=["programming_language_id", "topic_id"])
            return task

        # Created — best-effort fill name/statement/task_id from DL (once).
        if session_id:
            try:
                data = fetch_task_info(node_id, session_id=session_id, remove_html_tags=True)
            except DLApiError:
                data = None
            if data:
                apply_dl_task_info(task, data)
                task.save(update_fields=["task_id", "name", "statement"])
        return task
    except Exception:
        logger.exception("ensure_task failed for node_id=%s", node_id)
        return None