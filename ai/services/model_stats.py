"""Накопленная статистика моделей по скоупу (модель, язык, тема): время, % решённых.

Источник данных — пакетные прогоны /arm/solve/, запущенные суперюзером с
включённым чекбоксом «заносить результаты в БД» (см. ``arm_runner``), и
финальные DL-вердикты кэша решённых задач (``solution_cache.record_result``).
Semantics агрегации совпадают с ``arm_runner._per_bucket``: в счётчики
решённости идут только пары с вердиктом solved/failed, длительности —
только не-skipped измерения.

Скоупы (2026-10): пара инкрементирует ОБЕ строки — свою тему
(model_key, язык, тема) и ВСЕГДА языковую (model_key, язык, тема NULL); темы
нет → только языковая; языка нет → пара не пишется (не пачкает скоупы).
Строки (NULL, NULL) — легаси-глобальный скоуп: новые приращения в него не
пишутся, его читает только model_health.get_available_model_options (сортировка
селектора моделей для старых кук); API при неуказанном языке вместо него
отдаёт АГРЕГАТ по всем scoped-строкам модели (общие данные по всем темам
и языкам).

Потребители — ``model_health.get_available_model_options`` (легаси-строки
только для сортировки селектора моделей) и API ``/ai/api/model-stats/``
(скоуп язык/тема для «Реши задачу»/«В чём ошибка»).
"""

from django.db.models import F, Sum

from ..models import AIModelStats, Topic

# Вердикты, попадающие в счётчик решённости (пропущенные пары не считаются —
# как в ``_per_bucket``).
_VERDICT_SOLVED = "solved"
_VERDICT_FAILED = "failed"


def record_batch_solve_stats(results):
    """Инкрементирует ``AIModelStats`` по результатам завершённого прогона.

    ``results`` — result-item'ы пар (ключи ``model_key``, ``model_title``,
    ``verdict``, ``duration`` + скоуп: ``programming_language_id`` — язык
    прогона (в batch-прогоне язык один, см. ``fallback_language_id``),
    ``topic_id`` — тема задачи). Каждая пара инкрементирует ОБЕ скоуп-строки —
    строку своей темы (если тема известна и того же языка: темы пер-языковые,
    чужая тема хуже отсутствующей) и ВСЕГДА языковую (тема NULL). Пара без
    языка не пишется — иначе она пачкала бы легаси-глобальный скоуп
    (NULL/NULL), который больше не читается.

    Race-safe upsert (тот же, что и раньше): инкремент через ``F``-выражения,
    при отсутствии строки — ``create``. Никогда не читает-модифицирует-пишет
    (конкурентные прогоны).
    """
    if not results:
        return

    # (model_key, lang_id, topic_id|None) → bucket. Тема при известном языке
    # валидируется одной выборкой ниже (чужой язык → остаётся только языковая
    # строка), языковая (None) валидна всегда.
    per_scope = {}
    for r in results:
        key = r.get("model_key") or ""
        lang_id = r.get("programming_language_id") or None
        if not key or not lang_id:
            continue
        topic_id = r.get("topic_id") or None
        scope_keys = [(key, lang_id, None)]
        if topic_id is not None:
            scope_keys.append((key, lang_id, topic_id))
        for scope_key in scope_keys:
            bucket = per_scope.setdefault(
                scope_key,
                {"title": r.get("model_title") or key, "solved": 0, "total": 0, "dur_n": 0, "dur_sum": 0.0},
            )
            verdict = r.get("verdict") or ""
            if verdict in (_VERDICT_SOLVED, _VERDICT_FAILED):
                bucket["total"] += 1
                if verdict == _VERDICT_SOLVED:
                    bucket["solved"] += 1
            duration = r.get("duration")
            if duration is not None and verdict != "skipped":
                bucket["dur_n"] += 1
                bucket["dur_sum"] += float(duration or 0.0)

    # Валидация тем одной выборкой: тема пер-языковая, скоуп чужого языка не
    # пишем (легаси-поведение ensure_task: чужая тема хуже отсутствующей).
    topic_ids = {sk[2] for sk in per_scope if sk[2] is not None}
    valid_lang_topic = set()
    if topic_ids:
        valid_lang_topic = {
            (lang, pk)
            for pk, lang in Topic.objects.filter(pk__in=topic_ids).values_list("pk", "programming_language_id")
        }

    for (key, lang_id, topic_id), bucket in per_scope.items():
        if topic_id is not None and (lang_id, topic_id) not in valid_lang_topic:
            continue
        updated = AIModelStats.objects.filter(
            model_key=key,
            programming_language_id=lang_id,
            topic_id=topic_id,
        ).update(
            model_title=bucket["title"],
            solved_count=F("solved_count") + bucket["solved"],
            total_count=F("total_count") + bucket["total"],
            duration_count=F("duration_count") + bucket["dur_n"],
            duration_sum=F("duration_sum") + bucket["dur_sum"],
        )
        if not updated:
            AIModelStats.objects.create(
                model_key=key,
                model_title=bucket["title"],
                solved_count=bucket["solved"],
                total_count=bucket["total"],
                duration_count=bucket["dur_n"],
                duration_sum=bucket["dur_sum"],
                programming_language_id=lang_id,
                topic_id=topic_id,
            )


def _stats(duration_sum, duration_count, solved_count, total_count):
    """Счётчики строки/агрегата в форму суффиксов. None — счётчик пуст
    (показываем «ничего», а не ноль)."""
    return {
        "avg_seconds": round(duration_sum / duration_count, 1) if duration_count else None,
        "percent_solved": (
            round(solved_count * 100.0 / total_count, 1) if total_count else None
        ),
    }


def get_legacy_stats_map(model_keys):
    """Данные легаси-глобального скоупа (NULL, NULL). Читает ТОЛЬКО
    model_health.get_available_model_options — сортировка селектора моделей
    чата: скоуп заморожен (новые приращения пишутся в язык/тему), а данные
    нужны сортировке как историческая база. Клиентский API этот скоуп не
    читает — общие данные без скоупа это агрегат scoped-строк
    (get_model_stats_map с language_id=None).
    """
    keys = [k for k in model_keys if k]
    if not keys:
        return {}
    stats_map = {}
    qs = AIModelStats.objects.filter(
        model_key__in=keys, programming_language__isnull=True, topic__isnull=True,
    )
    for row in qs:
        stats_map[row.model_key] = _stats(
            row.duration_sum, row.duration_count, row.solved_count, row.total_count,
        )
    return stats_map


def get_model_stats_map(model_keys, language_id=None, topic_id=None):
    """``{model_key: {"avg_seconds": float|None, "percent_solved": float|None}}``.

    Читается РОВНО один скоуп — строки разных скоупов нельзя смешивать в ключе
    ``model_key``: язык+тема → точная тема-строка; язык без темы → языковые
    строки (тема NULL, «общие данные по всем темам данного языка»); оба None
    → ОБЩИЕ данные по всем темам и всем языкам — scoped-строки модели
    СУММИРУЮТСЯ (взвешенно по счётчикам: время по duration_sum/счёту,
    точность по solved/total). Легаси-глобальные строки (NULL, NULL) при
    агрегации не участвуют. Модели без накопленной статистики в словаре
    отсутствуют.
    """
    keys = [k for k in model_keys if k]
    if not keys:
        return {}

    if language_id is None:
        # Оба None → общие данные по ВСЕМ темам и языкам: суммируем
        # scoped-строки модели (SQL-агрегат; легаси-строки (NULL, NULL)
        # отфильтрованы — их приращения уже давно не пишутся).
        grouped = (
            AIModelStats.objects
            .filter(model_key__in=keys, programming_language__isnull=False)
            .values("model_key")
            .annotate(
                d_sum=Sum("duration_sum"),
                d_cnt=Sum("duration_count"),
                s_cnt=Sum("solved_count"),
                t_cnt=Sum("total_count"),
            )
        )
        return {
            row["model_key"]: _stats(
                row["d_sum"] or 0, row["d_cnt"] or 0, row["s_cnt"] or 0, row["t_cnt"] or 0,
            )
            for row in grouped
        }

    qs = AIModelStats.objects.filter(
        model_key__in=keys, programming_language_id=language_id,
    )
    if topic_id is not None:
        qs = qs.filter(topic_id=topic_id)
    else:
        qs = qs.filter(topic__isnull=True)

    stats_map = {}
    for row in qs:
        stats_map[row.model_key] = _stats(
            row.duration_sum, row.duration_count, row.solved_count, row.total_count,
        )
    return stats_map