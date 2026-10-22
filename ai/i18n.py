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
        "myModels": "Мои AI-модели",
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
        "myModelsGroup": "Мои модели",
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
        "myModels": "My AI models",
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
        "myModelsGroup": "My models",
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
        "myModels": "Mes modèles IA",
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
        "myModelsGroup": "Mes modèles",
        "allModelsGroup": "Tous les modèles",
        "experimentalModelsGroup": "Modèles expérimentaux",
        "noModels": "Aucun modèle disponible aujourd'hui",
        "task": "Tâche :",
        "taskplace": "Collez ici l'énoncé de la tâche",
        "codetx": "Code du programme :",
        "dlTestButton": "Test",
    },
}


# === Тексты страницы «Мои AI-модели» (клиентская + админская) =============
#
# Панель «Мои AI-модели» одна и та же и в админке (/ai/admin/user-models/),
# и на клиентской странице (/ai/my-models/) — поэтому тексты живут здесь, а
# не в шаблоне. Клиентская часть триязычна (RU/EN/FR — язык только через
# #selectLang, как и остальные страницы чата); админка не переводится и
# получает русский вариант.
_USER_MODELS_STRINGS = {
    "ru": {
        "title": "Мои AI-модели",
        "intro": (
            "Добавьте свою AI-модель и пользуйтесь ей в чате, в «Реши задачу» "
            "и в «Найти ошибку» — она появится в селекторе моделей отдельной "
            "группой «Мои модели». Модель должна работать без VPN: адрес "
            "провайдера должен быть доступен из сети университета без обхода "
            "ограничений. Чужие пользователи вашу модель не видят и не могут "
            "вызвать — она доступна только вам. Ключ API хранится "
            "зашифрованным и нигде не показывается в открытом виде."
        ),
        "newModel": "Новая модель",
        "editModel": "Правка модели",
        "listTitle": "Мои модели",
        "colTitle": "Название",
        "colProvider": "Провайдер",
        "colModelId": "ID модели",
        "colUrl": "Адрес",
        "colKey": "Ключ",
        "colState": "Состояние",
        "fieldTitle": "Название *",
        "fieldProvider": "Провайдер",
        "fieldModelId": "ID модели у провайдера *",
        "fieldBaseUrl": "Базовый URL (для совместимых провайдеров)",
        "fieldApiKey": "Ключ API",
        "fieldMaxTokens": "Потолок ответа",
        "checkVision": "Умеет картинки",
        "checkThink": "Умеет think",
        "checkActive": "Активна",
        "btnSave": "Сохранить модель",
        "btnCancel": "Отменить правку",
        "btnEdit": "Править",
        "btnDelete": "Удалить",
        "empty": "У вас пока нет своих моделей.",
        "keySet": "задан",
        "keyNone": "нет",
        "stateOn": "активна",
        "stateOff": "выключена",
        "hintBaseUrl": "Только https, публичный адрес (не localhost и не внутренняя сеть).",
        "hintApiKey": "Хранится зашифрованным. Чтобы сменить ключ — введите новый.",
        "placeholderTitle": "Например: Моя Qwen3 через OpenRouter",
        "placeholderModelId": "например qwen/qwen3-235b-a22b",
        "placeholderBaseUrl": "https://api.example.com/v1",
        "placeholderApiKey": "оставьте пустым, чтобы не менять",
        "msgSaving": "Сохраняем…",
        "msgDeleting": "Удаляем…",
        "msgSaved": "Сохранено. Обновите страницу, чтобы увидеть модель в списке.",
        "msgDeleteConfirm": "Удалить модель? Её нельзя будет выбрать в чате и на страницах решения.",
        "errSave": "Ошибка сохранения",
        "errDelete": "Ошибка удаления",
    },
    "en": {
        "title": "My AI models",
        "intro": (
            "Add your own AI model and use it in the chat, in “Solve the task” "
            "and in “What is the error?” — it will appear in the model selector "
            "in a separate “My models” group. The model must work without a "
            "VPN: the provider address must be reachable from the university "
            "network without bypassing restrictions. Other users cannot see or "
            "call your model — it is available only to you. The API key is "
            "stored encrypted and is never shown in plain text."
        ),
        "newModel": "New model",
        "editModel": "Edit model",
        "listTitle": "My models",
        "colTitle": "Name",
        "colProvider": "Provider",
        "colModelId": "Model ID",
        "colUrl": "Address",
        "colKey": "Key",
        "colState": "State",
        "fieldTitle": "Name *",
        "fieldProvider": "Provider",
        "fieldModelId": "Model ID at the provider *",
        "fieldBaseUrl": "Base URL (for compatible providers)",
        "fieldApiKey": "API key",
        "fieldMaxTokens": "Response limit",
        "checkVision": "Handles images",
        "checkThink": "Handles think (reasoning)",
        "checkActive": "Active",
        "btnSave": "Save model",
        "btnCancel": "Cancel editing",
        "btnEdit": "Edit",
        "btnDelete": "Delete",
        "empty": "You have no models of your own yet.",
        "keySet": "set",
        "keyNone": "not set",
        "stateOn": "active",
        "stateOff": "disabled",
        "hintBaseUrl": "HTTPS only, public address (not localhost and not the internal network).",
        "hintApiKey": "Stored encrypted. Enter a new key to change it.",
        "placeholderTitle": "For example: My Qwen3 via OpenRouter",
        "placeholderModelId": "for example qwen/qwen3-235b-a22b",
        "placeholderBaseUrl": "https://api.example.com/v1",
        "placeholderApiKey": "leave empty to keep the current key",
        "msgSaving": "Saving…",
        "msgDeleting": "Deleting…",
        "msgSaved": "Saved. Refresh the page to see the model in the list.",
        "msgDeleteConfirm": "Delete the model? It will no longer be selectable in the chat and on the task pages.",
        "errSave": "Save error",
        "errDelete": "Delete error",
    },
    "fr": {
        "title": "Mes modèles IA",
        "intro": (
            "Ajoutez votre propre modèle IA et utilisez-le dans le chat, dans "
            "« Résoudre la tâche » et dans « Quelle est l'erreur ? » — il "
            "apparaîtra dans le sélecteur de modèles, dans un groupe séparé "
            "« Mes modèles ». Le modèle doit fonctionner sans VPN : l'adresse "
            "du fournisseur doit être accessible depuis le réseau de "
            "l'université sans contournement des restrictions. Les autres "
            "utilisateurs ne voient pas votre modèle et ne peuvent pas "
            "l'appeler — il n'est accessible qu'à vous. La clé API est "
            "stockée chiffrée et n'est jamais affichée en clair."
        ),
        "newModel": "Nouveau modèle",
        "editModel": "Modification du modèle",
        "listTitle": "Mes modèles",
        "colTitle": "Nom",
        "colProvider": "Fournisseur",
        "colModelId": "ID du modèle",
        "colUrl": "Adresse",
        "colKey": "Clé",
        "colState": "État",
        "fieldTitle": "Nom *",
        "fieldProvider": "Fournisseur",
        "fieldModelId": "ID du modèle chez le fournisseur *",
        "fieldBaseUrl": "URL de base (pour les fournisseurs compatibles)",
        "fieldApiKey": "Clé API",
        "fieldMaxTokens": "Limite de réponse",
        "checkVision": "Comprend les images",
        "checkThink": "Comprend le think (raisonnement)",
        "checkActive": "Actif",
        "btnSave": "Enregistrer le modèle",
        "btnCancel": "Annuler la modification",
        "btnEdit": "Modifier",
        "btnDelete": "Supprimer",
        "empty": "Vous n'avez pas encore de modèle personnel.",
        "keySet": "définie",
        "keyNone": "absente",
        "stateOn": "actif",
        "stateOff": "désactivé",
        "hintBaseUrl": "HTTPS uniquement, adresse publique (ni localhost ni le réseau interne).",
        "hintApiKey": "Stockée chiffrée. Saisissez une nouvelle clé pour la changer.",
        "placeholderTitle": "Par exemple : Mon Qwen3 via OpenRouter",
        "placeholderModelId": "par exemple qwen/qwen3-235b-a22b",
        "placeholderBaseUrl": "https://api.example.com/v1",
        "placeholderApiKey": "laissez vide pour ne pas changer la clé",
        "msgSaving": "Enregistrement…",
        "msgDeleting": "Suppression…",
        "msgSaved": "Enregistré. Actualisez la page pour voir le modèle dans la liste.",
        "msgDeleteConfirm": "Supprimer le modèle ? Il ne sera plus sélectionnable dans le chat et sur les pages de tâche.",
        "errSave": "Erreur d'enregistrement",
        "errDelete": "Erreur de suppression",
    },
}


def user_models_strings(ui_language: str = "") -> dict:
    """Тексты панели «Мои AI-модели» на языке интерфейса (по умолчанию — RU)."""
    suffix = get_ui_language_suffix(ui_language)
    return _USER_MODELS_STRINGS.get(suffix, _USER_MODELS_STRINGS["ru"])


def user_models_strings_all() -> dict:
    """Все языки панели «Мои AI-модели» (для переключения языка на клиенте).

    Ключи — имена языков селекта #selectLang («Русский»/«English»/«Français»):
    их читает JS-апплаер при смене языка.
    """
    return {
        "Русский": _USER_MODELS_STRINGS["ru"],
        "English": _USER_MODELS_STRINGS["en"],
        "Français": _USER_MODELS_STRINGS["fr"],
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
