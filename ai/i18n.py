"""Хелперы локализации UI для моделей AI-приложения.

Поддерживает три языка интерфейса: Русский, English, Français.
Предоставляет функции для получения локализованных имён и текстов полей
моделей (Prompt, Topic, SharedPrompt) на основе выбранного языка UI.
"""

from typing import Any

UI_LANGUAGES = (
    ("ru", "Русский"),
    ("en", "English"),
    ("fr", "Français"),
)

UI_LANGUAGE_TO_SUFFIX = {
    "Русский": "ru",
    "English": "en",
    "Français": "fr",
}

UI_SUFFIX_TO_LANGUAGE = {v: k for k, v in UI_LANGUAGE_TO_SUFFIX.items()}


def get_ui_language_suffix(ui_language: str) -> str:
    """Return the DB suffix (ru/en/fr) for a UI-language display name.

    Falls back to the first two lowercase characters if the exact name is
    not recognized (e.g. 'Russian' -> 'ru').
    """
    suffix = UI_LANGUAGE_TO_SUFFIX.get(ui_language)
    if suffix:
        return suffix
    low = (ui_language or "").lower()
    if low.startswith("ru"):
        return "ru"
    if low.startswith("en"):
        return "en"
    if low.startswith("fr"):
        return "fr"
    return "ru"


def get_localized_name(obj: Any, ui_language: str, default_attr: str = "name") -> str:
    """Return a localized name for the object, falling back to Russian.

    Looks for ``{default_attr}_{suffix}`` first, then the *_ru field.
    Если оба пусты — НЕ ``str(obj)``: модели Prompt/Topic/SharedPrompt
    реализуют ``__str__`` через этот же хелпер, и пустые *_ru поля уводили
    его в бесконечную рекурсию (RecursionError → 500 на /admin/ai/prompt/add/
    и везде, где строки приводятся к str). Безопасный фолбэк — подпись по pk.
    """
    suffix = get_ui_language_suffix(ui_language)
    candidates = [
        f"{default_attr}_{suffix}",
        f"{default_attr}_ru",
    ]
    for attr in candidates:
        value = getattr(obj, attr, None)
        if value:
            return str(value)
    pk = getattr(obj, "pk", None)
    if pk is None:
        return obj.__class__.__name__
    return f"{obj.__class__.__name__} #{pk}"


def get_localized_text(obj: Any, ui_language: str, default_attr: str = "text") -> str:
    """Return a localized text for the object, falling back to *_ru."""
    suffix = get_ui_language_suffix(ui_language)
    candidates = [
        f"{default_attr}_{suffix}",
        f"{default_attr}_ru",
    ]
    for attr in candidates:
        value = getattr(obj, attr, None)
        if value:
            return str(value)
    return ""


def get_language_instruction(ui_language: str) -> str:
    """Return a language constraint instruction for model prompts."""
    if ui_language == "Русский":
        return "\n\nОтвечай только по-русски."
    if ui_language == "Français":
        return "\n\nRéponds uniquement en français."
    if ui_language == "English":
        return "\n\nRespond only in English."
    return ""


_SHARED_PROMPT_PREFIX = {
    "ru": "[Общий]",
    "en": "[Shared]",
    "fr": "[Partagé]",
}


def get_shared_prompt_prefix(ui_language: str = "") -> str:
    """Return the localized prefix for SharedPrompt.__str__."""
    suffix = get_ui_language_suffix(ui_language)
    return _SHARED_PROMPT_PREFIX.get(suffix, _SHARED_PROMPT_PREFIX["ru"])


# === Клиентский UI трёх пользовательских страниц (чат / реши / ошибка) ===
#
# Правило (CLAUDE.md): весь пользовательский текст клиентской части
# триязычен (RU/EN/FR). Ключи зеркалят словарь ``localization`` в
# static/admin/js/ai-common.js (там же живёт applier при смене #selectLang);
# server-side словарь нужен первому рендеру шаблона (до onload-диспетча
# change), а также строкам, которые JS-апплаеры не переписывают
# (optgroup-лейблы селектора моделей, модалка справки, aria-метки).
_CLIENT_STRINGS = {
    "ru": {
        "send": "Отправить",
        "clear": "Очистить контекст",
        "placeholder": (
            "Задайте вопрос (желательно на английском во избежание ошибок), "
            "для красивого форматирования оберните код в ```(буква Ё на клавиатуре)\n"
            "Пример форматирования кода:\n```\nprint('Hello, world!')\n```"
        ),
        "codePlaceholder": (
            "Вставьте сюда ваш код решения задачи, для красивого форматирования "
            "оберните код в ```(буква Ё на клавиатуре)"
        ),
        "adminPanel": "Админ-Панель",
        "chat": "Чат с DLAI",
        "decideTask": "Реши задачу",
        "findError": "В чём ошибка?",
        "enterHint": "При нажатии на Enter будет отправляться вопрос (для переноса строки Enter+Shift)",
        "preprompt": "Промпт",
        "themeToggle": "Сменить тему",
        "openMenu": "Открыть меню",
        "lastUpdate": "Последнее обновление",
        "userDocs": "Инструкция пользователя",
        "docsTour": "Подсказки",
        "docsDownload": "Скачать .md",
        "docsClose": "Закрыть",
        "voiceMode": "Голосовой режим",
        "voiceInput": "Голосовой ввод",
        "voiceOutput": "Озвучить ответ",
        "speakThinkLabel": "Озвучивать дополнительную информацию",
        "sortDefault": "Порядок: по алфавиту",
        "sortSpeed": "Порядок: по скорости",
        "sortAccuracy": "Порядок: по точности",
        "sortBoth": "Порядок: скорость + точность",
        "favoritesGroup": "Часто используемые",
        "allModelsGroup": "Все модели",
        "experimentalModelsGroup": "Экспериментальные модели",
        "noModels": "Сегодня нет доступных моделей",
        "task": "Задача:",
        "taskplace": "Вставьте сюда условие задачи",
        "codetx": "Код программы:",
        "dlTestButton": "Тестирование",
    },
    "en": {
        "send": "Send",
        "clear": "Clear Context",
        "placeholder": (
            "Ask a question (preferably in English to avoid errors), for nice "
            "formatting wrap the code in ```\nExample of code formatting:\n```\n"
            "print('Hello, world!')\n```"
        ),
        "codePlaceholder": (
            "Paste your solution code here, for nice formatting wrap the code "
            "in ``` (the 'Ё' key on the keyboard)"
        ),
        "adminPanel": "Admin Panel",
        "chat": "Chat with DLAI",
        "decideTask": "Solve the task",
        "findError": "What's the error?",
        "enterHint": "Press Enter to send the question (Shift+Enter for a new line)",
        "preprompt": "Prompt",
        "themeToggle": "Toggle theme",
        "openMenu": "Open menu",
        "lastUpdate": "Last updated",
        "userDocs": "User guide",
        "docsTour": "Guide tips",
        "docsDownload": "Download .md",
        "docsClose": "Close",
        "voiceMode": "Voice mode",
        "voiceInput": "Voice input",
        "voiceOutput": "Speak answer",
        "speakThinkLabel": "Voice extra information",
        "sortDefault": "Order: alphabetical",
        "sortSpeed": "Order: by speed",
        "sortAccuracy": "Order: by accuracy",
        "sortBoth": "Order: speed + accuracy",
        "favoritesGroup": "Frequently used",
        "allModelsGroup": "All models",
        "experimentalModelsGroup": "Experimental models",
        "noModels": "No models available today",
        "task": "Task:",
        "taskplace": "Paste the task condition here",
        "codetx": "Program code:",
        "dlTestButton": "Testing",
    },
    "fr": {
        "send": "Envoyer",
        "clear": "Effacer le contexte",
        "placeholder": (
            "Posez une question (de préférence en anglais pour éviter les erreurs), "
            "pour un bon formatage, encadrez le code dans ```\n"
            "Exemple de formatage du code:\n```\nprint('Hello, world!')\n```"
        ),
        "codePlaceholder": (
            "Collez ici votre code de la solution, pour un bon formatage, "
            "encadrez le code dans ``` (touche 'Ё' du clavier)"
        ),
        "adminPanel": "Panneau Admin",
        "chat": "Chat avec DLAI",
        "decideTask": "Résoudre la tâche",
        "findError": "Quelle est l'erreur?",
        "enterHint": "Appuyez sur Entrée pour envoyer la question (Shift+Entrée pour une nouvelle ligne)",
        "preprompt": "Prompt",
        "themeToggle": "Changer le thème",
        "openMenu": "Ouvrir le menu",
        "lastUpdate": "Dernière mise à jour",
        "userDocs": "Guide de l'utilisateur",
        "docsTour": "Aide",
        "docsDownload": "Télécharger .md",
        "docsClose": "Fermer",
        "voiceMode": "Mode vocal",
        "voiceInput": "Saisie vocale",
        "voiceOutput": "Lire la réponse",
        "speakThinkLabel": "Informations supplémentaires vocales",
        "sortDefault": "Ordre : alphabétique",
        "sortSpeed": "Ordre : par vitesse",
        "sortAccuracy": "Ordre : par précision",
        "sortBoth": "Ordre : vitesse + précision",
        "favoritesGroup": "Fréquemment utilisés",
        "allModelsGroup": "Tous les modèles",
        "experimentalModelsGroup": "Modèles expérimentaux",
        "noModels": "Aucun modèle disponible aujourd'hui",
        "task": "Tâche :",
        "taskplace": "Collez ici l'énoncé de la tâche",
        "codetx": "Code du programme :",
        "dlTestButton": "Test",
    },
}


def client_strings(ui_language: str = "") -> dict:
    """Словарь клиентских шаблонных строк ({ключ: текст}) для первого рендера.

    Язык — как у ``#selectLang`` («Русский»/«English»/«Français» или пусто
    до первого выбора пользователя); при неизвестном значении — русский.
    """
    suffix = get_ui_language_suffix(ui_language)
    return _CLIENT_STRINGS.get(suffix, _CLIENT_STRINGS["ru"])


# === Серверные тексты WS-протокола (consumers.py, трёхязычная клиентская часть)===
#
# Русские формулировки менять НЕЛЬЗЯ: фронтенд-фолбэки сниффят их подстроки
# («Запрос успешно обработан», «Обрабатываю запрос пользователя», «Контекст
# очищен» — isTerminalAiMessage/_isPersistableWsMessage в ai-common.js) и
# чистят как служебные (servicePatterns). Терминальность/персистенция для
# EN/FR несёт trailing-маркер «[[ai-status:<status>]]» (страницы срезают
# маркер перед рендером, но хранят в localStorage вместе с сообщением).
_WS_STRINGS = {
    "ru": {
        "processing": "Обрабатываю запрос пользователя. Вы: {message}",
        "success_head": "Запрос успешно обработан",
        "success_full": (
            "Модель: {model}\n"
            "Время обработки запроса: {duration}\n"
            "Потрачено токенов: {tokens}\n"
            "{response}"
        ),
        "cleared": "Контекст очищен",
        "invalid_json": "Ошибка: Неверный формат JSON",
        "rate_limit": "Слишком много сообщений. Попробуйте позже.",
        "something_wrong": "Что-то пошло не так. {friendly}",
        "error_prefix": "Ошибка при обработке запроса: {friendly}",
        "empty_response": "Модель вернула пустой ответ",
        "model_not_found": "Модель {model_key} не найдена. Используйте доступные модели.",
        "cached_processing": "Задача взята из базы данных (кэш решённых задач).",
        "cached_note": (
            "Задача взята из базы данных: она уже была решена ранее "
            "(тестирование пройдено) — выдаю сохранённый код без обращения "
            "к модели.\n\n```{code}```"
        ),
        "cached_title": "Сохранённое решение (из БД)",
        "cached_log_note": "(решение выдано из кэша)",
        "dur_sec": "{value} сек",
        "dur_min_sec": "{minutes} мин {seconds} сек",
    },
    "en": {
        "processing": "Processing your request. You: {message}",
        "success_head": "Request processed successfully",
        "success_full": (
            "Model: {model}\n"
            "Request processing time: {duration}\n"
            "Tokens spent: {tokens}\n"
            "{response}"
        ),
        "cleared": "Context cleared",
        "invalid_json": "Error: Invalid JSON format",
        "rate_limit": "Too many messages. Please try again later.",
        "something_wrong": "Something went wrong. {friendly}",
        "error_prefix": "Request processing error: {friendly}",
        "empty_response": "The model returned an empty response",
        "model_not_found": "Model {model_key} not found. Please use the available models.",
        "cached_processing": "The task was taken from the database (cache of solved tasks).",
        "cached_note": (
            "The task was taken from the database: it has already been solved "
            "earlier (testing passed) — returning the saved code without "
            "calling the model.\n\n```{code}```"
        ),
        "cached_title": "Saved solution (from DB)",
        "cached_log_note": "(solution served from cache)",
        "dur_sec": "{value} s",
        "dur_min_sec": "{minutes} min {seconds} s",
    },
    "fr": {
        "processing": "Traitement de votre requête. Vous : {message}",
        "success_head": "Requête traitée avec succès",
        "success_full": (
            "Modèle : {model}\n"
            "Durée du traitement de la requête : {duration}\n"
            "Jetons dépensés : {tokens}\n"
            "{response}"
        ),
        "cleared": "Contexte effacé",
        "invalid_json": "Erreur : format JSON invalide",
        "rate_limit": "Trop de messages. Réessayez plus tard.",
        "something_wrong": "Une erreur est survenue. {friendly}",
        "error_prefix": "Erreur lors du traitement de la requête : {friendly}",
        "empty_response": "Le modèle a renvoyé une réponse vide",
        "model_not_found": "Modèle {model_key} introuvable. Veuillez utiliser les modèles disponibles.",
        "cached_processing": "La tâche a été extraite de la base de données (cache des tâches résolues).",
        "cached_note": (
            "La tâche a été extraite de la base de données : elle a déjà été résolue "
            "auparavant (test réussi) — je renvoie le code enregistré sans appeler "
            "le modèle.\n\n```{code}```"
        ),
        "cached_title": "Solution enregistrée (depuis la BD)",
        "cached_log_note": "(solution servie depuis le cache)",
        "dur_sec": "{value} s",
        "dur_min_sec": "{minutes} min {seconds} s",
    },
}


def ws_text(language: str, key: str, **params) -> str:
    """Локализованный серверный текст WS-протокола (см. _WS_STRINGS).

    ``language`` — имя из #selectLang («Русский»/«English»/«Français») или
    пусто/неизвестно → русский. ``params`` — плейсхолдеры шаблона.
    """
    suffix = get_ui_language_suffix(language)
    table = _WS_STRINGS.get(suffix, _WS_STRINGS["ru"])
    template = table.get(key)
    if not template:
        return key
    return template.format(**params) if params else template
