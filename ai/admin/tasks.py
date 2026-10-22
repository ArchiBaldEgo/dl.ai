"""Доска задач «Таски» (ТЗ 2026-10-19).

Суперюзер создаёт таски (название, описание, срок — текстом), выставляет
приоритет (Высокий/Средний/Низкий — бейдж в строке и фильтр поверх) и тип
(«Фронт» / «Хотелка»). Закрыть и вернуть таск может только суперюзер: закрытый
таск НЕ удаляется, а уходит в «закрытые» (closed_at) и возвращается кнопкой
«Вернуть» — с тем же типом и тем же взятым. Разработчик промптов берёт
таск: taken_by/taken_at пишутся атомарно (условный UPDATE — «первый успел»),
у взятого таска в таблице сразу ФИО + группа + дата взятия, повторно взять
нельзя. Порядок доски — свежие сверху (новый таск виден сразу); приоритет
на порядок не влияет. ModelAdmin не регистрируется — страница целиком
кастомная, по тому же образцу, что «Закреплённые пакетные решения» (pinned.py).
"""

from django.http import HttpResponseForbidden, JsonResponse
from django.template.response import TemplateResponse
from django.utils import timezone
from django.views.decorators.http import require_POST

from ..constants import MOSCOW_TZ
from ..models import AIDevTask, ExternalDLAccount
from ..services.education_directory import education_title_maps, group_display
from .guest_mode import is_viewing_as_guest
from .permissions import (
    can_access_tasks,
    is_prompt_developer_user,
    is_staff_or_superuser,
    is_superuser_user,
)
from .site import ai_admin_site

TASKS_URL = "/ai/admin/tasks/"

# Порядок доски: свежие таски сверху (новый таск виден сразу после создания).
# Приоритет — бейдж в строке и фильтр, на порядок не влияет.
_TASKS_ORDER = ("-created_at", "-id")


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
    """Страница «Таски»: таблица доски, форма создания (суперюзер), фильтры."""
    if not can_access_tasks(request):
        return HttpResponseForbidden("Access denied")
    guest = is_viewing_as_guest(request)
    superuser = is_superuser_user(request.user)

    selected = (request.GET.get("priority") or "").strip()
    selected_kind = (request.GET.get("kind") or "").strip()
    # Показ закрытых: open (по умолчанию) | closed | all.
    state = (request.GET.get("state") or "").strip().lower()
    if state not in ("open", "closed", "all"):
        state = "open"
    rows_qs = AIDevTask.objects.select_related(
        "taken_by", "taken_by__external_dl_account"
    )
    if selected in dict(AIDevTask.Priority.choices):
        rows_qs = rows_qs.filter(priority=selected)
    if selected_kind in dict(AIDevTask.Kind.choices):
        rows_qs = rows_qs.filter(kind=selected_kind)
    if state == "open":
        rows_qs = rows_qs.filter(closed_at__isnull=True)
    elif state == "closed":
        rows_qs = rows_qs.filter(closed_at__isnull=False)
    tasks = list(rows_qs.order_by(*_TASKS_ORDER))
    edu_maps = education_title_maps()
    rows = []
    for task in tasks:
        rows.append({
            "id": task.pk,
            "priority": task.priority,
            "priority_display": task.get_priority_display(),
            "kind": task.kind,
            "kind_display": task.get_kind_display(),
            "title": task.title,
            "description": task.description,
            "estimate": task.estimate,
            "created_at": task.created_at,
            "taken": task.taken_by_id is not None,
            "claimant": _claimant_display(task.taken_by, edu_maps),
            "taken_at": task.taken_at,
            "closed": task.closed_at is not None,
            "closed_at": task.closed_at,
            "claim_url": f"{TASKS_URL}{task.pk}/claim/",
            "close_url": f"{TASKS_URL}{task.pk}/close/",
            "restore_url": f"{TASKS_URL}{task.pk}/restore/",
        })

    context = {
        **ai_admin_site.each_context(request),
        "title": "Таски",
        "rows": rows,
        "priorities": AIDevTask.Priority.choices,
        "selected_priority": selected,
        "kinds": AIDevTask.Kind.choices,
        "selected_kind": selected_kind,
        "state": state,
        "can_create": superuser and not guest,
        "can_close": superuser and not guest,
        "moscow_tz": MOSCOW_TZ,
    }
    return TemplateResponse(request, "admin/ai/tasks.html", context)


@require_POST
def admin_task_create_view(request):
    """Создание таска (только суперюзер, не в гостевом режиме)."""
    if not is_superuser_user(request.user) or is_viewing_as_guest(request):
        return _forbid("Создавать таски может только суперпользователь")
    title = (request.POST.get("title") or "").strip()
    if not title:
        return JsonResponse(
            {"ok": False, "message": "Название таска обязательно"}, status=400
        )
    priority = (request.POST.get("priority") or AIDevTask.Priority.NORMAL).strip()
    if priority not in dict(AIDevTask.Priority.choices):
        return JsonResponse(
            {"ok": False, "message": "Неизвестный приоритет"}, status=400
        )
    kind = (request.POST.get("kind") or AIDevTask.Kind.FRONT).strip()
    if kind not in dict(AIDevTask.Kind.choices):
        return JsonResponse(
            {"ok": False, "message": "Неизвестный тип таска"}, status=400
        )
    task = AIDevTask.objects.create(
        title=title,
        description=(request.POST.get("description") or "").strip(),
        estimate=(request.POST.get("estimate") or "").strip(),
        priority=priority,
        kind=kind,
        created_by=request.user,
    )
    return JsonResponse({"ok": True, "id": task.pk})


@require_POST
def admin_task_claim_view(request, task_id):
    """Взять таск (суперюзер ИЛИ prompt_developer; гостю нельзя).

    Атомарный условный UPDATE: строка обновляется только если таск ещё
    свободен — второй конкурент получает 0 строк и 409 (sqlite-тесты
    не знают select_for_update(skip_locked=True), поэтому не он).
    """
    if not can_access_tasks(request):
        return _forbid("Access denied")
    if is_viewing_as_guest(request):
        return _forbid("В гостевом режиме доска только для просмотра")
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
    """Закрыть таск — уходит в «закрытые» (только суперюзер, не в гостевом).

    Таск НЕ удаляется: кнопка «Вернуть» возвращает его в открытые — с тем же
    типом и тем же взятым (исполнителем). Закрытые видны в доске при фильтре
    «Закрытые» / «Все».
    """
    if not is_superuser_user(request.user) or is_viewing_as_guest(request):
        return _forbid("Закрывать таски может только суперпользователь")
    task = AIDevTask.objects.filter(pk=task_id).only("pk", "closed_at").first()
    if task is None:
        return JsonResponse({"ok": False, "message": "Таск не найден"}, status=404)
    if task.closed_at is None:
        AIDevTask.objects.filter(pk=task.pk, closed_at__isnull=True).update(
            closed_at=timezone.now(),
        )
    return JsonResponse({"ok": True})


@require_POST
def admin_task_restore_view(request, task_id):
    """Вернуть закрытый таск в открытые (только суперюзер, не в гостевом).

    Тип и взявший (исполнитель) сохраняются — восстанавливается только факт
    закрытия.
    """
    if not is_superuser_user(request.user) or is_viewing_as_guest(request):
        return _forbid("Возвращать таски может только суперпользователь")
    task = AIDevTask.objects.filter(pk=task_id).only("pk", "closed_at").first()
    if task is None:
        return JsonResponse({"ok": False, "message": "Таск не найден"}, status=404)
    AIDevTask.objects.filter(pk=task.pk, closed_at__isnull=False).update(
        closed_at=None,
    )
    return JsonResponse({"ok": True})