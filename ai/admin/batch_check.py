"""Загрузка пакетов, дерево результатов и протокол проверок."""

from pathlib import PurePosixPath
from uuid import uuid4

from django import forms
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Avg, Count, FloatField, Prefetch, Q
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast
from django.http import HttpResponseForbidden, HttpResponseNotFound, JsonResponse
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods

from ..models import AICheckBatch, AICheckBatchFile
from .check_ai import CheckAiForm, SHORT_INPUT_WORDS, check_name_field
from .permissions import can_access_check_ai, is_superuser_user
from .site import ai_admin_site

MAX_BATCH_FILES = 1000
MAX_BATCH_MIB = 200
PROGRAM_EXTENSIONS = frozenset("""
    .asm .bas .c .cc .clj .cljc .cljs .cob .coffee .cpp .cs .cxx .d .dart .dpr .el .erl .ex
    .exs .f .f90 .fs .fsx .go .h .hh .hpp .hs .hxx .java .jl .js .jsx .kts .kt
    .lisp .lpr .lua .m .mjs .ml .mli .nim .ocaml .p .pas .php .pl .pp .pro .py .pyw .r .rb
    .rs .scala .scm .sh .sql .swift .tcl .ts .tsx .vb .vbs .zig
""".split())
IGNORED_FOLDERS = frozenset({"__pycache__", "node_modules", "venv"})
Status = AICheckBatchFile.Status


class BatchCheckFileForm(CheckAiForm):
    max_input_mib = 5


class BatchCheckForm(forms.Form):
    name = check_name_field()
    submission_id = forms.UUIDField(
        required=False,
        widget=forms.HiddenInput,
        error_messages={"invalid": "Форма устарела. Откройте новую пакетную проверку."},
    )
    skipped_files = forms.IntegerField(
        required=False,
        min_value=0,
        max_value=2_147_483_647 - MAX_BATCH_FILES,
    )


def _batch_created_response(batch):
    detail_url = reverse("ai_check_ai_batch_detail", args=[batch.pk])
    return JsonResponse({"url": detail_url}, status=201)


def _checks_for(user):
    if is_superuser_user(user):
        return AICheckBatch.objects.all()
    return AICheckBatch.objects.filter(created_by=user)


def _is_program(path):
    parts = path.split("/")
    return (
        len(path) <= 1024
        and len(parts) <= 32
        and "\\" not in path
        and not any(ord(char) < 32 for char in path)
        and all(part and not part.startswith(".") for part in parts)
        and not any(part.lower() in IGNORED_FOLDERS for part in parts[:-1])
        and PurePosixPath(path).suffix.lower() in PROGRAM_EXTENSIONS
    )


def _submit_batch(request):
    batch_form = BatchCheckForm(request.POST)
    if not batch_form.is_valid():
        if batch_form.errors.get("skipped_files"):
            return JsonResponse({"error": "Не удалось определить число пропущенных файлов."}, status=400)
        return JsonResponse({"error": next(iter(batch_form.errors.values()))[0]}, status=400)

    submission_id = batch_form.cleaned_data["submission_id"]
    if submission_id:
        existing = AICheckBatch.objects.filter(created_by=request.user, submission_id=submission_id).first()
        if existing:
            return _batch_created_response(existing)

    uploads = request.FILES.getlist("files")
    paths = request.POST.getlist("paths") or [upload.name for upload in uploads]
    if not uploads or len(uploads) != len(paths):
        return JsonResponse({"error": "Выберите папку с исходниками или файлы программ."}, status=400)
    if len(uploads) > MAX_BATCH_FILES:
        return JsonResponse({"error": f"В одном пакете может быть до {MAX_BATCH_FILES} файлов."}, status=400)

    programs = {}
    skipped = batch_form.cleaned_data["skipped_files"] or 0
    for path, upload in zip(paths, uploads):
        if not _is_program(path) or PurePosixPath(path).name != upload.name or path in programs:
            skipped += 1
            continue
        programs[path] = upload
    if not programs:
        return JsonResponse({"error": "В выбранных папках нет поддерживаемых файлов программ."}, status=400)
    if sum(upload.size for upload in programs.values()) > MAX_BATCH_MIB * 1024 * 1024:
        return JsonResponse({"error": f"Общий размер программ в пакете — до {MAX_BATCH_MIB} МиБ."}, status=400)

    items = []
    for path, upload in sorted(programs.items()):
        form = BatchCheckFileForm(data={}, files={"file": upload})
        item = AICheckBatchFile(relative_path=path)
        if form.is_valid():
            item.source_text = form.cleaned_data["text"]
            item.result["short_input"] = len(item.source_text.split()) < SHORT_INPUT_WORDS
        else:
            item.status = Status.ERROR
            item.checked_at = timezone.now()
            item.error = next(iter(form.errors.values()))[0]
        items.append(item)

    # Работа становится видна очереди только после записи всех её файлов.
    batch_fields = {
        "checker_name": request.user.get_full_name() or request.user.get_username(),
        "skipped_count": skipped,
        "name": batch_form.cleaned_data["name"],
    }
    with transaction.atomic():
        if submission_id:
            # Уникальность в БД также защищает от двух одновременных отправок.
            batch, created = AICheckBatch.objects.get_or_create(
                created_by=request.user, submission_id=submission_id, defaults=batch_fields,
            )
            if not created:
                return _batch_created_response(batch)
        else:
            batch = AICheckBatch.objects.create(created_by=request.user, **batch_fields)
        for item in items:
            item.batch_id = batch.pk
        AICheckBatchFile.objects.bulk_create(items, batch_size=20)
    return _batch_created_response(batch)


def _batch_page(request, **context):
    return TemplateResponse(request, "admin/ai/check_ai_batch.html", {
        **ai_admin_site.each_context(request),
        "title": "Пакетная проверка на ИИ",
        "check_ai_tab": "batch",
        "batch_form": BatchCheckForm(initial={"submission_id": uuid4()}),
        "recent_batches": _checks_for(request.user).filter(kind=AICheckBatch.Kind.BATCH).order_by("-id")[:10],
        "extensions": ",".join(sorted(PROGRAM_EXTENSIONS)),
        "ignored_folders": ",".join(sorted(IGNORED_FOLDERS)),
        "max_files": MAX_BATCH_FILES,
        "max_batch_mib": MAX_BATCH_MIB,
        "max_file_mib": BatchCheckFileForm.max_input_mib,
        **context,
    })


@require_http_methods(["GET", "POST"])
def admin_check_ai_batch_view(request):
    if not can_access_check_ai(request):
        return HttpResponseForbidden("У вас нет доступа к проверке на ИИ.")
    if request.method == "POST":
        return _submit_batch(request)
    return _batch_page(request)


def _empty_folder(name="", path=""):
    return {"name": name, "path": path, "folders": {}, "files": [], "count": 0, "sum": 0, "average": None}


def _result_tree(items):
    root = _empty_folder()
    for item in items:
        folder = root
        parts = item.relative_path.split("/")
        for index, part in enumerate(parts[:-1]):
            if part not in folder["folders"]:
                folder["folders"][part] = _empty_folder(part, "/".join(parts[:index + 1]))
            folder = folder["folders"][part]
            if item.status == Status.DONE:
                folder["count"] += 1
                folder["sum"] += item.result["ai_percent"]
                folder["average"] = folder["sum"] / folder["count"]
        folder["files"].append(item)
    # В шаблон отдаём списки: папка с именем «values» не должна затенить метод dict.values.
    folders = [root]
    for folder in folders:
        folder["folders"] = list(folder["folders"].values())
        folders.extend(folder["folders"])
    return root


@require_GET
def admin_check_ai_batch_detail_view(request, batch_id):
    if not can_access_check_ai(request):
        return HttpResponseForbidden("У вас нет доступа к проверке на ИИ.")
    batch = _checks_for(request.user).filter(pk=batch_id, kind=AICheckBatch.Kind.BATCH).first()
    if batch is None:
        return HttpResponseNotFound("Пакет проверки не найден.")
    items = list(batch.files.defer("source_text").order_by("relative_path"))
    if request.GET.get("fragment") == "programs":
        return TemplateResponse(request, "admin/ai/_check_ai_protocol_programs.html", {"items": items})
    finished = sum(item.checked_at is not None for item in items)
    context = {
        "batch": batch,
        "tree": _result_tree(items),
        "total": len(items),
        "finished": finished,
        "done": finished == len(items),
    }
    if request.GET.get("fragment") == "1":
        return TemplateResponse(request, "admin/ai/_check_ai_batch_results.html", context)
    return _batch_page(request, **context)


@require_GET
def admin_check_ai_protocol_view(request):
    if not can_access_check_ai(request):
        return HttpResponseForbidden("У вас нет доступа к проверке на ИИ.")
    checks = _checks_for(request.user).annotate(
        file_count=Count("files"),
        processed_count=Count("files__checked_at"),
        error_count=Count("files", filter=Q(files__status=Status.ERROR)),
        average_ai=Avg(
            Cast(KeyTextTransform("ai_percent", "files__result"), FloatField()),
            filter=Q(files__status=Status.DONE),
        ),
        average_human=Avg(
            Cast(KeyTextTransform("human_percent", "files__result"), FloatField()),
            filter=Q(files__status=Status.DONE),
        ),
    ).prefetch_related(Prefetch(
        "files",
        queryset=AICheckBatchFile.objects.filter(batch__kind=AICheckBatch.Kind.SINGLE).defer("source_text"),
        to_attr="single_files",
    )).order_by("-created_at", "-id")
    return TemplateResponse(request, "admin/ai/check_ai_protocol.html", {
        **ai_admin_site.each_context(request),
        "title": "Протокол проверки на ИИ",
        "check_ai_tab": "protocol",
        "page_obj": Paginator(checks, 50).get_page(request.GET.get("page")),
    })
