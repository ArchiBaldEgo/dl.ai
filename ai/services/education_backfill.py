"""Дозагрузка названий вуза/школы и группы/класса — в фоне.

Названия живут в ``ExternalDLAccount.education_school_name`` /
``education_group_name`` (DL отдаёт их в блоке EducationInfo начиная с
REST API 1.4.2). У студентов, залогинившихся ДО появления этого блока, поля
пустые — в дневном отчёте и на доске «Таски» вместо названий видны сырые ID.

Дозагрузка идёт ОТДЕЛЬНЫМ daemon-потоком: страница отчёта не должна ждать
DL (иначе рендер plantonно тормозит на десятках HTTP-запросов). Названия
появятся в следующем открытии отчёта.

Массовая дозагрузка всех аккаунтов — отдельной командой:
``python manage.py backfill_education_names``.
"""

import logging
import threading

from django.db import close_old_connections

logger = logging.getLogger(__name__)

# Сколько аккаунтов дозагружаем за один проход (страница не должна
# упираться в DL).
MAX_PER_RUN = 20

# Ключи DL-ответа → поля ExternalDLAccount.
_EDU_FIELDS = (
    ("schoolName", "education_school_name"),
    ("groupName", "education_group_name"),
)


def accounts_missing_names(account_ids):
    """Аккаунты, у которых не хватает названия школы ИЛИ группы."""
    from django.db.models import Q

    from ..models import ExternalDLAccount

    ids = [str(i) for i in dict.fromkeys(account_ids) if i]
    if not ids:
        return []
    return list(
        ExternalDLAccount.objects.filter(external_user_id__in=ids).exclude(
            Q(education_school_name__gt="") & Q(education_group_name__gt="")
        )
    )


def backfill_education_names(account_ids, limit=MAX_PER_RUN):
    """Синхронная дозагрузка названий (команда/тесты). Возвращает число обновлённых.

    Пустые значения DL НЕ затирают уже сохранённые названия.
    """
    from ..dl_api_client import fetch_user_education
    from ..models import ExternalDLAccount

    accounts = accounts_missing_names(account_ids)[:limit]
    updated = 0
    for account in accounts:
        education = fetch_user_education(account.external_user_id)
        changes = {}
        for dl_key, model_field in _EDU_FIELDS:
            value = (education.get(dl_key) or "").strip()
            if value and not (getattr(account, model_field, "") or "").strip():
                changes[model_field] = value
        if changes:
            ExternalDLAccount.objects.filter(pk=account.pk).update(**changes)
            updated += 1
    return updated


def schedule_education_backfill(account_ids, limit=MAX_PER_RUN):
    """Запустить дозагрузку в фоне (daemon-поток). Не блокирует рендер."""
    ids = [str(i) for i in dict.fromkeys(account_ids) if i][:limit]
    if not ids:
        return False

    def _worker():
        try:
            backfill_education_names(ids, limit=limit)
        except Exception:  # noqa: BLE001 — фоновая задача не должна ронять процесс
            logger.exception("education backfill failed")
        finally:
            close_old_connections()

    threading.Thread(target=_worker, name="ai-education-backfill", daemon=True).start()
    return True
