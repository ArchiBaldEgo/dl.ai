"""Требования курса к коду решения — по языку программирования.

Единственный источник правды: ограничения, без которых проверка на
dl.gsu.by падает или возвращает НЕРАСПОЗНАВАЕМУЮ причину провала
(«Причина не распознана» на стороне DL):

* С-МПА / Ассемблер i8086 — массивы всегда на 100 элементов: размерность
  из условия модель угадывает неверно, а эталон DL ждёт 100 → «Wrong answer»
  без внятной причины;
* Ассемблер i8086 — только инструкции 8086: расширения 80386/Pentium и
  псевдоинструкции на машине проверки дают «Unknown instruction» (лог
  задачи #2606916, очередь 15231484, DelTA4_NIT1).

Подключается к solve-сообщениям И в пакетном прогоне
(``ai/arm_runner.py::_build_solve_message``), И в чате
(``ai/services/message_composer.py::SolveModeBuilder``) — иначе правило
знает только одна из двух поверхностей (DRY).

Тексты — на языке интерфейса (ru/en/fr), как остальные приписки
(``arm_runner._NO_COMMENTS_INSTRUCTION``).
"""

from __future__ import annotations

# Ключ требований — не расширение, а «язык курса»: .i86 и .asm — один и тот
# же Ассемблер i8086 (расширение→язык — EXTENSION_TO_LANG, task_registry).
_LANG_KEYS = {
    ".mpc": "mpa",
    ".i86": "asm86",
    ".asm": "asm86",
}

# Требования по «языку курса». Порядок слов в приписке одинаков для всех
# языков UI: сначала про массивы, потом про набор инструкций.
_REQUIREMENTS = {
    "mpa": {
        "ru": (
            "Требования языка С-МПА: любой массив объявляй ВСЕГДА на 100 элементов "
            "(например, int a[100];) — размерность 100 обязательна, иначе ответ "
            "не совпадёт с эталоном проверки."
        ),
        "en": (
            "C-MPA language requirements: always declare every array with 100 elements "
            "(for example, int a[100];) — the size of 100 is mandatory, otherwise the "
            "answer will not match the reference solution."
        ),
        "fr": (
            "Exigences du langage C-MPA : déclare toujours chaque tableau avec 100 éléments "
            "(par exemple, int a[100];) — la taille 100 est obligatoire, sinon la réponse "
            "ne correspondra pas à la solution de référence."
        ),
    },
    "asm86": {
        "ru": (
            "Требования языка «Ассемблер i8086»: используй ТОЛЬКО инструкции "
            "процессора 8086 — никаких расширений 80386/Pentium, машинных кодов и "
            "псевдоинструкций (иначе проверка падает с «Unknown instruction»). "
            "Массивы (таблицы) объявляй ВСЕГДА на 100 элементов."
        ),
        "en": (
            "i8086 assembly requirements: use ONLY 8086 processor instructions — "
            "no 80386/Pentium extensions, no machine codes and no pseudo-instructions "
            "(otherwise the check fails with «Unknown instruction»). Always declare "
            "arrays (tables) with 100 elements."
        ),
        "fr": (
            "Exigences de l’assembleur i8086 : utilise UNIQUEMENT les instructions du "
            "processeur 8086 — pas d’extensions 80386/Pentium, pas de codes machine "
            "ni de pseudo-instructions (sinon la vérification échoue avec « Unknown "
            "instruction »). Déclare toujours les tableaux avec 100 éléments."
        ),
    },
}


def language_requirements(lang_name: str = "", file_extension: str = "", ui_suffix: str = "ru") -> str:
    """Требования курса для языка решения — текст приписки или "".

    ``file_extension`` (``.mpc`` / ``.i86`` / ``.asm``) — точный ключ;
    ``lang_name`` (название языка) используется, если расширение неизвестно
    или не передано. ``ui_suffix`` — ru/en/fr (язык интерфейса).
    """
    ext = (file_extension or "").strip().lower()
    key = _LANG_KEYS.get(ext)
    if key is None:
        from .task_registry import language_extension

        key = _LANG_KEYS.get(language_extension(lang_name or ""))
    if key is None:
        return ""
    texts = _REQUIREMENTS[key]
    return texts.get(ui_suffix) or texts["ru"]
