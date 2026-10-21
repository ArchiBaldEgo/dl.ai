"""Страница «Запросы по DL API»: заявки пользователей на создание API к DL.

Две вкладки:
* «Актуальные» — заявки, по которым API ещё НЕ создан (status=pending);
* «Неактуальные» — API уже создан (status=done): строка переезжает сюда
  автоматически, как только админ отметил «API создано».

В таблице — описание, кто попросил, когда попросил (ТЗ владельца).

Вкладка — серверная (?tab=pending|done), URL шарится. Отметка «API создано»
и комментарий — POST на ту же страницу (JSON), обработчик проверяет права
владельца страницы (page.dl_api_requests) и пишет resolved_at/resolved_by.
"""

import json

from django.http import HttpResponseForbidden, JsonResponse
from django.template.response import TemplateResponse
from django.utils import timezone
from django.views.decorators.http import require_POST

from ..constants import MOSCOW_TZ
from ..models import AIDLApiRequest, ExternalDLAccount
from .permissions import can_access_dl_api_requests
from .site import ai_admin_site

TAB_PENDING = "pending"
TAB_DONE = "done"


def _forbid(message="Access denied", status=403):
    return JsonResponse({"ok": False, "message": message}, status=status)


def _row(item):
    """Строка таблицы: описание / кто попросил / когда попросил."""
    created = item.created_at
    if created is not None:
        created_msk = timezone.localtime(created, MOSCOW_TZ)
        created_label = created_msk.strftime("%d.%m.%Y %H:%M")
    else:
        created_label = "—"
    resolved = item.resolved_at
    if resolved is not None:
        resolved_label = timezone.localtime(resolved, MOSCOW_TZ).strftime("%d.%m.%Y %H:%M")
    else:
        resolved_label = ""
    who = item.user_full_name or item.username or item.external_user_id or "—"
    return {
        "id": item.pk,
        "description": item.description,
        "who": who,
        "user_full_name": item.user_full_name,
        "username": item.username,
        "external_user_id": item.external_user_id,
        "created_label": created_label,
        "created_at": created,
        "status": item.status,
        "comment": item.comment,
        "resolved_label": resolved_label,
        "resolved_by": (
            item.resolved_by.get_full_name() or item.resolved_by.username
            if item.resolved_by_id
            else ""
        ),
    }


def admin_dl_api_requests_view(request):
    """Страница «Запросы по DL API»: вкладки «Актуальные»/«Неактуальные»."""
    if not can_access_dl_api_requests(request):
        return HttpResponseForbidden("Access denied")

    tab = (request.GET.get("tab") or TAB_PENDING).strip()
    if tab not in (TAB_PENDING, TAB_DONE):
        tab = TAB_PENDING

    qs = AIDLApiRequest.objects.select_related("user", "resolved_by")
    if tab == TAB_PENDING:
        qs = qs.filter(status=AIDLApiRequest.STATUS_PENDING)
    else:
        qs = qs.filter(status=AIDLApiRequest.STATUS_DONE)
    rows = [_row(item) for item in qs.order_by("-created_at")]

    counts = {
        "pending": AIDLApiRequest.objects.filter(
            status=AIDLApiRequest.STATUS_PENDING
        ).count(),
        "done": AIDLApiRequest.objects.filter(status=AIDLApiRequest.STATUS_DONE).count(),
    }

    # each_context — обязателен: без него admin/base.html не рендерит
    # ЛЕВЫЙ БАР (is_nav_sidebar_enabled приходит только отсюда) и шапку.
    context = {
        **ai_admin_site.each_context(request),
        "title": "Запросы по DL API",
        "tab": tab,
        "rows": rows,
        "counts": counts,
    }
    return TemplateResponse(request, "admin/ai/dl_api_requests.html", context)


@require_POST
def admin_dl_api_request_create_view(request):
    """Создать заявку на создание API к DL (форма в админке).

    Тело — JSON: ``{description}``. Права — как у страницы
    (page.dl_api_requests): без них 403. Описание обязательно.
    """
    if not can_access_dl_api_requests(request) or not request.user.is_authenticated:
        return _forbid()
    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return _forbid("Некорректный запрос", status=400)
    if not isinstance(payload, dict):
        return _forbid("Некорректный запрос", status=400)
    description = (payload.get("description") or "").strip()
    if not description:
        return _forbid("Описание заявки обязательно", status=400)
    description = description[:5000]

    user = request.user
    full_name = (user.get_full_name() or "").strip()
    external_user_id = ""
    account = ExternalDLAccount.objects.filter(user=user).first()
    if account is not None:
        external_user_id = account.external_user_id or ""
        full_name = full_name or (
            (account.external_first_name or "").strip()
            + " " + (account.external_last_name or "").strip()
        ).strip()
    item = AIDLApiRequest.objects.create(
        description=description,
        user=user,
        username=user.username or "",
        user_full_name=full_name,
        external_user_id=external_user_id,
        status=AIDLApiRequest.STATUS_PENDING,
    )
    return JsonResponse({"ok": True, "id": item.pk, "status": item.status})


@require_POST
def admin_dl_api_request_update_view(request):
    """Отметить заявку как выполненную («API создано») или вернуть в актуальные.

    Тело — JSON: ``{id, status, comment}``. Права — те же, что у страницы
    (page.dl_api_requests): без них 403.
    """
    if not can_access_dl_api_requests(request) or not request.user.is_authenticated:
        return _forbid()
    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return _forbid("Некорректный запрос", status=400)
    try:
        item_pk = int(payload.get("id") or 0)
    except (TypeError, ValueError):
        return _forbid("Некорректный идентификатор", status=400)
    item = AIDLApiRequest.objects.filter(pk=item_pk).first()
    if item is None:
        return _forbid("Запрос не найден", status=404)

    status = (payload.get("status") or "").strip()
    if status not in dict(AIDLApiRequest.STATUS_CHOICES):
        return _forbid("Неизвестный статус", status=400)

    comment = (payload.get("comment") or "").strip()[:2000]
    item.status = status
    item.comment = comment
    if status == AIDLApiRequest.STATUS_DONE:
        item.resolved_at = timezone.now()
        item.resolved_by = request.user
    else:
        item.resolved_at = None
        item.resolved_by = None
    item.save(update_fields=["status", "comment", "resolved_at", "resolved_by"])
    return JsonResponse({"ok": True, "id": item.pk, "status": item.status})
