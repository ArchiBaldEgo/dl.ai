"""Авто-регистрация DL-задач, решаемых через страницу чата.

Когда пользователь решает DL-задачу на /ai/solve-problem/, WebSocket consumer
вызывает ensure_task, чтобы задача появилась в локальной таблице Task
и стала доступна для batch-solve ARM. Извлечение полей DL — общее с
TaskAdmin.refresh_from_dl через apply_dl_task_info (DRY).
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
    "c-mpa": ".mpc",
    "cmpa": ".mpc",
    "с-мпа": ".mpc",
    "ассемблер i86": ".i86",
    "ассемблер i8086": ".i86",
    "ассемблер": ".i86",
    "pascal": ".pas",
    "verilog": ".v",
    "c++": ".cpp",
    "c": ".c",
    "java": ".java",
}

# Path-based extension detection: DL path contains the language spec,
# e.g. "Программирование [Ассемблер i8086, C-MPA]\...".
# Внимание: папка курса может перечислять несколько языков через запятую
# ("[Ассемблер i8086, C-MPA]"), поэтому путь неоднозначен — автогадание лишь
# лучший усилие; окончательное расширение задаёт пользователь на /arm/solve/.
_PATH_KEYWORDS = [
    ("ассемблер i8086", ".i86"),
    ("i8086", ".i86"),
    ("i86", ".i86"),
    ("ассемблер", ".i86"),
    ("c-mpa", ".mpc"),
    ("с-мпа", ".mpc"),
    ("cmpa", ".mpc"),
    ("hlccad", ".mpc"),  # HLCCAD uses C-MPA language
    ("python", ".py"),
    ("pascal", ".pas"),
    ("verilog", ".v"),
    ("c++", ".cpp"),
]

# Канонический список расширений, принимаемых DL REST send-solution, и
# соответствующих им названий языков для препромпта. Пользователь выбирает
# расширение вручную на /arm/solve/ (тема из дерева DL не определяет язык
# однозначно — курс "[Ассемблер i8086, C-MPA]" содержит задачи обоих языков,
# причём каждая задача допускает и .mpc, и .i86).
SOLVE_EXTENSION_CHOICES = [
    (".pas", "Pascal"),
    (".cpp", "C++"),
    (".c", "C"),
    (".py", "Python"),
    (".java", "Java"),
    (".i86", "Ассемблер i8086"),
    (".mpc", "C-MPA (С-МПА)"),
    (".v", "Verilog"),
]

# Обратный map: расширение → название языка (для препромпта в batch-solve).
EXTENSION_TO_LANG = {ext: name for ext, name in SOLVE_EXTENSION_CHOICES}


def _guess_extension(prog_lang_name: str) -> str:
    """Best-effort file extension from a programming language name or DL path.

    First tries exact language-name match, then falls back to keyword
    detection in the path/name (handles "Ассемблер i8086, C-MPA" etc).
    """
    if not prog_lang_name:
        return ""
    low = prog_lang_name.lower().strip()
    # Exact match.
    ext = _LANG_TO_EXTENSION.get(low, "")
    if ext:
        return ext
    # Keyword detection in path/name.
    for keyword, file_ext in _PATH_KEYWORDS:
        if keyword in low:
            return file_ext
    return ""


def extension_to_language_ids(file_extension):
    """Вернуть id ``ProgrammingLanguage``, чьё расширение совпадает с данным.

    Переиспользует ``_guess_extension`` (DRY): итерирует все ``ProgrammingLanguage``
    и оставляет те, у кого ``_guess_extension(language_name) == file_extension``.
    Используется на /arm/solve/ для фильтрации препромтов по выбранному расширению
    (промпты привязаны к теме, тема — к языку программирования; прямого поля
    расширения у Prompt нет). Таблица языков мала, поэтому полный scan приемлем.

    Возвращает ``set[int]`` (пустой, если ни один язык не маппится в расширение —
    значит оператор не завёл ``ProgrammingLanguage`` с нужным именем).
    """
    if not file_extension:
        return set()
    from ..models import ProgrammingLanguage

    ids = set()
    for pl in ProgrammingLanguage.objects.all():
        if _guess_extension(pl.language_name) == file_extension:
            ids.add(pl.id)
    return ids


# Mapping from DL path fragments to local Topic names. The DL course tree
# uses folder names that are close but not identical to Topic.topic_name, so
# we match by substring. Order matters: more specific patterns first.
_PATH_TOPIC_KEYWORDS = [
    # Specific sub-folder names first (both branches share some names like
    # "Условное вычисление выражений" — we distinguish by the branch prefix).
    # HLCCAD sub-folders — match before the generic "hlccad" catch-all.
    ("по логическим функциям", "Логические функции"),
    ("логические функции", "Логические функции"),
    ("по таблицам истинности", "Таблицы истинности"),
    ("таблицам истинности", "Таблицы истинности"),
    ("комбинационн", "Комбинационные схемы"),
    ("простые устройства с памятью", "Устройства с памятью"),
    ("устройства с памятью", "Устройства с памятью"),
    # Plain ASM/CMPA branch sub-folders
    ("обработка строк", "Обработка строк"),
    ("одномерн", "Одномерный массив"),  # "Одномерные числовые массивы"
    ("цифры числа", "Цифры числа"),
    ("цифры числ", "Цифры числа"),
    # "Условное вычисление выражений" appears in both branches.
    # Distinguish by branch prefix in the path:
    #   HLCCAD branch → topic 11, plain branch → topic 2.
    ("hlccad", "HLCCAD - Условное вычисление выражений"),
    ("условное вычисление выражений", "Условное вычисление выражений"),
    ("условные вычисления выражений", "Условные вычисления выражений"),
    # "Простейшая (Программы с подсказками)" — basic ASM tasks (add/sub/mul/
    # div/cmp). Use the ASM topic as default — it has the i8086 syntax rules.
    ("программы с подсказками", "Условные вычисления выражений"),
    ("простейшая", "Условные вычисления выражений"),
]


def _guess_topic_from_path(path: str):
    """Best-effort: find a local Topic from a DL task path.

    Returns the Topic instance or None. The DL path contains folder names
    (e.g. "Программирование [Ассемблер i8086, C-MPA]\\Условное вычисление
    выражений\\...") that correspond to local Topic names. We match by
    keyword substring (case-insensitive) and then look up the Topic by name.
    """
    if not path:
        return None
    from ..models import Topic
    low = path.lower()
    for keyword, topic_name in _PATH_TOPIC_KEYWORDS:
        if keyword in low:
            topic = Topic.objects.filter(topic_name__iexact=topic_name).first()
            if topic:
                return topic
    return None


def ensure_task(node_id, *, programming_language_id=None, topic_id=None, session_id=None, course_id=None):
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
            # Backfill missing topic/extension/statement from DL for pre-existing
            # tasks created before auto-detection was added.
            if session_id and (not task.topic_id or not task.file_extension or not task.statement):
                try:
                    data = fetch_task_info(node_id, session_id=session_id, remove_html_tags=True, course_id=course_id)
                except DLApiError:
                    data = None
                if data:
                    path = data.get("path", "")
                    apply_dl_task_info(task, data)
                    if not task.file_extension and path:
                        guessed = _guess_extension(path)
                        if guessed:
                            task.file_extension = guessed
                            dirty = True
                    if not task.topic_id and path:
                        guessed_topic = _guess_topic_from_path(path)
                        if guessed_topic:
                            task.topic_id = guessed_topic.id
                            dirty = True
            if dirty:
                task.save(update_fields=["programming_language_id", "topic_id", "file_extension", "name", "statement", "task_id"])
            return task

        # Created — best-effort fill name/statement/task_id from DL (once).
        if session_id:
            try:
                data = fetch_task_info(node_id, session_id=session_id, remove_html_tags=True, course_id=course_id)
            except DLApiError:
                data = None
            if data:
                apply_dl_task_info(task, data)
                path = data.get("path", "")
                # If file_extension is still empty, try to guess from path.
                if not task.file_extension and path:
                    guessed = _guess_extension(path)
                    if guessed:
                        task.file_extension = guessed
                # If topic is not set, try to guess from DL path.
                if not task.topic_id and path:
                    guessed_topic = _guess_topic_from_path(path)
                    if guessed_topic:
                        task.topic_id = guessed_topic.id
                task.save(update_fields=["task_id", "name", "statement", "file_extension", "topic"])
        return task
    except Exception:
        logger.exception("ensure_task failed for node_id=%s", node_id)
        return None