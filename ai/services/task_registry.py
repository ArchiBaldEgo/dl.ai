"""Авто-регистрация DL-задач, решаемых через страницу чата.

Когда пользователь решает DL-задачу на /ai/solve-problem/, WebSocket consumer
вызывает ensure_task, чтобы задача появилась в локальной таблице Task
и стала доступна для batch-solve ARM. Извлечение полей DL — общее с
TaskAdmin.refresh_from_dl через apply_dl_task_info (DRY).
"""

import logging
import re

from ..constants import DL_DEFAULT_COURSE_ID
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
    # Путь в дереве задач DL (get-task-info → path): нужен для честной
    # подписи темы в отчёте пакетного решения (папка DL вместо имени задачи).
    path = (data.get("path") or "")[:512]
    if path:
        task.tree_path = path


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
# соответствующих им названий языков для промпта. Пользователь выбирает
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
    (".asm", "Ассемблер i8086"),
    # .mpc (не .cmp) — DL REST send-solution принимает именно .mpc
    # (см. миграцию 0032_cmp_to_mpc).
    (".mpc", "C-MPA (С-МПА)"),
    (".v", "Verilog"),
]

# Обратный map: расширение → название языка (для промпта в batch-solve).
# Единственный источник этого соответствия (DRY) — arm_runner и админка
# импортируют EXTENSION_TO_LANG, а не держат свои копии.
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


def solve_language_options():
    """Языки программирования для селектора /arm/solve/.

    Возвращает ``[{"id", "name", "extension"}]`` — только языки, чей
    ``_guess_extension(language_name)`` входит в ``EXTENSION_TO_LANG``
    (то есть допущен к пакетному решению). Расширение на странице выводится
    из языка автоматически (readonly), поэтому единый источник соответствия —
    снова ``_guess_extension`` (DRY).
    """
    from ..models import ProgrammingLanguage

    options = []
    for pl in ProgrammingLanguage.objects.all():
        ext = _guess_extension(pl.language_name)
        if ext in EXTENSION_TO_LANG:
            options.append({"id": pl.id, "name": pl.language_name, "extension": ext})
    options.sort(key=lambda opt: opt["name"].lower())
    return options


# Mapping from DL path fragments to local Topic names. The DL course tree
# uses folder names that are close but not identical to Topic.topic_name_ru, so
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


def _guess_topic_from_path(path: str, programming_language_id=None):
    """Best-effort: find a local Topic from a DL task path.

    Returns the Topic instance or None. Три прохода:
    1. Точное сопоставление каждого сегмента пути (название ветки/папки из
       get-task-info ``path``) с ``Topic.topic_name_ru`` (без учёта регистра) —
       покрывает темы, чьё название совпадает с веткой целиком (напр.
       «Простейшая (Программы с подсказками)»).
    2. Нечёткое совпадение: сегмент пути СОДЕРЖИТ название темы (или
       наоборот) — названия папок DL часто лишь БЛИЗКИ к теме
       («Одномерные числовые массивы» vs «Одномерный массив»). Этот
       проход не требует хардкод-списка: он работает с ЛЮБОЙ темой, которую
       оператор завёл в админке, — поэтому «Без темы» в отчётах прогона
       появляется заметно реже.
    3. Fallback — keyword-подстроки из ``_PATH_TOPIC_KEYWORDS`` (исторические
       исключения, которые не ловит нечёткое совпадение).

    Все три прохода смотрят только на ПАПКИ пути: последний сегмент пути DL —
    имя задачи, а не тема (иначе задача «Сумма цифр числа» давала тему
    «Цифры числа»).

    Темы per-языковые (курс «[Ассемблер i8086, C-MPA]» держит два Topic с
    одинаковым названием — по одному на язык), поэтому при известном языке
    ищем только среди тем этого языка. Совпадений в языке нет → None:
    чужая тема хуже отсутствующей (с None резолв привязки честно уйдёт
    «на весь язык», а плейсхолдер {topic} не получит чужое название).
    """
    if not path:
        return None
    from ..models import Topic

    topics = Topic.objects.all()
    if programming_language_id is not None:
        topics = topics.filter(programming_language_id=programming_language_id)

    # Точное совпадение сегмента с topic_name (порядок — от глубоких папок к
    # корню: более конкретная тема приоритетна).
    segments = re.split(r"[\\/|]", path)
    # Последний сегмент пути DL — ИМЯ ЗАДАЧИ, а тема — это ПАПКА дерева.
    # Без этого имя задачи («Сумма цифр числа») находило тему «Цифры
    # числа» — отсюда «неправильно взяты темы» в отчёте прогона.
    folder_segments = segments[:-1] if len(segments) > 1 else segments
    for segment in reversed(folder_segments):
        name = segment.strip()
        if not name:
            continue
        topic = topics.filter(topic_name_ru__iexact=name).first()
        if topic:
            return topic

    # Нечёткое совпадение (проход 2): тема, чьё название входит в
    # название папки DL (или наоборот). Длина названия темы — от 4
    # символов, чтобы короткие совпадения («СИ») не давали ложную тему.
    fuzzy = _fuzzy_topic_from_segments(topics, folder_segments)
    if fuzzy is not None:
        return fuzzy

    low = "\\".join(folder_segments).lower()
    for keyword, topic_name in _PATH_TOPIC_KEYWORDS:
        if keyword in low:
            topic = topics.filter(topic_name_ru__iexact=topic_name).first()
            if topic:
                return topic
    return None


def _fuzzy_topic_from_segments(topics, segments):
    """Тема, название которой входит в сегмент пути DL (или наоборот).

    Возвращает Topic или None. Поиск идёт от глубоких сегментов к корню
    (как и точное совпадение) и среди самых длинных названий (самая
    специфичная тема приоритетна) — иначе «Одномерный массив» победил бы
    «Массивы» в папке «Одномерные числовые массивы».
    """
    candidates = [
        (t.id, (t.topic_name_ru or "").strip())
        for t in topics.only("id", "topic_name_ru")
    ]
    # Темы с названием короче 4 символов — слишком шумны для нечёткого
    # совпадения (иначе «СИ» нашлось бы в любой папке).
    candidates = [(tid, name) for tid, name in candidates if len(name) >= 4]
    if not candidates:
        return None
    candidates.sort(key=lambda item: (-len(item[1]), item[0]))
    by_id = {tid: name for tid, name in candidates}
    for segment in reversed(segments):
        seg = segment.strip().lower()
        if len(seg) < 4:
            continue
        for tid, name in candidates:
            low_name = name.lower()
            if low_name in seg or seg in low_name:
                # Возвращаем Topic-объект: угадавший — по id из выборки.
                return topics.filter(pk=tid).first()
    return None


def dl_path_folder(path):
    """Папка DL задачи по её пути в дереве (``предпоследний`` сегмент).

    Путь DL выглядит как «Программирование [..]\\Одномерные массивы\\Сумма цифр
    числа» — последний сегмент это ИМЯ ЗАДАЧИ, а папка (реальная «тема»
    дерева DL) — предпоследний. Пусто, если путь короче двух сегментов.
    """
    if not path:
        return ""
    segments = [s.strip() for s in re.split(r"[\\/|]", path) if s.strip()]
    if len(segments) < 2:
        return ""
    return segments[-2]


def dl_tree_path_map(course_id):
    """{node_id: "путь\\в\\дереве\\DL"} по закэшированному дереву курса.

    Дерево курса уже кэшируется в Redis при загрузке на форме /arm/solve/
    (ai/admin/arm.py::admin_arm_solve_load_tree_view) — пути строятся из
    кэша, БЕЗ дополнительных запросов к DL. Формат пути совпадает с
    get-task-info («папки\\папка\\имя задачи»).

    Нужен, чтобы у задач прогона была реальная ветка DL (Task.tree_path), а
    в отчёте пакетного решения — честная подпись темы (папка DL), а не имя
    задачи. Кэша нет/Redis недоступен → пустая карта (не ломаем прогон).
    """
    if not course_id:
        return {}
    from django.core.cache import cache
    from ..constants import AI_CACHE_KEY_PREFIX

    try:
        data = cache.get(f"{AI_CACHE_KEY_PREFIX}:dl_tree:{course_id}")
    except Exception:  # pragma: no cover — Redis недоступен
        logger.warning("dl_tree_path_map: cache unavailable for course %s", course_id)
        return {}
    tree = (data or {}).get("tree") or []
    paths: dict = {}

    def _walk(nodes, prefix):
        for node in nodes or []:
            if not isinstance(node, dict):
                continue
            name = str(node.get("name") or "").strip()
            children = node.get("children") or []
            if node.get("isFolder", False):
                _walk(children, prefix + ([name] if name else []))
                continue
            node_id = node.get("nodeId")
            if node_id is None:
                continue
            parts = prefix + ([name] if name else [])
            paths[str(node_id)] = "\\".join(parts)

    _walk(tree, [])
    return paths


def ensure_task(node_id, *, programming_language_id=None, topic_id=None, session_id=None, course_id=None, fallback_topic_id=None):
    """Get-or-create a ``Task`` row for a DL node id; best-effort DL fill on create.

    Used by the chat consumer when a user solves a DL task. Never raises —
    registration must not break the chat. ``file_extension`` is intentionally
    left blank (it cannot be derived from ``ProgrammingLanguage``'s display
    name and is required for ``fetch_task_solution``); the operator fills it
    and activates the task. Auto-created tasks are ``active=False`` so they do
    not clutter batch-solve "all active" runs while still ungradeable.

    When ``programming_language_id`` is given (ARM batch-solve passes the
    run form's language), the task's language follows it and a topic of a
    different language is re-guessed from the DL path scoped to that language
    (topics are per-language; a stale foreign-language topic would make
    ArmPromptBinding.resolve miss the exact binding).

    ``fallback_topic_id`` — тема прогона (выбранная оператором на форме
    /arm/solve/). Используется ТОЛЬКО как запасной вариант: если тему
    задачи не удалось определить из ветки DL, задача наследует тему
    прогона (того же языка). Без этого в отчёте пакетного решения появлялись
    строки «Без темы» вместо реально задействованных тем. Явно заданная
    тема задачи (``topic_id``) всегда важнее.
    """
    try:
        from ..models import Topic

        # Auto-determine file_extension from programming language if provided.
        file_ext = ""
        if programming_language_id is not None:
            from ..models import ProgrammingLanguage
            try:
                pl = ProgrammingLanguage.objects.get(pk=programming_language_id)
                file_ext = _guess_extension(pl.language_name)
            except ProgrammingLanguage.DoesNotExist:
                pass

        # Запасная тема прогона — берём только если она того же языка
        # (темы per-языковые; чужая тема хуже отсутствующей).
        run_topic_id = None
        if fallback_topic_id is not None:
            run_topic = Topic.objects.filter(pk=fallback_topic_id).first()
            if run_topic is not None and (
                programming_language_id is None
                or run_topic.programming_language_id is None
                or run_topic.programming_language_id == programming_language_id
            ):
                run_topic_id = run_topic.id

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
            # Тема per-языковая: тема чужого языка не имеет смысла для прогона
            # на другом языке (привязки ArmPromptBinding задаются per-язык) —
            # её нужно перегадать из path. NULL-язык темы не считается stale
            # (не ломаем легитимные безязыковые темы).
            topic_lang_id = (
                Topic.objects.filter(pk=task.topic_id)
                .values_list("programming_language_id", flat=True).first()
                if task.topic_id is not None else None
            )
            topic_stale = (
                programming_language_id is not None
                and topic_lang_id is not None
                and topic_lang_id != programming_language_id
            )
            if programming_language_id is not None and task.programming_language_id != programming_language_id:
                task.programming_language_id = programming_language_id
                dirty = True
            if topic_id is not None and task.topic_id != topic_id:
                task.topic_id = topic_id
                dirty = True
            # Backfill missing topic/extension/statement from DL for pre-existing
            # tasks created before auto-detection was added. topic_stale тоже
            # требует фэтча: перегадать тему можно только из path.
            # tree_path НЕ требует фэтча: его заполняет воркер прогона из
            # кэша дерева курса (dl_tree_path_map) — без обращения к DL.
            if session_id and (
                not task.topic_id or topic_stale or not task.file_extension
                or not task.statement
            ):
                try:
                    data = fetch_task_info(node_id, session_id=session_id, remove_html_tags=True, course_id=course_id)
                except DLApiError:
                    data = None
                if data:
                    path = data.get("path", "")
                    apply_dl_task_info(task, data)
                    dirty = True  # фэтч дорого — его результат обязан сохраниться
                    if not task.file_extension and path:
                        guessed = _guess_extension(path)
                        if guessed:
                            task.file_extension = guessed
                    if (not task.topic_id or topic_stale) and path:
                        guessed_topic = _guess_topic_from_path(
                            path, programming_language_id=programming_language_id,
                        )
                        if guessed_topic and guessed_topic.id != task.topic_id:
                            task.topic_id = guessed_topic.id
                            dirty = True
                        elif not guessed_topic and topic_stale:
                            # В этом языке темы нет: чужая тема хуже отсутствующей —
                            # сбрасываем, резолв привязки уйдёт «на весь язык».
                            task.topic_id = None
                            dirty = True
            # Тема так и не определилась — наследуем тему прогона (того же
            # языка), чтобы в отчёте была реальная тема, а не «Без темы».
            if task.topic_id is None and run_topic_id is not None:
                task.topic_id = run_topic_id
                dirty = True
            if dirty:
                task.save(update_fields=["programming_language_id", "topic_id", "file_extension", "name", "statement", "task_id", "tree_path"])
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
                    guessed_topic = _guess_topic_from_path(
                        path, programming_language_id=programming_language_id,
                    )
                    if guessed_topic:
                        task.topic_id = guessed_topic.id
                task.save(update_fields=["task_id", "name", "statement", "file_extension", "topic", "tree_path"])
        # Созданная задача: тема не нашлась по ветке DL — берём тему прогона.
        if task.topic_id is None and run_topic_id is not None:
            task.topic_id = run_topic_id
            task.save(update_fields=["topic"])
        return task
    except Exception:
        logger.exception("ensure_task failed for node_id=%s", node_id)
        return None

def dl_task_url(node_id, course_id=None):
    """Пользовательская ссылка на задачу в DL (не admin-вьювер).

    ``/task.jsp?nid=<узел>&cid=<курс>`` (nid первым, cid вторым). cid —
    это дерево задач: приписка ``&cid=`` ставится ВСЕГДА — если курс записи
    не известен (старые записи кэша/журнала), подставляем единственное
    дерево DL (DL_DEFAULT_COURSE_ID), иначе DL откроет задачу без контекста
    дерева. Просмотр условия от имени админа DL (fullTaskviewer) не
    используется. Единственное определение: его импортируют и admin-модули
    (request_logs, models), и arm_runner (dl_url на result-item'ах).
    """
    if not node_id:
        return None
    cid = course_id or DL_DEFAULT_COURSE_ID
    return f"https://dl.gsu.by/task.jsp?nid={node_id}&cid={cid}"
