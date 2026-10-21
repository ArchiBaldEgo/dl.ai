"""Границы периодов по МСК — единый владелец (DRY).

Пользователь думает в календарных днях МСК, а ``sent_at`` в БД хранится в
UTC. Фильтр ``sent_at__date__gte`` по колонке с индексом Postgres
не использует индекс (вычисляет выражение по каждой строке) — поэтому
периоды считаем как ДИАПАЗОН по ``sent_at`` с границами полуночи МСК.

Здесь живут две функции:
* ``parse_date`` — разбор строки ``YYYY-MM-DD`` (и «человеческих» форматов)
  в datetime с зоной МСК;
* ``msk_day_bounds`` — границы суток МСК (00:00 → 23:59:59.999) в UTC.

Используют: журнал запросов (список/дневной отчёт), контроль токенов.
"""

from __future__ import annotations

from datetime import datetime, timezone

from django.utils import timezone as dj_timezone

from ..constants import MOSCOW_TZ


def parse_date(value) -> datetime | None:
    """Разобрать дату в datetime (МСК, полночь) — None при мусоре/пусто.

    Поддерживаемые форматы: ``YYYY-MM-DD`` (основной, из URL фильтра),
    ``DD.MM.YYYY`` и ``DD/MM/YYYY`` (как в админке Django).
    """
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return parsed.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=MOSCOW_TZ)
    return None


def msk_day_bounds(day: datetime | None = None) -> tuple[datetime, datetime]:
    """Границы суток по МСК (полночь → 23:59:59.999) в UTC.

    Единый способ считать периоды: ``sent_at`` хранится в UTC, а
    пользователь думает в МСК. Возвращает (начало, конец) в UTC — готовые
    границы для ``__gte``/``__lte`` (индекс-хостильный диапазон, а не
    ``__date``-фильтр).
    """
    if day is None:
        day = dj_timezone.localtime(dj_timezone.now(), MOSCOW_TZ)
    else:
        day = dj_timezone.localtime(day, MOSCOW_TZ)
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    end = day.replace(hour=23, minute=59, second=59, microsecond=999_000)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)
