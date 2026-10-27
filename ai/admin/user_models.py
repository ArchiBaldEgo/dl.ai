"""Страница «Мои AI-модели»: личные модели пользователя (админский вход).

Пользователь приносит СВОЮ модель: провайдер + ID модели + свой ключ +
(для совместимых) базовый URL. Модель работает в чате, «Реши задачу» и
«Найти ошибку» — везде, где есть выбор модели (тот же путь вызова, что у
глобальных моделей).

ВАЖНО ДЛЯ ПОЛЬЗОВАТЕЛЯ (текст страницы): модель должна работать БЕЗ VPN —
то есть endpoint провайдера должен быть доступен из сети университета без
обхода ограничений.

Доступность: по умолчанию только владельцу.
* список «Мои модели» показывает только его записи (фильтр по ``user``);
* правка/удаление — только своей записи (владелец проверяется по pk);
* владелец может открыть модель адресно (``shared_with``) или всем
  (``is_public``) — но только на пользовательских страницах, не в ARM;
* суперюзер видит таблицу «Все модели» и меняет ТОЛЬКО доступ
  (публичность/список получателей) — чужие URL/ключи/настройки не читает
  и не меняет (ai/services/user_models.py::set_user_model_access).

Безопасность ввода (владелец отдельно потребовал «защиту от атак»):
* ``base_url`` — только https, без приватных/локальных/линковых адресов,
  без credentials в URL, порт из белого списка (SSRF-защита — в
  ai/services/user_models.py::validate_base_url);
* ключ API хранится шифром Fernet и в открытом виде не показывается;
* длины полей ограничены (см. константы сервиса).

Вся операционная логика — в СЕРВИСЕ (ai/services/user_models.py): страница
существует в двух местах (здесь, в админке, и на клиентской стороне —
``/ai/my-models/``, ai/views.py::my_models_view), поэтому вьюхи только
разрешают доступ и отдают шаблон.
"""

import json

from django.http import HttpResponseForbidden, JsonResponse
from django.template.response import TemplateResponse
from django.views.decorators.http import require_GET, require_POST

from ..i18n import user_models_strings, user_models_strings_all
from ..models import AIUserModel
from ..services.user_directory import search_users_for_sharing
from ..services.user_models import (
    PROVIDER_BASE_URLS,
    admin_user_model_rows,
    save_user_model,
    delete_user_model,
    set_user_model_access,
    user_model_rows,
)
from .permissions import can_access_user_models, is_superuser_user
from .site import ai_admin_site

#: URL страницы — из клиентского шаблона тоже (там админский путь).
USER_MODELS_URL = "/ai/admin/user-models/"


def _forbid(message="Access denied", status=403):
    return JsonResponse({"ok": False, "message": message}, status=status)


def admin_user_models_view(request):
    """Страница «Мои AI-модели»: список + форма добавления."""
    if not can_access_user_models(request):
        return HttpResponseForbidden("Access denied")
    if not request.user.is_authenticated:
        return HttpResponseForbidden("Access denied")

    # each_context — обязателен: без него admin/base.html не рендерит
    # ЛЕВЫЙ БАР (is_nav_sidebar_enabled приходит только отсюда) и шапку.
    context = {
        **ai_admin_site.each_context(request),
        "title": "Мои AI-модели",
        "user_models": user_model_rows(request.user),
        # Таблица «Все модели» — только реальному суперюзеру (режим
        # «Посмотреть как…» её не показывает: эмуляция честная).
        "all_models": admin_user_model_rows() if is_superuser_user(request.user) else None,
        "provider_choices": AIUserModel.PROVIDER_CHOICES,
        "provider_base_urls": PROVIDER_BASE_URLS,
        "can_edit": True,
        # Тексты панели — по умолчанию русские (админка не переводится).
        "t": user_models_strings("Русский"),
        "um_i18n": user_models_strings_all(),
    }
    return TemplateResponse(request, "admin/ai/user_models.html", context)


@require_POST
def admin_user_model_create_view(request):
    """Создать личную модель (или обновить существующую — pk в теле).

    Тело — JSON: ``{id?, title, provider, model_id, base_url, api_key,
    max_tokens, supports_vision, supports_think, is_active}``.

    Владелец: ``id`` принадлежит текущему пользователю — чужую запись
    обновить нельзя (иначе «не найдена» — записи для него не существует).
    """
    if not can_access_user_models(request) or not request.user.is_authenticated:
        return _forbid()
    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return _forbid("Некорректный запрос", status=400)
    if not isinstance(payload, dict):
        return _forbid("Некорректный запрос", status=400)

    ok, message, model_pk = save_user_model(request.user, payload)
    if not ok:
        # «Не найдена» — 404 (существование чужих записей не подтверждаем),
        # остальное — 400 (плохие данные).
        status = 404 if message == "Модель не найдена" else 400
        return _forbid(message, status=status)
    return JsonResponse({"ok": True, "id": model_pk})


@require_POST
def admin_user_model_delete_view(request):
    """Удалить личную модель (только свою)."""
    if not can_access_user_models(request) or not request.user.is_authenticated:
        return _forbid()
    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return _forbid("Некорректный запрос", status=400)
    ok, message = delete_user_model(request.user, payload.get("id"))
    if not ok:
        status = 404 if message == "Модель не найдена" else 400
        return _forbid(message, status=status)
    return JsonResponse({"ok": True})


@require_POST
def admin_user_model_access_view(request):
    """Изменить доступ к модели (владелец или суперюзер): только доступ.

    Тело — ``{id, is_public, shared_with: [id…]}``. Конфигурационные поля
    (URL, ключ, провайдер, модель, лимиты) этой операцией не меняются.
    """
    if not can_access_user_models(request) or not request.user.is_authenticated:
        return _forbid()
    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return _forbid("Некорректный запрос", status=400)
    if not isinstance(payload, dict):
        return _forbid("Некорректный запрос", status=400)
    ok, message = set_user_model_access(
        request.user, payload.get("id"), payload.get("is_public"),
        payload.get("shared_with"),
    )
    if not ok:
        status = 404 if message == "Модель не найдена" else 400
        return _forbid(message, status=status)
    return JsonResponse({"ok": True})


@require_GET
def admin_user_model_search_users_view(request):
    """Поиск получателей доступа (админка): DL-id точно или ФИО/логин."""
    if not can_access_user_models(request) or not request.user.is_authenticated:
        return _forbid()
    users = search_users_for_sharing(request.GET.get("q", ""), request.user)
    return JsonResponse({"ok": True, "users": users})
