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
