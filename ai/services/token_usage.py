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

from django.db.models import Q
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


def collect_batch_slices(test_runs, *, date_from=None, date_to=None, model: str = "") -> dict:
    """Агрегация по парам пакетного прогона (AIModelTestResult).

    ``test_runs`` — queryset прогонов (AIModelTestRun). Фильтры: период по
    времени старта прогона, модель (по ``model_title``/``model_key``).
    """
    from ..models import AIModelTestResult

    if date_from:
        day_start, _ = msk_day_bounds(date_from)
        test_runs = test_runs.filter(started_at__gte=day_start)
    if date_to:
        _, day_end = msk_day_bounds(date_to)
        test_runs = test_runs.filter(started_at__lte=day_end)

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
        AIModelTestRun.objects.filter(run_type=AIModelTestRun.RUN_TYPE_BATCH),
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
