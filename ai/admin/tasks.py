"""Доска задач «Таски» (ТЗ 2026-10-19, правки 2026-10-20; архив 2026-10-20).

Два типа тасков-вкладок: «Фронт работы» (front) и «Хотелки» (wish) — у
каждого СВОЙ числовой уникальный номер с №1 (меньше = важнее, выдаётся
сервером: max+1 внутри вида; порядок меняют перетаскиванием строк →
tasks/reorder/ перенумеровывает 1..N в пределах присланного вида).
Псевдо-вкладки-фильтры: «Не взятые» (unclaimed) — весь свободный фронт,
«Поиск» (search) — ВСЕ активные таски (фронт первым) с фильтром «Кто взял»,
«Мои таски» (mine) — взятые пользователем, «Закрытые» (closed) — архив
выполненных. Закрытие НЕ удаляет таск: closed_at/closed_by ставятся,
priority_no сбрасывается (номерное пространство свободно), взятый
(closed) таск пропадает из всех рабочих видов и попадает в «Закрытые»
(разграничение по датам закрытия, типу и исполнителю + «кто дал»/«кто
выполнил»/«когда»), откуда суперюзер/action.task_close может ВЕРНУТЬ его
на доску
(tasks/reopen/: closed-поля в NULL, новый номер max+1 внутри вида; фронт
спрашивает — оставить исполнителя (taken_by сохраняется) или освободить
(тело {"free_executor": true} → в «Не взятые»)). Создать/закрыть/вернуть —
суперюзер либо ключи
action.task_create/task_close (см. «Группы доступа»); переставить —
superuser/action.task_reorder. Взять может любой, кто допущен в админку
(page.tasks — baseline, ТЗ 2026-10-19): taken_by/taken_at пишутся атомарно
(условный UPDATE — «первый успел»). ModelAdmin не регистрируется —
страница целиком кастомная, по тому же образцу, что «Закреплённые
пакетные решения» (pinned.py).
"""

import json
from datetime import date, datetime, timedelta

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


def _parse_date_param(value):
    """GET-дату «YYYY-MM-DD» (инпут type=date) → date; мусор/пусто → None
    (фильтр молча не применяется, как мусорный ?taken= на «Поиске»)."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _msk_day_start(day):
    """Полночь дня в Московском часовом поясе (aware) — граница фильтра по
    closed_at. UTC-полночь (TIME_ZONE=UTC) резала бы вечерние закрытия в
    соседний день относительно показанных в {% timezone moscow_tz %} дат."""
    return timezone.make_aware(
        datetime.combine(day, datetime.min.time()), MOSCOW_TZ
    )


def admin_tasks_view(request):
    """Страница «Таски»: вкладки, таблица, форма создания.

    Вкладки — серверные (?kind=), URL шарится. Реальные виды — front/wish
    (у обоих свои номера с №1); ПСЕВДО-вкладки-фильтры: unclaimed («Не
    взятые» — весь свободный фронт), search («Поиск» — все активные таски,
    фронт первым, фильтр ?taken=<user_id> по взявшему), mine («Мои таски» —
    взятые пользователем), closed («Закрытые» — архив: разграничение по
    датам закрытия ?closed_from/?closed_to (вкл., МСК), типу ?task_kind= и
    исполнителю ?done_by=<user_id>, «кто дал»/«кто
    выполнил»/«когда закрыт», возврат на доску).
    Перетаскивание включается draggable + data-reorder="1" только у тех,
    кому можно (право), и только на вкладках реальных видов (front/wish).
    """
    if not can_access_tasks(request):
        return HttpResponseForbidden("Access denied")
    superuser = is_superuser_user(request.user)
    active_taken = ""  # выбранный «Кто взял» на «Поиске» (id строкой)
    closed_from = None  # границы фильтра «Закрыт …—…» на вкладке «Закрытые»
    closed_to = None
    closed_kind = ""  # выбранный тип («Фронт работы»/«Хотелки») в архиве
    closed_done_by = ""  # выбранный «Кто выполнил» в архиве (id строкой)

    VIEW_MINE = "mine"
    VIEW_UNCLAIMED = "unclaimed"
    VIEW_SEARCH = "search"
    VIEW_CLOSED = "closed"
    kind = request.GET.get("kind") or AIDevTask.Kind.FRONT
    if kind not in dict(AIDevTask.Kind.choices) and kind not in (
        VIEW_MINE, VIEW_UNCLAIMED, VIEW_SEARCH, VIEW_CLOSED
    ):
        kind = AIDevTask.Kind.FRONT

    rows_qs = AIDevTask.objects.select_related(
        "taken_by", "taken_by__external_dl_account", "created_by"
    )
    is_closed_tab = kind == VIEW_CLOSED
    # Рабочие виды показывают ТОЛЬКО незакрытое (closed_at IS NULL), архив —
    # только закрытое: isnull = not is_closed_tab.
    rows_qs = rows_qs.filter(
        closed_at__isnull=not is_closed_tab
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
        # «Поиск»: ВСЕ активные таски, фронт первым («front» < «wish»
        # строково). NULL-номера легаси — в конец (nulls_last, см. выше).
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
    elif kind == VIEW_CLOSED:
        # Архив «Закрытые»: свежие закрытия первыми; диапазон дат закрытия —
        # обе границы включительно («по» = до конца дня, МСК); тип и
        # исполнитель — как «Кто взял» на «Поиске» (мусорное значение —
        # фильтр молча не применяется).
        rows_qs = rows_qs.order_by("-closed_at", "-id")
        closed_from = _parse_date_param(request.GET.get("closed_from"))
        closed_to = _parse_date_param(request.GET.get("closed_to"))
        if closed_from:
            rows_qs = rows_qs.filter(closed_at__gte=_msk_day_start(closed_from))
        if closed_to:
            rows_qs = rows_qs.filter(
                closed_at__lt=_msk_day_start(closed_to) + timedelta(days=1)
            )
        closed_kind = (request.GET.get("task_kind") or "").strip()
        if closed_kind and closed_kind not in dict(AIDevTask.Kind.choices):
            closed_kind = ""
        if closed_kind:
            rows_qs = rows_qs.filter(kind=closed_kind)
        closed_done_by = (request.GET.get("done_by") or "").strip()
        if closed_done_by and closed_done_by.isdigit():
            rows_qs = rows_qs.filter(taken_by_id=int(closed_done_by))
        else:
            closed_done_by = ""
    else:
        rows_qs = rows_qs.filter(kind=kind).order_by(
            "priority_no", "-created_at", "-id"
        )

    edu_maps = education_title_maps()
    kind_labels = dict(AIDevTask.Kind.choices)
    rows = []
    for task in rows_qs:
        rows.append({
            "id": task.pk,
            "kind": task.kind,
            # Метка типа нужна только на архивной вкладке (там нет колонок
            # видов), но строится одинаково для всех строк.
            "kind_label": kind_labels.get(task.kind, task.kind),
            "priority_no": task.priority_no,
            "title": task.title,
            "description": task.description,
            "estimate": task.estimate,
            "creator": _claimant_display(task.created_by, edu_maps),
            "created_at": task.created_at,
            "taken": task.taken_by_id is not None,
            "claimant": _claimant_display(task.taken_by, edu_maps),
            "taken_at": task.taken_at,
            "closed_at": task.closed_at,
            "claim_url": f"{TASKS_URL}{task.pk}/claim/",
            "close_url": f"{TASKS_URL}{task.pk}/close/",
            "reopen_url": f"{TASKS_URL}{task.pk}/reopen/",
        })

    can_reorder_kind = kind in (AIDevTask.Kind.FRONT, AIDevTask.Kind.WISH)
    can_reorder = (
        (superuser or user_has_key(request.user, "action.task_reorder"))
        and can_reorder_kind
    )
    def _taker_options(user_ids):
        """Селект-варианты «Кто взял/выполнил»: «ФИО (группа)» по списку pk
        (DRY для «Поиска» и архива; порядок — по username, у обоих фильтров)."""
        options = []
        for user in get_user_model().objects.filter(
            pk__in=set(uid for uid in user_ids if uid)
        ).order_by("username"):
            options.append(
                {"id": user.pk, "name": _claimant_display(user, edu_maps)}
            )
        return options

    # Фильтр «Кто взял» на «Поиске»: только те, кто хоть что-то взял.
    taken_options = []
    if kind == VIEW_SEARCH:
        taken_options = _taker_options(
            AIDevTask.objects.exclude(taken_by=None)
            .filter(closed_at__isnull=True)
            .values_list("taken_by_id", flat=True)
            .distinct()
        )
    # «Кто выполнил» в архиве: те, по чьим закрытым таскам есть исполнитель.
    closed_done_options = []
    if is_closed_tab:
        closed_done_options = _taker_options(
            AIDevTask.objects.exclude(taken_by=None)
            .filter(closed_at__isnull=False)
            .values_list("taken_by_id", flat=True)
            .distinct()
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
            {"kind": VIEW_CLOSED, "label": "Закрытые", "url": f"{TASKS_URL}?kind={VIEW_CLOSED}"},
        ],
        "create_kinds": [
            {"kind": value, "label": label, "selected": value == (kind if kind in dict(AIDevTask.Kind.choices) else AIDevTask.Kind.FRONT)}
            for value, label in AIDevTask.Kind.choices
        ],
        "can_reorder": can_reorder,
        "taken_options": taken_options,
        "active_taken": active_taken,
        "closed_from": closed_from.isoformat() if closed_from else "",
        "closed_to": closed_to.isoformat() if closed_to else "",
        "closed_done_options": closed_done_options,
        "closed_done_by": closed_done_by,
        "closed_kind": closed_kind,
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
    БЕЗ закрытых (гонка create/close/reopen и вид-мисматч → 409, доска на
    сервере не трогается).
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
        AIDevTask.objects.filter(kind=kind, closed_at__isnull=True)
        .values_list("pk", flat=True)
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
            # Сначала ВСЕ номера АКТИВНЫХ строк вида сбрасываются в NULL
            # (NULL не сравнивается констрейнтом; закрытые уже NULL),
            # затем присваиваются 1..N.
            AIDevTask.objects.filter(
                kind=kind, closed_at__isnull=True
            ).update(priority_no=None)
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
    свободен И не закрыт (закрытое в архиве нельзя «перевзять») — второй
    конкурент получает 0 строк и 409 (sqlite-тесты
    не знают select_for_update(skip_locked=True), поэтому не он).
    """
    if not can_access_tasks(request):
        return _forbid()
    task = AIDevTask.objects.filter(pk=task_id).only(
        "pk", "closed_at"
    ).first()
    if task is None:
        return JsonResponse({"ok": False, "message": "Таск не найден"}, status=404)
    if task.closed_at is not None:
        return JsonResponse(
            {"ok": False, "message": "Таск закрыт — верните его на доску прежде чем взять"},
            status=409,
        )
    claimed = (
        AIDevTask.objects.filter(
            pk=task_id, taken_by__isnull=True, closed_at__isnull=True
        )
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
    """Закрыть таск — увод с доски в архив «Закрытые», НЕ удаление
    (суперюзер/action.task_close).

    Строка остаётся: closed_at/closed_by ставятся, priority_no сбрасывается
    (номерное пространство вида освобождается целиком — закрытые никогда не
    конфликтуют с нумерацией и перенумерацией активных строк); taken_by/
    taken_at НЕ трогаются — в архиве видно «кто выполнил». Условный UPDATE:
    второй закрывающий получает 0 строк и 409 (гонка, как у claim).
    Вернуть на доску — admin_task_reopen_view.
    """
    if not request.user.is_authenticated:
        return _forbid()
    if not (is_superuser_user(request.user) or user_has_key(request.user, "action.task_close")):
        return _forbid("Закрывать таски может только суперпользователь")
    closed = AIDevTask.objects.filter(
        pk=task_id, closed_at__isnull=True
    ).update(
        closed_at=timezone.now(), closed_by=request.user, priority_no=None,
    )
    if not closed:
        if not AIDevTask.objects.filter(pk=task_id).exists():
            return JsonResponse({"ok": False, "message": "Таск не найден"}, status=404)
        return JsonResponse({"ok": False, "message": "Таск уже закрыт"}, status=409)
    return JsonResponse({"ok": True})


@require_POST
def admin_task_reopen_view(request, task_id):
    """Вернуть закрытый таск на доску (суперюзер/action.task_close).

    Инверсия close (разработчик случайно закрыл таск или выполнил его не
    так, как нужно). Тело опционально JSON {"free_executor": true} —
    выбор спрашивает фронт («Вернуть» раскрывает «С исполнителем» /
    «Освободить»): исполнитель СОХРАНЯЕТСЯ (таск возвращается тому, кто
    его взял, — пусть переделает) или СБРАСЫВАЕТСЯ (taken_by/taken_at в
    NULL → таск попадёт в «Не взятые»). closed_at/closed_by сбрасываются
    всегда; номер присваивается заново (max+1 внутри вида — закрытые
    номер не занимают, таск идёт в конец доски; гонка с созданием/
    возвратом на один max+1 → IntegrityError → честный 409).
    """
    if not request.user.is_authenticated:
        return _forbid()
    if not (is_superuser_user(request.user) or user_has_key(request.user, "action.task_close")):
        return _forbid("Возвращать таски может только суперпользователь")
    # Тело опционально: «С исполнителем» шлёт POST вообще без тела (b"") —
    # это НЕ ошибка, просто пустой payload; непарсящийся НЕпустой — 400.
    raw = request.body or b""
    if raw.strip():
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return _forbid("Некорректный запрос", status=400)
    else:
        payload = {}
    task = AIDevTask.objects.filter(pk=task_id).only("kind", "closed_at").first()
    if task is None:
        return JsonResponse({"ok": False, "message": "Таск не найден"}, status=404)
    if task.closed_at is None:
        return JsonResponse({"ok": False, "message": "Таск уже на доске"}, status=409)
    reopen_kwargs = {"closed_at": None, "closed_by": None, "priority_no": None}
    # «Освободить»: taken_by/taken_at не трогаем по умолчанию — только при
    # явном флаге (клиент выбирает между «переделает тот же» и «в Не взятые»).
    if payload.get("free_executor"):
        reopen_kwargs["taken_by"] = None
        reopen_kwargs["taken_at"] = None
    try:
        with transaction.atomic():
            next_no = (
                AIDevTask.objects.filter(kind=task.kind).aggregate(
                    max_no=Max("priority_no")
                )["max_no"] or 0
            ) + 1
            reopen_kwargs["priority_no"] = next_no
            reopened = AIDevTask.objects.filter(
                pk=task_id, closed_at__isnull=False
            ).update(**reopen_kwargs)
    except IntegrityError:
        return JsonResponse(
            {"ok": False, "message": "Доска изменилась — обновите страницу"},
            status=409,
        )
    if not reopened:
        # close/reopen успел кто-то другой между чтением строки и UPDATE —
        # вернуться на «уже на доске» нельзя, честный отказ.
        return JsonResponse({"ok": False, "message": "Таск уже закрыт"}, status=409)
    return JsonResponse({"ok": True})