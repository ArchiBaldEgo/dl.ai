"""Доска задач «Таски» (ТЗ 2026-10-19, правки того же числа).

Два типа тасков-вкладок: «Фронт работы» (front) — приоритетный поток с
ЧИСЛОВЫМ уникальным номером (меньше = важнее, выдаётся сервером: max+1;
порядок меняют перетаскиванием строк → tasks/reorder/ перенумеровывает
1..N) и «Хотелки» (wish) — без нумерации. Псевдо-вкладка-фильтр «Мои таски»
(mine) показывает только взятые пользователем таски; закрытый таск удалён
с доски, значит всё показанное — работающее. Создать/закрыть — суперюзер
либо ключи action.task_create/task_close (см. «Группы доступа»);
переставить — superuser/action.task_reorder. Взять может любой, кто
допущен в админку (page.tasks — baseline, ТЗ 2026-10-19): taken_by/taken_at
пишутся атомарно (условный UPDATE — «первый успел»). ModelAdmin не
регистрируется — страница целиком кастомная, по тому же образцу, что
«Закреплённые пакетные решения» (pinned.py).
"""

import json

from django.db import IntegrityError, transaction
from django.db.models import F, Max, Q
from django.http import HttpResponseForbidden, JsonResponse
from django.template.response import TemplateResponse
from django.utils import timezone
from django.views.decorators.http import require_POST

from ..constants import MOSCOW_TZ
from ..models import AIDevTask, ExternalDLAccount
from ..services.education_directory import education_title_maps, group_display

from .permissions import (
    can_access_tasks,
    is_prompt_developer_user,
    is_staff_or_superuser,
    is_superuser_user,
    user_has_key,
)
from .site import ai_admin_site

TASKS_URL = "/ai/admin/tasks/"


def _claimant_display(user, edu_maps=None):
    """``"ФИО (группа)"`` взявшего таск разработчика.

    ФИО — общее имя админской шапки (``_user_display_name``, DRY), группа —
    DL-учебная из справочника (education_group_mask_id → DLEducationName,
    рендер-резидент: правка справочника чинит строки задним числом);
    без DL-аккаунта/справочника — ролевая метка («Разработчик промптов» и
    т.п.). Формат совпадает с «Обновлениями» (AuthorAlias.display_label).
    """
    if user is None:
        return ""
    name = ai_admin_site._user_display_name(user)
    if edu_maps is None:
        edu_maps = education_title_maps()
    _, groups, _schools_school = edu_maps
    try:  # reverse OneToOne на отсутствующую запись кидает DoesNotExist
        account = user.external_dl_account
    except ExternalDLAccount.DoesNotExist:
        account = None
    group = group_display(groups, account)
    if group == "—":
        group = ai_admin_site._user_role_label(
            user,
            is_superuser_user(user),
            is_staff_or_superuser(user),
            is_prompt_developer_user(user),
        )
    return f"{name} ({group})"


def _forbid(message="Access denied", status=403):
    return JsonResponse({"ok": False, "message": message}, status=status)


def admin_tasks_view(request):
    """Страница «Таски»: вкладки (front/wish/mine), таблица, форма создания.

    Вкладки — серверные (?kind=), URL шарится; wanted-строки без номера в
    колонке «№» — «—». «Мои таски» (kind=mine) — ПСЕВДО-вкладка-фильтр:
    только таски, взятые пользователем (закрытый таск удалён с доски,
    поэтому всё показанное — активное).
    Перетаскивание включается draggable + data-reorder="1" только у тех,
    кому можно (право), и только на вкладке front.
    """
    if not can_access_tasks(request):
        return HttpResponseForbidden("Access denied")
    superuser = is_superuser_user(request.user)

    VIEW_MINE = "mine"
    kind = request.GET.get("kind") or AIDevTask.Kind.FRONT
    if kind not in dict(AIDevTask.Kind.choices) and kind != VIEW_MINE:
        kind = AIDevTask.Kind.FRONT

    if kind == VIEW_MINE:
        # Псевдо-вкладка «Мои таски»: ТОЛЬКО взятые пользователем. Таск может
        # быть и хоталкой — порядок по номеру фронта, NULL-номера (хотелки)
        # ВСЕГДА в конец: nulls_last явно, иначе sqlite (NULLs first) и
        # Postgres (NULLs last) расходятся.
        rows_qs = AIDevTask.objects.select_related(
            "taken_by", "taken_by__external_dl_account"
        ).filter(
            taken_by=request.user
        ).order_by(F("priority_no").asc(nulls_last=True), "-created_at", "-id")
    else:
        rows_qs = AIDevTask.objects.select_related(
            "taken_by", "taken_by__external_dl_account"
        ).filter(kind=kind)
        if kind == AIDevTask.Kind.FRONT:
            rows_qs = rows_qs.order_by("priority_no", "-created_at", "-id")
        else:
            rows_qs = rows_qs.order_by("-created_at", "-id")

    edu_maps = education_title_maps()
    rows = []
    for task in rows_qs:
        rows.append({
            "id": task.pk,
            "kind": task.kind,
            "priority_no": task.priority_no,
            "title": task.title,
            "description": task.description,
            "estimate": task.estimate,
            "created_at": task.created_at,
            "taken": task.taken_by_id is not None,
            "claimant": _claimant_display(task.taken_by, edu_maps),
            "taken_at": task.taken_at,
            "claim_url": f"{TASKS_URL}{task.pk}/claim/",
            "close_url": f"{TASKS_URL}{task.pk}/close/",
        })

    can_reorder = (
        (superuser or user_has_key(request.user, "action.task_reorder"))
        and kind == AIDevTask.Kind.FRONT
    )
    context = {
        **ai_admin_site.each_context(request),
        "title": "Таски",
        "rows": rows,
        "active_kind": kind,
        "tabs": [
            {"kind": value, "label": label, "url": f"{TASKS_URL}?kind={value}"}
            for value, label in AIDevTask.Kind.choices
        ] + [{"kind": VIEW_MINE, "label": "Мои таски", "url": f"{TASKS_URL}?kind={VIEW_MINE}"}],
        "create_kinds": [
            {"kind": value, "label": label, "selected": value == (kind if kind in dict(AIDevTask.Kind.choices) else AIDevTask.Kind.FRONT)}
            for value, label in AIDevTask.Kind.choices
        ],
        "can_reorder": can_reorder,
        "can_create": (
            (superuser or user_has_key(request.user, "action.task_create"))
        ),
        "can_close": (
            (superuser or user_has_key(request.user, "action.task_close"))
        ),
        "moscow_tz": MOSCOW_TZ,
    }
    return TemplateResponse(request, "admin/ai/tasks.html", context)


@require_POST
def admin_task_create_view(request):
    """Создание таска: суперюзер или action.task_create.

    Номер приоритета присваивает СЕРВЕР (max+1 по фронту, атомарно) — клиент
    его прислать не может; у «Хотелок» номера нет вообще.
    """
    if not request.user.is_authenticated:
        return _forbid()
    if not (is_superuser_user(request.user) or user_has_key(request.user, "action.task_create")):
        return _forbid("Создавать таски может только суперпользователь")
    title = (request.POST.get("title") or "").strip()
    if not title:
        return JsonResponse(
            {"ok": False, "message": "Название таска обязательно"}, status=400
        )
    kind = (request.POST.get("kind") or AIDevTask.Kind.FRONT).strip()
    if kind not in dict(AIDevTask.Kind.choices):
        return JsonResponse({"ok": False, "message": "Неизвестный тип таска"}, status=400)

    kwargs = {
        "title": title,
        "description": (request.POST.get("description") or "").strip(),
        "estimate": (request.POST.get("estimate") or "").strip(),
        "kind": kind,
        "created_by": request.user,
    }
    try:
        with transaction.atomic():
            if kind == AIDevTask.Kind.FRONT:
                kwargs["priority_no"] = (
                    AIDevTask.objects.aggregate(
                        max_no=Max("priority_no")
                    )["max_no"] or 0
                ) + 1
            task = AIDevTask.objects.create(**kwargs)
    except IntegrityError:
        # Гонка двух одновременных созданий на один max+1 — честный 409.
        return JsonResponse(
            {"ok": False, "message": "Доска менялась — повторите создание"},
            status=409,
        )
    return JsonResponse({"ok": True, "id": task.pk})


@require_POST
def admin_task_reorder_view(request):
    """Перенумеровать фронт по присланному порядку (перетаскивание строк).

    Тело: JSON {"ids": [...]} — порядок front-тасков после дропа. Права:
    суперюзер или action.task_reorder. Множество id должно
    ТОЧНО совпасть с текущим множеством front-pk (гонка create/close → 409,
    доска на сервере не трогается). В transaction.atomic() строки получают
    номера 1..N — заодно уплотняются дырки от закрытых тасков (select_for_update
    не нужен: sqlite-тесты его не знают, конфликт уникальности в атомарном
    блоке даёт IntegrityError → 409).
    """
    if not request.user.is_authenticated:
        return _forbid()
    if not (is_superuser_user(request.user) or user_has_key(request.user, "action.task_reorder")):
        return _forbid("Порядок меняет только суперпользователь")
    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return _forbid("Некорректный запрос", status=400)
    ids = payload.get("ids")
    if not isinstance(ids, list) or not all(
        isinstance(value, int) for value in ids
    ):
        return _forbid("Некорректный порядок", status=400)

    expected_ids = list(
        AIDevTask.objects.filter(kind=AIDevTask.Kind.FRONT)
        .values_list("pk", flat=True)
    )
    if set(ids) != set(expected_ids) or len(ids) != len(expected_ids):
        return JsonResponse(
            {"ok": False, "message": "Доска изменилась — обновите страницу"},
            status=409,
        )
    try:
        with transaction.atomic():
            # Двухпроходная перенумерация: частичный уникальный констрейнт на
            # priority_no запрещает промежуточные коллизии (прямой присвоения
            # 1..N хватило на IntegrityError при перестановке соседей). Сначала
            # ВСЕ номера фронта сбрасываются в NULL (NULL не сравнивается
            # констрейнтом), затем присваиваются 1..N.
            AIDevTask.objects.filter(
                kind=AIDevTask.Kind.FRONT
            ).update(priority_no=None)
            for number, task_id in enumerate(ids, start=1):
                AIDevTask.objects.filter(
                    pk=task_id, kind=AIDevTask.Kind.FRONT
                ).update(priority_no=number)
    except IntegrityError:
        return JsonResponse(
            {"ok": False, "message": "Доска изменилась — обновите страницу"},
            status=409,
        )
    return JsonResponse({"ok": True})


@require_POST
def admin_task_claim_view(request, task_id):
    """Взять таск: любой с доступом к доске.

    Атомарный условный UPDATE: строка обновляется только если таск ещё
    свободен — второй конкурент получает 0 строк и 409 (sqlite-тесты
    не знают select_for_update(skip_locked=True), поэтому не он).
    """
    if not can_access_tasks(request):
        return _forbid()
    task = AIDevTask.objects.filter(pk=task_id).only("pk").first()
    if task is None:
        return JsonResponse({"ok": False, "message": "Таск не найден"}, status=404)
    claimed = (
        AIDevTask.objects.filter(pk=task_id, taken_by__isnull=True)
        .update(taken_by=request.user, taken_at=timezone.now())
    )
    if not claimed:
        return JsonResponse(
            {"ok": False, "message": "Таск уже взят"},
            status=409,
        )
    return JsonResponse({"ok": True})


@require_POST
def admin_task_close_view(request, task_id):
    """Закрыть таск — удаление с доски (суперюзер/action.task_close)."""
    if not request.user.is_authenticated:
        return _forbid()
    if not (is_superuser_user(request.user) or user_has_key(request.user, "action.task_close")):
        return _forbid("Закрывать таски может только суперпользователь")
    task = AIDevTask.objects.filter(pk=task_id).only("pk").first()
    if task is None:
        return JsonResponse({"ok": False, "message": "Таск не найден"}, status=404)
    task.delete()
    return JsonResponse({"ok": True})