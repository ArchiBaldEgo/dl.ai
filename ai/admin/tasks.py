"""Доска задач «Таски» (ТЗ 2026-10-19, правки 2026-10-20).

Два типа тасков-вкладок: «Фронт работы» (front) и «Хотелки» (wish) — у
каждого СВОЙ числовой уникальный номер с №1 (меньше = важнее, выдаётся
сервером: max+1 внутри вида; порядок меняют перетаскиванием строк →
tasks/reorder/ перенумеровывает 1..N в пределах присланного вида).
Псевдо-вкладки-фильтры: «Не взятые» (unclaimed) — весь свободный фронт,
«Поиск» (search) — ВСЕ таски (фронт первым) с фильтром «Кто взял»,
«Мои таски» (mine) — взятые пользователем. Закрытый таск удалён с доски,
значит всё показанное — работающее. Создать/закрыть — суперюзер либо
ключи action.task_create/task_close (см. «Группы доступа»); переставить —
superuser/action.task_reorder. Взять может любой, кто допущен в админку
(page.tasks — baseline, ТЗ 2026-10-19): taken_by/taken_at пишутся атомарно
(условный UPDATE — «первый успел»). ModelAdmin не регистрируется —
страница целиком кастомная, по тому же образцу, что «Закреплённые
пакетные решения» (pinned.py).
"""

import json

from django.contrib.auth import get_user_model
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
    """Страница «Таски»: вкладки, таблица, форма создания.

    Вкладки — серверные (?kind=), URL шарится. Реальные виды — front/wish
    (у обоих свои номера с №1); ПСЕВДО-вкладки-фильтры: unclaimed («Не
    взятые» — весь свободный фронт), search («Поиск» — все таски, фронт
    первым, фильтр ?taken=<user_id> по взявшему), mine («Мои таски» —
    взятые пользователем; закрытый таск удалён с доски, поэтому всё
    показанное — активное).
    Перетаскивание включается draggable + data-reorder="1" только у тех,
    кому можно (право), и только на вкладках реальных видов (front/wish).
    """
    if not can_access_tasks(request):
        return HttpResponseForbidden("Access denied")
    superuser = is_superuser_user(request.user)
    active_taken = ""  # выбранный «Кто взял» на «Поиске» (id строкой)

    VIEW_MINE = "mine"
    VIEW_UNCLAIMED = "unclaimed"
    VIEW_SEARCH = "search"
    kind = request.GET.get("kind") or AIDevTask.Kind.FRONT
    if kind not in dict(AIDevTask.Kind.choices) and kind not in (
        VIEW_MINE, VIEW_UNCLAIMED, VIEW_SEARCH
    ):
        kind = AIDevTask.Kind.FRONT

    rows_qs = AIDevTask.objects.select_related(
        "taken_by", "taken_by__external_dl_account"
    )
    if kind == VIEW_MINE:
        # Псевдо-вкладка «Мои таски»: ТОЛЬКО взятые пользователем. NULL-номера
        # легаси ВСЕГДА в конец: nulls_last явно, иначе sqlite (NULLs first)
        # и Postgres (NULLs last) расходятся.
        rows_qs = rows_qs.filter(
            taken_by=request.user
        ).order_by(F("priority_no").asc(nulls_last=True), "-created_at", "-id")
    elif kind == VIEW_UNCLAIMED:
        # По ТЗ: «Не взятые» — весь ВОЛЬНЫЙ фронт; взять можно прямо отсюда.
        rows_qs = rows_qs.filter(
            kind=AIDevTask.Kind.FRONT, taken_by__isnull=True
        ).order_by("priority_no", "-created_at", "-id")
    elif kind == VIEW_SEARCH:
        # «Поиск»: ВСЕ таски, фронт первым («front» < «wish» строково).
        # NULL-номера легаси — в конец (nulls_last, см. выше).
        rows_qs = rows_qs.order_by(
            "kind", F("priority_no").asc(nulls_last=True), "-created_at", "-id"
        )
        taker_param = (request.GET.get("taken") or "").strip()
        if taker_param:
            try:
                rows_qs = rows_qs.filter(taken_by_id=int(taker_param))
            except ValueError:
                pass  # мусорное значение — просто показываем все
        active_taken = taker_param if taker_param.isdigit() else ""
    else:
        rows_qs = rows_qs.filter(kind=kind).order_by(
            "priority_no", "-created_at", "-id"
        )

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

    can_reorder_kind = kind in (AIDevTask.Kind.FRONT, AIDevTask.Kind.WISH)
    can_reorder = (
        (superuser or user_has_key(request.user, "action.task_reorder"))
        and can_reorder_kind
    )
    # Фильтр «Кто взял» на «Поиске»: только те, кто хоть что-то взял.
    taken_options = []
    if kind == VIEW_SEARCH:
        taker_ids = (
            AIDevTask.objects.exclude(taken_by=None)
            .values_list("taken_by_id", flat=True)
            .distinct()
        )
        for user in get_user_model().objects.filter(
            pk__in=set(tid for tid in taker_ids if tid)
        ).order_by("username"):
            taken_options.append(
                {"id": user.pk, "name": _claimant_display(user, edu_maps)}
            )
    context = {
        **ai_admin_site.each_context(request),
        "title": "Таски",
        "rows": rows,
        "active_kind": kind,
        "tabs": [
            {"kind": value, "label": label, "url": f"{TASKS_URL}?kind={value}"}
            for value, label in AIDevTask.Kind.choices
        ] + [
            {"kind": VIEW_UNCLAIMED, "label": "Не взятые", "url": f"{TASKS_URL}?kind={VIEW_UNCLAIMED}"},
            {"kind": VIEW_SEARCH, "label": "Поиск", "url": f"{TASKS_URL}?kind={VIEW_SEARCH}"},
            {"kind": VIEW_MINE, "label": "Мои таски", "url": f"{TASKS_URL}?kind={VIEW_MINE}"},
        ],
        "create_kinds": [
            {"kind": value, "label": label, "selected": value == (kind if kind in dict(AIDevTask.Kind.choices) else AIDevTask.Kind.FRONT)}
            for value, label in AIDevTask.Kind.choices
        ],
        "can_reorder": can_reorder,
        "taken_options": taken_options,
        "active_taken": active_taken,
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

    Номер приоритета присваивает СЕРВЕР (max+1 ВНУТРИ вида таска, атомарно)
    — клиент его прислать не может; у фронта и хотелок нумерация раздельная
    (оба с №1, констрейнт по (kind, priority_no)).
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
            kwargs["priority_no"] = (
                AIDevTask.objects.filter(kind=kind).aggregate(
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
    """Перенумеровать таски одного вида по присланному порядку (drag&drop).

    Тело: JSON {"kind": "front"|"wish", "ids": [...]} — порядок тасков вида
    после дропа (kind опционален, дефолт front — назадсовместимо с прежним
    телом {"ids": [...]}). Права: суперюзер или action.task_reorder.
    Множество id должно ТОЧНО совпасть с текущим множеством pk этого вида
    (гонка create/close и вид-мисматч → 409, доска на сервере не трогается).
    В transaction.atomic() строки получают номера 1..N — заодно уплотняются
    дырки от закрытых тасков (select_for_update не нужен: sqlite-тесты его
    не знают, конфликт уникальности в атомарном блоке даёт IntegrityError →
    409; констрейнт по (kind, priority_no) — сброс номеров СВОЕГО вида не
    задевает номера другого).
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
    kind = payload.get("kind") or AIDevTask.Kind.FRONT
    if kind not in dict(AIDevTask.Kind.choices):
        return JsonResponse({"ok": False, "message": "Неизвестный тип таска"}, status=400)

    expected_ids = list(
        AIDevTask.objects.filter(kind=kind).values_list("pk", flat=True)
    )
    if set(ids) != set(expected_ids) or len(ids) != len(expected_ids):
        return JsonResponse(
            {"ok": False, "message": "Доска изменилась — обновите страницу"},
            status=409,
        )
    try:
        with transaction.atomic():
            # Двухпроходная перенумерация: частичный уникальный констрейнт
            # (kind, priority_no) запрещает промежуточные коллизии (прямое
            # присвоение 1..N давало IntegrityError на перестановке соседей).
            # Сначала ВСЕ номера этого вида сбрасываются в NULL (NULL не
            # сравнивается констрейнтом), затем присваиваются 1..N.
            AIDevTask.objects.filter(kind=kind).update(priority_no=None)
            for number, task_id in enumerate(ids, start=1):
                AIDevTask.objects.filter(pk=task_id, kind=kind).update(
                    priority_no=number
                )
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