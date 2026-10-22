"""Admin view для страницы «Контроль токенов».

Показывает, сколько токенов потрачено: какие модели, кто, в каком режиме.
Источники — журнал запросов (AIRequestLog) и пары пакетного решения
(AIModelTestResult); агрегация живёт в ai/services/token_usage.py (единственный
владелец), view только собирает фильтры и передаёт их сервису.

Учёт считается как «промпт + ответ». Web-пулы (Web_DeepSeek / Web_Kimi)
токены не отдают — такие строки честно помечаются «учёт недоступен», а
общая сумма по ним считается неполной (флаг ``incomplete``).
"""

from django.http import HttpResponseForbidden
from django.shortcuts import render

from ..model_clients import registry
from ..services.token_usage import MODE_LABELS, parse_date, token_usage_report
from .permissions import can_access_token_usage
from .site import ai_admin_site


def _annotate_shares(rows: list) -> None:
    """Добавить строкам таблицы «share» — процент от максимума этого среза.

    Числа в колонке «Всего» уже отсортированы по убыванию, но пропорция
    считывается глазами медленнее, чем длиной полоски: share рисует
    долю строки от наибольшей строки ЭТОЙ таблицы (не от общего итога) —
    каждая таблица имеет свой максимум. Строки без расхода полоску
    не получают (share отсутствует).
    """
    max_total = max((int(row.get("total_tokens") or 0) for row in rows), default=0)
    if max_total <= 0:
        return
    for row in rows:
        share = int(round(float(row["total_tokens"]) / max_total * 100))
        if share > 0:
            row["share"] = share


def _annotate_averages(report: dict) -> None:
    """Средний расход на запрос (журнал) и на пару (пакетные прогонки).

    Понятность: абсолютная сумма мало о чём говорит без масштаба —
    «120 000 токенов за 400 запросов» читается легче со средней строкой
    в подзаголовке карточки. Целое число (округление вниз/банковское
    не важно — это подсказка, не бухгалтерия).
    """
    logs_total = report["logs"]["total"]
    requests = int(logs_total.get("requests") or 0)
    if requests > 0:
        report["logs"]["avg_per_request"] = int(logs_total["total_tokens"]) // requests
    batch_total = report["batch"]["total"]
    pairs = int(report["batch"].get("pairs") or 0)
    if pairs > 0:
        report["batch"]["avg_per_pair"] = int(batch_total["total_tokens"]) // pairs


@ai_admin_site.admin_view
def admin_token_usage_view(request):
    """Страница «Контроль токенов»: расход по моделям / пользователям / режимам."""
    if not can_access_token_usage(request):
        return HttpResponseForbidden("Access denied")

    date_from = parse_date(request.GET.get("date_from", ""))
    date_to = parse_date(request.GET.get("date_to", ""))
    mode = (request.GET.get("mode") or "").strip()
    model = (request.GET.get("model") or "").strip()
    user = (request.GET.get("user") or "").strip()
    source = (request.GET.get("source") or "").strip()

    report = token_usage_report(
        date_from=date_from,
        date_to=date_to,
        mode=mode,
        model=model,
        user=user,
        source=source,
    )

    # Оформление (не агрегация): полоски долей в таблицах + средние
    # в карточках-итогах. Мутируем свежепостроенный отчёт — он живёт
    # только в этом запросе.
    for section, slice_keys in (
        (report["logs"], ("by_model", "by_user", "by_mode", "by_day")),
        (report["batch"], ("by_model", "by_topic")),
    ):
        for key in slice_keys:
            _annotate_shares(section[key])
    _annotate_averages(report)

    # XHR-ветка: при смене фильтров клиент просит ТОЛЬКО панели (карточки +
    # срезы) — без формы, шапки и левого бара. Фильтры приезжают в URL, но
    # наборы моделей/режимов/источников остаются от прошлой загрузки: они
    # нужны форме, а её при XHR-запросе нет.
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return render(
            request,
            "admin/ai/_tokens_panels.html",
            {
                "logs": report["logs"],
                "batch": report["batch"],
                "incomplete": report["incomplete"],
            },
        )

    # Модели для селекта — из реестра (плюс фактические названия из отчёта:
    # в журнале модель записывается TITLE'ом, а не ключом).
    registry_models = sorted(
        title for title in registry.title_to_key().keys() if title
    )
    seen_models = {row["label"] for row in report["logs"]["by_model"]}
    seen_models |= {row["label"] for row in report["batch"]["by_model"]}
    model_choices = sorted(seen_models | set(registry_models))

    # each_context — обязателен: без него admin/base.html не рендерит
    # ЛЕВЫЙ БАР (is_nav_sidebar_enabled приходит только отсюда) и шапку.
    context = {
        **ai_admin_site.each_context(request),
        "title": "Контроль токенов",
        "report": report,
        "logs": report["logs"],
        "batch": report["batch"],
        "model_choices": model_choices,
        "mode_choices": [(key, label) for key, label in MODE_LABELS.items()],
        "source_choices": [
            ("websocket", "WebSocket"),
            ("http", "HTTP"),
            ("arm", "ARM"),
        ],
        "filters": report["filters"],
        "filters_date_from": report["filters"]["date_from"],
        "filters_date_to": report["filters"]["date_to"],
        "filters_mode": mode,
        "filters_model": model,
        "filters_user": user,
        "filters_source": source,
        "incomplete": report["incomplete"],
    }
    return render(request, "admin/ai/tokens.html", context)
