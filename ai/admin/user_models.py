"""Страница «Мои AI-модели»: личные модели пользователя (админский вход).

Пользователь приносит СВОЮ модель: провайдер + ID модели + свой ключ +
(для совместимых) базовый URL. Модель работает в чате, «Реши задачу» и
«Найти ошибку» — везде, где есть выбор модели (тот же путь вызова, что у
глобальных моделей).

ВАЖНО ДЛЯ ПОЛЬЗОВАТЕЛЯ (текст страницы): модель должна работать БЕЗ VPN —
то есть endpoint провайдера должен быть доступен из сети университета без
обхода ограничений.

Доступность — ТОЛЬКО владельцу:
* список показывает только его модели (запрос фильтруется по ``user``);
* правка/удаление — только своей записи (владелец проверяется по pk);
* вызов чужой модели невозможен: ключ ``User_<pk>`` резолвится только
  владельцем (ai/services/user_models.py::find_user_model).

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
from django.views.decorators.http import require_POST

from ..i18n import user_models_strings, user_models_strings_all
from ..models import AIUserModel
from ..services.user_models import (
    PROVIDER_BASE_URLS,
    save_user_model,
    delete_user_model,
    user_model_rows,
)
from .permissions import can_access_user_models

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

    context = {
        "title": "Мои AI-модели",
        "user_models": user_model_rows(request.user),
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
