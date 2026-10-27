"""Учёт токенов: единственный владелец агрегации (страница «Контроль токенов»).

Источники данных (владелец решил: оба):

1. ``AIRequestLog`` — запросы чата / «реши задачу» / «найти ошибку» / пакетного
   решения (одна запись на прогон ARM — токены прогона суммируются в неё).
2. ``AIModelTestResult`` — отдельные пары задача×модель пакетного прогона
   (подробная разбивка ARM-расхода по моделям/темам/задачам).

СЧЁТ: ``tokens`` — токены ОТВЕТА (completion), ``tokens_prompt`` — токены
ПРОМПТА (вход). Итог = сумма обеих величин. Провайдеры, не отдающие usage
(web-пулы Web_DeepSeek / Web_Kimi), дают 0 — такие строки честно помечаются
«учёт недоступен», а общая сумма по ним считается НЕПОЛНОЙ (см. флаг
``incomplete`` в срезе).

Честность покрытия — важная часть контракта: страница не должна создавать
впечатление, что учёт полный, когда часть моделей токены не отдаёт.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from django.db.models import (
    BooleanField,
    Case,
    CharField,
    Count,
    IntegerField,
    Max,
    Q,
    Sum,
    Value,
    When,
)
from django.db.models.functions import Coalesce, NullIf, TruncDay
from django.utils import timezone

from ..constants import MOSCOW_TZ
from .msk_time import msk_day_bounds, parse_date  # noqa: F401 (реэкспорт)

# Модели, которые не отдают usage: токены по ним всегда 0. Список — из
# префиксов ключей (см. ai/model_clients/registry.py).
WEB_POOL_PREFIXES = ("Web_", "Web ")

# Режимы, которые попадают в отчёт (все, что есть в AIRequestLog.MODE_CHOICES).
MODE_LABELS = {
    "chat": "Чат",
    "solve": "Решить задачу",
    "find_error": "Найти ошибку",
    "arm": "ARM",
    "batch_solve": "Пакетное решение",
    "testing": "Тестирование",
}

# Ключ среза «по моделям» для сводной записи прогона, когда включён фильтр
# по модели: расход прогона относится к нескольким моделям сразу, отнести
# его к одной — значит завысить её счётчик. Считаем отдельной «корзиной».
MULTI_MODEL_LABEL = "несколько моделей (прогон)"


def is_web_pool_model(name: str) -> bool:
    """Модель из web-пула (Puppeteer) — токены ею не отдаются."""
    text = str(name or "").strip()
    return any(text.startswith(prefix) for prefix in WEB_POOL_PREFIXES)


@dataclass
class TokenSlice:
    """Срез агрегации: одна строка отчёта (модель / пользователь / режим / день)."""

    key: str
    label: str
    completion_tokens: int = 0
    prompt_tokens: int = 0
    requests: int = 0
    models: set = field(default_factory=set)
    incomplete: bool = False

    @property
    def total_tokens(self) -> int:
        return (self.completion_tokens or 0) + (self.prompt_tokens or 0)

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "completion_tokens": self.completion_tokens,
            "prompt_tokens": self.prompt_tokens,
            "total_tokens": self.total_tokens,
            "requests": self.requests,
            "models": sorted(self.models),
            "incomplete": self.incomplete,
        }


def _add(slice_: TokenSlice, completion: int, prompt: int, model_name: str = "") -> None:
    slice_.completion_tokens += int(completion or 0)
    slice_.prompt_tokens += int(prompt or 0)
    slice_.requests += 1
    if model_name:
        slice_.models.add(model_name)
        if is_web_pool_model(model_name):
            slice_.incomplete = True


def _sort_slices(slices: Iterable[TokenSlice]) -> list[dict]:
    rows = [s.as_dict() for s in slices]
    rows.sort(key=lambda row: (-row["total_tokens"], row["label"]))
    return rows


def _collect_models(slice_: TokenSlice, names: Iterable[str]) -> None:
    """Отметить модели среза (без токенов) — состав слага прогона.

    Токены сводной записи НЕ раскладываются по моделям (см.
    ``collect_log_slices``), но какие модели входили в прогон — знать
    полезно: в таблице видно, чей расход стоит в этой строке.
    """
    for name in names:
        text = str(name)
        slice_.models.add(text)
        if is_web_pool_model(text):
            slice_.incomplete = True


def _filter_logs(qs, *, date_from, date_to, mode: str, model: str, user: str, source: str):
    """Фильтры журнала запросов (диапазон дат — по МСК, индекс-хостильно).

    Границы — через ``msk_day_bounds`` (единый владелец, ai/services/msk_time.py):
    сутки МСК целиком (00:00 → 23:59:59.999), конвертированные в UTC.
    Раньше здесь стояло ``date_to.astimezone(timezone.utc)`` — это полночь
    выбранного дня, из-за чего записи ПОСЛЕ 00:00 МСК отсекались; к тому же
    ``django.utils.timezone.utc`` удалён в Django 5.0 (AttributeError → 500).
    """
    if date_from:
        day_start, _ = msk_day_bounds(date_from)
        qs = qs.filter(sent_at__gte=day_start)
    if date_to:
        _, day_end = msk_day_bounds(date_to)
        qs = qs.filter(sent_at__lte=day_end)
    if mode:
        qs = qs.filter(mode=mode)
    if source:
        qs = qs.filter(source=source)
    if user:
        qs = qs.filter(
            Q(user_full_name__icontains=user)
            | Q(username__icontains=user)
            | Q(external_user_id__icontains=user)
        )
    if model:
        # model_names — JSONField-список; JSON-индексов нет, поэтому фильтр
        # делается в Python (выборка ограничена фильтрами и периодом).
        pass
    return qs


def _log_who_expression():
    """SQL-выражение «кто»: ФИО → логин → внешний id → «—».

    NullIf+Coalesce, а не просто Coalesce: колонки — CharField(blank=True),
    то есть пустое значение там "", а не NULL, и обычный Coalesce вернул бы
    пустую строку вместо логина. Значение «—» — тот же фолбэк, что и в
    Python-версии (row["user_full_name"] or … or "—").
    """
    return Coalesce(
        NullIf("user_full_name", Value("")),
        NullIf("username", Value("")),
        NullIf("external_user_id", Value("")),
        Value("—"),
        output_field=CharField(),
    )


def _log_day_expression():
    """SQL-выражение «день МСК» (TruncDay с tzinfo) — вместо Python-разбора."""
    return TruncDay("sent_at", tzinfo=MOSCOW_TZ)


def _web_pool_filter():
    """SQL-фильтр «в выборке есть web-пул» (модель не отдаёт usage).

    Ищем по JSON-тексту с ОТКРЫВАЮЩЕЙ кавычкой: ``"Web_`` / ``"Web ``.
    Так ловится только элемент списка, НАЧИНАЮЩИЙСЯ с префикса
    (``["Web_DeepSeek"]``), а не модель с «Web» в середине названия
    (``["My Web_X"]``) — ровно семантика ``is_web_pool_model``.

    Работает и на Postgres (UPPER(model_names::text) LIKE), и на SQLite
    (UPPER(model_names) LIKE) — в отличие от ``__contains`` по JSONField,
    который на SQLite не поддерживается вовсе.
    """
    condition = Q()
    for prefix in WEB_POOL_PREFIXES:
        condition |= Q(model_names__icontains=f'"{prefix}')
    return condition


def _names_of(row):
    """Список названий моделей из сгруппированной строки (JSON-список)."""
    names = row["model_names"] or []
    if isinstance(names, str):
        names = [names]
    if not names:
        names = ["—"]
    return [str(n) for n in names]


def _matches_model(names, model: str) -> bool:
    """Есть ли в наборе модель, содержащая подстроку фильтра (без учёта регистра).

    Семантика — как в эталонной Python-агрегации: подстрока ищется ВНУТРИ
    одного названия модели, а не по JSON-тексту целиком (иначе фильтр «A, B»
    мог бы совпасть через границу двух элементов списка).
    """
    if not model:
        return True
    needle = model.lower()
    return any(needle in name.lower() for name in names)


def _merge_groups(rows, key_of, label_of, *, model: str = ""):
    """Свернуть сгруппированные строки в словарь срезов (по измерению).

    ``rows`` — результат GROUP BY (измерение, model_names) с суммами;
    ``key_of``/``label_of`` — как получить ключ и подпись среза. Набор
    моделей и признак неполноты (web-пул) собираются по группам — так
    результат совпадает с эталонной Python-агрегацией (см.
    ``_collect_log_slices_legacy``).
    """
    slices: dict = {}
    for row in rows:
        names = _names_of(row)
        if not _matches_model(names, model):
            continue
        key = key_of(row)
        slice_ = slices.get(key)
        if slice_ is None:
            slice_ = TokenSlice(key=key, label=label_of(row, key))
            slices[key] = slice_
        slice_.completion_tokens += int(row["completion"] or 0)
        slice_.prompt_tokens += int(row["prompt"] or 0)
        slice_.requests += int(row["requests"] or 0)
        _collect_models(slice_, names)
    return slices


def _collect_log_slices_sql(qs, *, model: str = "") -> dict:
    """Срезы журнала запросов, посчитанные в SQL (GROUP BY).

    Группировка идёт по паре (измерение, набор моделей): суммы считает база,
    а разбор JSON-списка — Python, но по ГРУППАМ (их единицы), а не по
    каждой записи журнала. Python-итерации по всей таблице больше нет —
    на большой таблице это и было главным тормозом страницы.

    Фильтр по модели применяется к группам (по названиям моделей внутри
    комбинации), а не к JSON-тексту: подстрока ищется внутри одного названия
    (семантика эталонной Python-агрегации).
    """
    # Комбинации моделей: суммы и число записей по каждой уникальной
    # комбинации model_names (GROUP BY по JSON-полю).
    combos = list(
        qs.values("model_names").annotate(
            completion=Coalesce(Sum("tokens"), 0),
            prompt=Coalesce(Sum("tokens_prompt"), 0),
            requests=Count("pk"),
        )
    )

    # by_mode — группировка по (режим, набор моделей).
    by_mode = _merge_groups(
        qs.values("mode", "model_names").annotate(
            completion=Coalesce(Sum("tokens"), 0),
            prompt=Coalesce(Sum("tokens_prompt"), 0),
            requests=Count("pk"),
        ),
        key_of=lambda row: row["mode"] or "",
        label_of=lambda row, key: MODE_LABELS.get(key, key or "—"),
        model=model,
    )

    # by_day — группировка по (МСК-день, набор моделей).
    by_day = _merge_groups(
        qs.annotate(day=_log_day_expression())
        .values("day", "model_names").annotate(
            completion=Coalesce(Sum("tokens"), 0),
            prompt=Coalesce(Sum("tokens_prompt"), 0),
            requests=Count("pk"),
        ),
        key_of=lambda row: (
            row["day"].strftime("%Y-%m-%d") if row["day"] is not None else "—"
        ),
        label_of=lambda row, key: key,
        model=model,
    )

    # by_user — группировка по («кто», набор моделей).
    by_user = _merge_groups(
        qs.annotate(who=_log_who_expression())
        .values("who", "model_names").annotate(
            completion=Coalesce(Sum("tokens"), 0),
            prompt=Coalesce(Sum("tokens_prompt"), 0),
            requests=Count("pk"),
        ),
        key_of=lambda row: row["who"] or "—",
        label_of=lambda row, key: key,
        model=model,
    )

    return {
        "by_mode": by_mode,
        "by_day": by_day,
        "by_user": by_user,
        "combos": combos,
    }


def _collect_log_models(combos, *, model: str = "") -> tuple:
    """Срез «по моделям» журнала из сгруппированных комбинаций.

    Возвращает (словарь срезов, флаг incomplete). Сводная запись прогона
    (несколько моделей) идёт ОДНИМ ключом — расход прогона нельзя приписать
    одной из его моделей (см. docstring ``collect_log_slices``).

    ``combos`` — результат GROUP BY по ``model_names`` (суммы и число записей
    по каждой комбинации). Разбор списка имён остаётся в Python, но идёт по
    группам, а не по записям журнала.
    """
    by_model: dict = {}
    incomplete = False
    for row in combos:
        all_names = _names_of(row)
        if not _matches_model(all_names, model):
            continue
        multi_model = len(all_names) > 1
        completion = int(row["completion"] or 0)
        prompt = int(row["prompt"] or 0)
        requests = int(row["requests"] or 0)
        if multi_model:
            model_key = MULTI_MODEL_LABEL if model else ", ".join(all_names)
            m = by_model.setdefault(model_key, TokenSlice(key=model_key, label=model_key))
            m.completion_tokens += completion
            m.prompt_tokens += prompt
            m.requests += requests
            _collect_models(m, all_names)
            if any(is_web_pool_model(n) for n in all_names):
                incomplete = True
            continue
        name = all_names[0]
        m = by_model.setdefault(name, TokenSlice(key=name, label=name))
        m.completion_tokens += completion
        m.prompt_tokens += prompt
        m.requests += requests
        _collect_models(m, [name])
        if is_web_pool_model(name):
            incomplete = True
    return by_model, incomplete


def collect_log_slices(
    qs,
    *,
    date_from=None,
    date_to=None,
    mode: str = "",
    model: str = "",
    user: str = "",
    source: str = "",
) -> dict:
    """Агрегация по журналу запросов.

    Возвращает ``{"by_model", "by_user", "by_mode", "by_day", "total", ...}`` —
    каждый срез — список словарей (см. ``TokenSlice.as_dict``).

    Все срезы считаются в SQL (GROUP BY): по режиму, по МСК-дню, по
    пользователю и по комбинации моделей. Python-итерации по каждой записи
    журнала больше нет — на большой таблице это и было главным тормозом
    страницы. Разбор JSON-списка моделей остался в Python, но идёт по
    сгруппированным комбинациям (их единицы, не тысячи записей).
    """
    qs = _filter_logs(qs, date_from=date_from, date_to=date_to, mode=mode, model=model, user=user, source=source)
    sql_parts = _collect_log_slices_sql(qs, model=model)
    by_model, models_incomplete = _collect_log_models(
        sql_parts["combos"], model=model,
    )

    # total — сумма по тем же группам, что и остальные срезы (поэтому
    # отфильтрованные по модели строки в неё не попадают).
    total = TokenSlice(key="total", label="Всего")
    for row in sql_parts["combos"]:
        names = _names_of(row)
        if not _matches_model(names, model):
            continue
        total.completion_tokens += int(row["completion"] or 0)
        total.prompt_tokens += int(row["prompt"] or 0)
        total.requests += int(row["requests"] or 0)
        _collect_models(total, names)

    return {
        "total": total.as_dict(),
        "by_model": _sort_slices(by_model.values()),
        "by_user": _sort_slices(sql_parts["by_user"].values()),
        "by_mode": _sort_slices(sql_parts["by_mode"].values()),
        "by_day": _sort_slices(sql_parts["by_day"].values()),
        "incomplete": models_incomplete,
    }


def _collect_log_slices_legacy(
    qs,
    *,
    date_from=None,
    date_to=None,
    mode: str = "",
    model: str = "",
    user: str = "",
    source: str = "",
) -> dict:
    """Эталонная Python-агрегация журнала (для сверки с SQL-вариантом).

    Используется только тестами-паритетами: та же логика, что была до
    перевода срезов в SQL. Держим её рядом — при расхождении цифр понятно,
    где именно разошлись (SQL-группировка vs Python-итерация).
    """
    qs = _filter_logs(qs, date_from=date_from, date_to=date_to, mode=mode, model=model, user=user, source=source)
    rows = qs.values(
        "model_names", "tokens", "tokens_prompt", "mode", "user_full_name",
        "username", "external_user_id", "sent_at",
    )

    by_model: dict = {}
    by_user: dict = {}
    by_mode: dict = {}
    by_day: dict = {}
    total = TokenSlice(key="total", label="Всего")
    incomplete = False

    for row in rows:
        names = row["model_names"] or []
        if isinstance(names, str):
            names = [names]
        if not names:
            names = ["—"]
        # Признак сводной записи считаем ДО фильтра по модели: фильтр
        # сужает список, но расход прогона всё равно общий.
        multi_model = len(names) > 1
        all_names = list(names)
        if model:
            matched = [n for n in names if model.lower() in str(n).lower()]
            if not matched:
                continue
            names = matched
        completion = int(row["tokens"] or 0)
        prompt = int(row["tokens_prompt"] or 0)
        who = (row["user_full_name"] or row["username"] or row["external_user_id"] or "—")
        mode_key = row["mode"] or ""
        sent_at = row["sent_at"]
        if sent_at is not None:
            day_key = timezone.localtime(sent_at, MOSCOW_TZ).strftime("%Y-%m-%d")
        else:
            day_key = "—"

        # Сколько моделей в записи журнала:
        #   1 — обычный запрос чата/«реши задачу»/«найти ошибку»: токены
        #       относятся к этой одной модели;
        #   >1 — СВОДНАЯ запись прогона (batch-решение или ARM-прогон с
        #       несколькими моделями): токены — СУММА по всем моделям
        #       прогона (arm_runner пишет их в журнал одной записью).
        # Раскладывать такую сумму на каждое имя нельзя: расход завышался
        # в N раз, и каждой модели приписывался весь расход прогона.
        # Поэтому сводная запись идёт в срез «по моделям» ОДНИМ ключом:
        #   без фильтра по модели — набор моделей прогона («A, B, C»);
        #   с фильтром — общая корзина MULTI_MODEL_LABEL (расход прогона
        #     нельзя отнести к одной модели — а приписать его ей значит
        #     завысить её счётчик в N раз).
        # Разбивка по моделям для прогонов есть в источнике 2 — парах
        # (AIModelTestResult, токены на уровне пары): раздел «Пакетные
        # пары: по моделям» даёт честную картину по моделям.
        if multi_model:
            _add(total, completion, prompt, "")
            _collect_models(total, all_names)
            u = by_user.setdefault(who, TokenSlice(key=who, label=who))
            _add(u, completion, prompt, "")
            _collect_models(u, all_names)
            md = by_mode.setdefault(mode_key, TokenSlice(key=mode_key, label=MODE_LABELS.get(mode_key, mode_key or "—")))
            _add(md, completion, prompt, "")
            _collect_models(md, all_names)
            d = by_day.setdefault(day_key, TokenSlice(key=day_key, label=day_key))
            _add(d, completion, prompt, "")
            _collect_models(d, all_names)
            if model:
                model_key = MULTI_MODEL_LABEL
            else:
                model_key = ", ".join(str(n) for n in all_names)
            m = by_model.setdefault(model_key, TokenSlice(key=model_key, label=model_key))
            _add(m, completion, prompt, "")
            _collect_models(m, all_names)
            if any(is_web_pool_model(str(n)) for n in all_names):
                incomplete = True
            continue

        name = str(names[0])
        _add(total, completion, prompt, name)
        m = by_model.setdefault(name, TokenSlice(key=name, label=name))
        _add(m, completion, prompt, name)
        u = by_user.setdefault(who, TokenSlice(key=who, label=who))
        _add(u, completion, prompt, name)
        md = by_mode.setdefault(mode_key, TokenSlice(key=mode_key, label=MODE_LABELS.get(mode_key, mode_key or "—")))
        _add(md, completion, prompt, name)
        d = by_day.setdefault(day_key, TokenSlice(key=day_key, label=day_key))
        _add(d, completion, prompt, name)
        if is_web_pool_model(name):
            incomplete = True

    return {
        "total": total.as_dict(),
        "by_model": _sort_slices(by_model.values()),
        "by_user": _sort_slices(by_user.values()),
        "by_mode": _sort_slices(by_mode.values()),
        "by_day": _sort_slices(by_day.values()),
        "incomplete": incomplete,
    }


def _collect_batch_slices_sql(test_runs) -> dict:
    """Срезы пакетных пар, посчитанные в SQL (GROUP BY).

    Здесь модель — обычная колонка (``model_title``/``model_key``), а не
    JSON-список, поэтому обе группировки (по модели и по теме) считает база.
    """
    from ..models import AIModelTestResult

    qs = AIModelTestResult.objects.filter(run__in=test_runs)
    name_expr = Coalesce(
        NullIf("model_title", Value("")),
        NullIf("model_key", Value("")),
        Value("—"),
        output_field=CharField(),
    )
    topic_expr = Coalesce(
        NullIf("topic_name_snapshot", Value("")),
        Value("Без темы"),
        output_field=CharField(),
    )

    total_row = qs.aggregate(
        completion=Coalesce(Sum("tokens"), 0),
        prompt=Coalesce(Sum("tokens_prompt"), 0),
        pairs=Count("pk"),
    )

    by_model = {}
    for row in qs.annotate(name=name_expr).values("name").annotate(
        completion=Coalesce(Sum("tokens"), 0),
        prompt=Coalesce(Sum("tokens_prompt"), 0),
        requests=Count("pk"),
    ):
        name = row["name"] or "—"
        slice_ = TokenSlice(key=name, label=name)
        slice_.completion_tokens = int(row["completion"] or 0)
        slice_.prompt_tokens = int(row["prompt"] or 0)
        slice_.requests = int(row["requests"] or 0)
        _collect_models(slice_, [name])
        if is_web_pool_model(name):
            slice_.incomplete = True
        by_model[name] = slice_

    by_topic = {}
    for row in qs.annotate(topic=topic_expr).values("topic").annotate(
        completion=Coalesce(Sum("tokens"), 0),
        prompt=Coalesce(Sum("tokens_prompt"), 0),
        requests=Count("pk"),
    ):
        topic = row["topic"] or "Без темы"
        slice_ = TokenSlice(key=topic, label=topic)
        slice_.completion_tokens = int(row["completion"] or 0)
        slice_.prompt_tokens = int(row["prompt"] or 0)
        slice_.requests = int(row["requests"] or 0)
        by_topic[topic] = slice_

    # Набор моделей среза «по темам»: тема может встречаться у разных
    # моделей — собираем их по (тема, модель) группам.
    topic_models: dict = {}
    for row in qs.annotate(topic=topic_expr, name=name_expr).values("topic", "name"):
        topic_models.setdefault(row["topic"] or "Без темы", set()).add(row["name"] or "—")
    for topic, names in topic_models.items():
        slice_ = by_topic.get(topic)
        if slice_ is None:
            continue
        for name in names:
            _collect_models(slice_, [name])

    # Общая сумма: тот же набор моделей, что и в срезе «по моделям».
    total_models = set()
    for name in by_model:
        total_models.add(name)

    return {
        "total": total_row,
        "by_model": by_model,
        "by_topic": by_topic,
        "total_models": total_models,
    }


def _collect_batch_slices_legacy(test_runs, *, model: str = "") -> dict:
    """Эталонная Python-агрегация пакетных пар (для сверки с SQL-вариантом)."""
    from ..models import AIModelTestResult

    rows = AIModelTestResult.objects.filter(run__in=test_runs).values(
        "model_title", "model_key", "tokens", "tokens_prompt", "verdict",
        "topic_name_snapshot", "failure_reason",
    )

    by_model: dict = {}
    by_topic: dict = {}
    total = TokenSlice(key="total", label="Всего")
    incomplete = False
    pairs = 0

    for row in rows:
        name = row["model_title"] or row["model_key"] or "—"
        if model and model.lower() not in str(name).lower():
            continue
        completion = int(row["tokens"] or 0)
        prompt = int(row["tokens_prompt"] or 0)
        pairs += 1
        _add(total, completion, prompt, name)
        m = by_model.setdefault(name, TokenSlice(key=name, label=name))
        _add(m, completion, prompt, name)
        topic = row["topic_name_snapshot"] or "Без темы"
        t = by_topic.setdefault(topic, TokenSlice(key=topic, label=topic))
        _add(t, completion, prompt, name)
        if is_web_pool_model(name):
            incomplete = True

    return {
        "total": total.as_dict(),
        "by_model": _sort_slices(by_model.values()),
        "by_topic": _sort_slices(by_topic.values()),
        "pairs": pairs,
        "incomplete": incomplete,
    }


def collect_batch_slices(test_runs, *, date_from=None, date_to=None, model: str = "") -> dict:
    """Агрегация по парам пакетного прогона (AIModelTestResult).

    ``test_runs`` — queryset прогонов (AIModelTestRun). Фильтры: период по
    времени старта прогона, модель (по ``model_title``/``model_key``).

    Без фильтра по модели обе группировки считаются в SQL (GROUP BY) — на
    большом прогоне Python-итерация по всем парам была тормозом. С фильтром
    по модели (case-insensitive подстрока) остаётся Python-путь: индексов под
    такой фильтр нет.
    """
    from ..models import AIModelTestResult

    if date_from:
        day_start, _ = msk_day_bounds(date_from)
        test_runs = test_runs.filter(started_at__gte=day_start)
    if date_to:
        _, day_end = msk_day_bounds(date_to)
        test_runs = test_runs.filter(started_at__lte=day_end)

    if model:
        return _collect_batch_slices_legacy(test_runs, model=model)

    sql_parts = _collect_batch_slices_sql(test_runs)
    total = TokenSlice(key="total", label="Всего")
    total.completion_tokens = int(sql_parts["total"]["completion"] or 0)
    total.prompt_tokens = int(sql_parts["total"]["prompt"] or 0)
    total.requests = int(sql_parts["total"]["pairs"] or 0)
    for name in sql_parts["total_models"]:
        _collect_models(total, [name])

    by_model = sql_parts["by_model"]
    by_topic = sql_parts["by_topic"]
    incomplete = any(
        is_web_pool_model(name) for name in by_model
    )

    return {
        "total": total.as_dict(),
        "by_model": _sort_slices(by_model.values()),
        "by_topic": _sort_slices(by_topic.values()),
        "pairs": int(sql_parts["total"]["pairs"] or 0),
        "incomplete": incomplete,
    }


def token_usage_report(
    *,
    date_from=None,
    date_to=None,
    mode: str = "",
    model: str = "",
    user: str = "",
    source: str = "",
) -> dict:
    """Сводный отчёт по токенам: журнал + пакетные пары.

    Единая точка входа для страницы «Контроль токенов» (view) — цифры
    считаются здесь, view только собирает фильтры и рендерит.
    """
    from ..models import AIRequestLog, AIModelTestRun

    log_qs = AIRequestLog.objects.all()
    logs = collect_log_slices(
        log_qs, date_from=date_from, date_to=date_to, mode=mode, model=model,
        user=user, source=source,
    )
    batch = collect_batch_slices(
        # Пакетное решение и отладка промтов: в обоих раннер тратит токены
        # моделей на пары «задача × модель» (у отладки — ещё на редактора).
        AIModelTestRun.objects.filter(
            run_type__in=(
                AIModelTestRun.RUN_TYPE_BATCH,
                AIModelTestRun.RUN_TYPE_PROMPT_DEBUG,
            )
        ),
        date_from=date_from, date_to=date_to, model=model,
    )
    return {
        "logs": logs,
        "batch": batch,
        # Общая сумма: журнал (включая сводные batch-записи) + подробные
        # пакетные пары. Это НЕ двойной счёт: в журнале batch-запись — это
        # одна сводка прогона, а в парах — разбивка того же прогона по
        # задачам. Поэтому итог на странице — «журнал» + «пары» как два
        # РАЗДЕЛА, а не складываются между собой.
        "incomplete": bool(logs["incomplete"] or batch["incomplete"]),
        "filters": {
            "date_from": date_from.strftime("%Y-%m-%d") if date_from else "",
            "date_to": date_to.strftime("%Y-%m-%d") if date_to else "",
            "mode": mode,
            "model": model,
            "user": user,
            "source": source,
        },
    }
