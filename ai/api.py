# ai/api.py
import json

from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Count, Q
from django.http import JsonResponse
from django.views.decorators.http import require_POST

from .models import AIModelTestResult, AIModelTestRun


@staff_member_required
@require_POST
def get_task_status(request):
    """
    Статусы задач для дерева ARM.

    POST:
        course_id=1450
        node_ids=2606747,2606748,...   (comma-separated)

    Ответ:
        {"ok": true, "statuses": {"2606747": 0, "2606748": 1, ...}}

    Статусы:
        0 — посылок не было
        1 — есть хотя бы одна solved
        2 — посылки были, но solved нет
    """
    try:
        if request.content_type == "application/json":
            payload = json.loads(request.body or "{}")
        else:
            payload = request.POST

        course_id = int(payload.get("course_id"))
        raw_node_ids = payload.get("node_ids")

        if isinstance(raw_node_ids, str):
            node_ids = [int(x) for x in raw_node_ids.split(",") if x.strip()]
        else:
            node_ids = [int(x) for x in (raw_node_ids or [])]
    except (TypeError, ValueError, json.JSONDecodeError):
        return JsonResponse(
            {"ok": False, "message": "Некорректные course_id или node_ids"},
            status=400,
        )

    if not node_ids:
        return JsonResponse({"ok": True, "statuses": {}})

    if len(node_ids) > 2000:
        return JsonResponse(
            {"ok": False, "message": "Слишком много node_ids за один запрос"},
            status=400,
        )

    # Агрегируем batch-результаты по DL nodeId:
    #   submissions — сколько пар (задача×модель) всего попытались
    #   solved      — сколько из них с verdict='solved'
    # Legacy single find-error строки имеют task=NULL и отсекаются
    # неявно (task__node_id__in=node_ids).
    rows = (
        AIModelTestResult.objects
        .filter(
            run__run_type=AIModelTestRun.RUN_TYPE_BATCH,
            run__course_id=course_id,
            task__node_id__in=node_ids,
            verdict__isnull=False,
        )
        .values("task__node_id")
        .annotate(
            submissions=Count("id"),
            solved=Count(
                "id",
                filter=Q(verdict=AIModelTestResult.VERDICT_SOLVED),
            ),
        )
    )

    by_node = {
        int(row["task__node_id"]): (
            int(row["submissions"]),
            int(row["solved"]),
        )
        for row in rows
    }

    statuses = {}
    for node_id in node_ids:
        submissions, solved = by_node.get(node_id, (0, 0))
        if submissions == 0:
            statuses[str(node_id)] = 0
        elif solved > 0:
            statuses[str(node_id)] = 1
        else:
            statuses[str(node_id)] = 2

    return JsonResponse({"ok": True, "statuses": statuses})