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

    # Модели для селекта — из реестра (плюс фактические названия из отчёта:
    # в журнале модель записывается TITLE'ом, а не ключом).
    registry_models = sorted(
        title for title in registry.title_to_key().keys() if title
    )
    seen_models = {row["label"] for row in report["logs"]["by_model"]}
    seen_models |= {row["label"] for row in report["batch"]["by_model"]}
    model_choices = sorted(seen_models | set(registry_models))

    context = {
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
