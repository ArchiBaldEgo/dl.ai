"""Admin views for the «Документация» section.

Три главы документации (см. ai/services/docs.py) открываются как страница
с HTML-рендером markdown и скачиваются как .md. Доступ по роли главы:
"any" — всем в админке, "staff" — staff/суперюзер, "superuser" — суперюзер.
В гостевом режиме суперюзер видит только главу для разработчика промптов
(доки "staff"/"superuser" скрываются вместе с остальными суперюзерскими
инструментами навигации).
"""

from urllib.parse import quote

from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import redirect, render

from ..services.docs import (
    DocUnavailableError,
    all_chapters,
    get_chapter,
    read_chapter_markdown,
    render_chapter_html,
    visible_chapter_slugs,
)
from .permissions import is_staff_or_superuser, is_superuser_user
from .site import ai_admin_site


def _chapter_visible_for_user(chapter, request) -> bool:
    """Глава доступна, если ранг роли главы не выше ранга пользователя.

    Гостевой режим визуально понижает суперюзера до разработчика промптов
    (см. guest_mode.is_viewing_as_guest) — придерживаемся того же правила.
    """
    from .guest_mode import is_viewing_as_guest

    if is_viewing_as_guest(request):
        # Гостевой режим = уровень разработчика промптов: только главы "any".
        return chapter.min_role == "any"
    if chapter.min_role == "any":
        return True
    if is_superuser_user(request.user):
        return True
    return chapter.min_role == "staff" and is_staff_or_superuser(request.user)


def _visible_chapters(request):
    return [c for c in all_chapters() if c.in_admin_nav and _chapter_visible_for_user(c, request)]


def _markdown_response(markdown_data: dict) -> HttpResponse:
    """Скачивание .md: text/markdown + UTF-8 Content-Disposition (RFC 5987)."""
    filename = quote(markdown_data["filename"])
    response = HttpResponse(markdown_data["text"], content_type="text/markdown; charset=utf-8")
    response["Content-Disposition"] = f"attachment; filename=\"docs.md\"; filename*=UTF-8''{filename}"
    return response


@ai_admin_site.admin_view
def admin_docs_index_view(request):
    """Redirect на первую доступную главу документации."""
    slugs = [c.slug for c in _visible_chapters(request)]
    if not slugs:
        return HttpResponseForbidden("Документация недоступна для вашей роли")
    return redirect("ai_docs_detail", slug=slugs[0])


@ai_admin_site.admin_view
def admin_docs_view(request, slug):
    """Страница документации: HTML-рендер главы + перекрёстные ссылки."""
    chapter = get_chapter(slug)
    if chapter is None or not chapter.in_admin_nav:
        return HttpResponseForbidden("Глава документации не найдена")
    if not _chapter_visible_for_user(chapter, request):
        return HttpResponseForbidden("Эта документация доступна только для других ролей")
    try:
        rendered = render_chapter_html(slug)
        fingerprint = rendered["fingerprint"]
    except DocUnavailableError as exc:
        return render(request, "admin/ai/docs.html", {
            "title": "Документация",
            "chapter_title": chapter.title,
            "html": f"<p>{exc}</p>",
            "download_url": None,
            "chapters": _visible_chapters(request),
            "current_slug": slug,
            "fingerprint": None,
            **ai_admin_site.each_context(request),
        })
    return render(request, "admin/ai/docs.html", {
        "title": chapter.title,
        "chapter_title": chapter.title,
        "html": rendered["html"],
        "download_url": f"/ai/admin/docs/{slug}/download/",
        "chapters": _visible_chapters(request),
        "current_slug": slug,
        "fingerprint": fingerprint,
        **ai_admin_site.each_context(request),
    })


@ai_admin_site.admin_view
def admin_docs_download_view(request, slug):
    """Скачивание главы как .md-файла."""
    chapter = get_chapter(slug)
    if chapter is None:
        return HttpResponseForbidden("Глава документации не найдена")
    if not _chapter_visible_for_user(chapter, request):
        return HttpResponseForbidden("Эта документация доступна только для других ролей")
    try:
        return _markdown_response(read_chapter_markdown(slug))
    except DocUnavailableError as exc:
        return HttpResponseForbidden(str(exc))


@ai_admin_site.admin_view
def admin_docs_content_view(request, slug):
    """Текущий HTML главы ({success, title, html, fingerprint}) для поллинга.

    Права — те же, что у страницы главы. fingerprint (mtime, size) — фронт
    заменяет тело страницы, только когда файл документации изменился.
    """
    chapter = get_chapter(slug)
    if chapter is None or not chapter.in_admin_nav:
        return JsonResponse({"success": False, "error": "Глава документации не найдена"}, status=404)
    if not _chapter_visible_for_user(chapter, request):
        return JsonResponse({"success": False, "error": "forbidden"}, status=403)
    try:
        rendered = render_chapter_html(slug)
    except DocUnavailableError as exc:
        return JsonResponse({"success": False, "error": str(exc)}, status=404)
    return JsonResponse({
        "success": True,
        "title": rendered["title"],
        "html": rendered["html"],
        "fingerprint": rendered["fingerprint"],
    })