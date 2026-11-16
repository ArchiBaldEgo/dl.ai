"""Единый человекочитаемый форматер длительности («1 ч 27 мин» / «12 мин 3 с»
/ «45 с») — единственная копия логики (DRY).

Питает и python-сторону (``ai.admin.logs.run_finish_meta`` — «Общее время»
прогона), и шаблоны журнала запросов («Время ответа») через фильтр
``human_duration``. Нули старших разрядов опускаются: «1 ч 1 мин»,
«1 мин 3 с», «45 с»; секунды старше часа не выводятся.
"""
from django import template

register = template.Library()


def format_human_duration(seconds) -> str:
    """Секунды (float) → «Ч ч М мин» / «М мин С с» / «С с»; пусто/коротко <= 0 → «».

    Дробная часть отсекается (отображение, значение поля не меняется) —
    субсекундный запрос показывается «0 с».
    """
    if not seconds or seconds <= 0:
        return ""
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours} ч {minutes} мин"
    if minutes:
        return f"{minutes} мин" + (f" {secs} с" if secs else "")
    return f"{secs} с"


@register.filter
def human_duration(value):
    """Фильтр-обёртка над format_human_duration — {{ log.duration_seconds|human_duration }}."""
    return format_human_duration(value)