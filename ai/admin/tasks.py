"""Доска задач «Таски» (ТЗ 2026-10-19).

Суперюзер создаёт таски (название, описание, срок — текстом) и выставляет
приоритет (Высокий/Средний/Низкий — важное вверху, фильтр поверх); закрыть
(удалить с доски) может только суперюзер. Разработчик промптов берёт таск:
taken_by/taken_at пишутся атомарно (условный UPDATE — «первый успел»),
у взятого таска в таблице сразу ФИО + группа + дата взятия, повторно взять
нельзя. ModelAdmin не регистрируется — страница целиком кастомная,
по тому же образцу, что «Закреплённые пакетные решения» (pinned.py).
"""

from django.db.models import Case, When
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

# Приоритет на доске: Высокий → Средний → Низкий (аннотация pr_rank),
# внутри одного приоритета — свежие сверху.
_PRIORITY_RANK = Case(
    When(priority=AIDevTask.Priority.HIGH, then=0),
    When(priority=AIDevTask.Priority.NORMAL, then=1),
    default=2,
)


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
    """Страница «Таски»: таблица доски, форма создания (суперюзер), фильтр."""
    if not can_access_tasks(request):
        return HttpResponseForbidden("Access denied")
    guest = is_viewing_as_guest(request)
    superuser = is_superuser_user(request.user)

    selected = (request.GET.get("priority") or "").strip()
    rows_qs = AIDevTask.objects.select_related(
        "taken_by", "taken_by__external_dl_account"
    )
    if selected in dict(AIDevTask.Priority.choices):
        rows_qs = rows_qs.filter(priority=selected)
    tasks = list(
        rows_qs.annotate(pr_rank=_PRIORITY_RANK)
        .order_by("pr_rank", "-created_at", "-id")
    )
    edu_maps = education_title_maps()
    rows = []
    for task in tasks:
        rows.append({
            "id": task.pk,
            "priority": task.priority,
            "priority_display": task.get_priority_display(),
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

    context = {
        **ai_admin_site.each_context(request),
        "title": "Таски",
        "rows": rows,
        "priorities": AIDevTask.Priority.choices,
        "selected_priority": selected,
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
    task = AIDevTask.objects.create(
        title=title,
        description=(request.POST.get("description") or "").strip(),
        estimate=(request.POST.get("estimate") or "").strip(),
        priority=priority,
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
    """Закрыть таск — удаление с доски (только суперюзер, не в гостевом)."""
    if not is_superuser_user(request.user) or is_viewing_as_guest(request):
        return _forbid("Закрывать таски может только суперпользователь")
    task = AIDevTask.objects.filter(pk=task_id).only("pk").first()
    if task is None:
        return JsonResponse({"ok": False, "message": "Таск не найден"}, status=404)
    task.delete()
    return JsonResponse({"ok": True})