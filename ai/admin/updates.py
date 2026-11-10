"""Admin view for the "Обновления" (Updates) section.

Shows the commit history stored in UpdateLog with filtering and search
by author and date. Only superusers can access this page.

Authors are git nicks in raw form (UpdateLog.author); the page renders them
through the AuthorAlias directory (ник → ФИО + группа) resolved at render
time, so editing the alias fixes past rows retroactively.
"""

from django.shortcuts import render
from django.http import HttpResponseForbidden
from django.db.models import Q

from ..models import AuthorAlias, UpdateLog
from ..services.user_search import flat_text_q
from .permissions import can_access_updates
from .site import ai_admin_site


@ai_admin_site.admin_view
def admin_updates_view(request):
    """Render the updates/commit history page (staff/superuser или page.updates)."""
    if not can_access_updates(request):
        return HttpResponseForbidden("Access denied")

    author_map = AuthorAlias.display_map()

    # Скрытые записи (отклонённые при интерактивной синхронизации / спрятанные
    # руками) на «Обновлениях» не показываются — управление ими в UpdateLogAdmin.
    visible = UpdateLog.objects.filter(hidden=False)
    queryset = visible.all()  # копия — фильтры ниже не должны трогать visible

    # Search by author or description. Поиск расширяется справочником: «Иванов»
    # находит коммиты ника, чей display_label содержит «Иванов»; ФИО — в любом
    # порядке («Иванов Иван» = «Иван Иванов»), инициалы — по началу слов.
    search_query = request.GET.get('q', '').strip()
    if search_query:
        alias_q = flat_text_q(("nick", "full_name", "group"), search_query)
        alias_nicks = (
            AuthorAlias.objects.filter(alias_q).values_list("nick", flat=True)
            if alias_q is not None else AuthorAlias.objects.none()
        )
        text_q = flat_text_q(("description", "author"), search_query)
        if text_q is not None:
            text_q |= Q(author__in=alias_nicks)
            queryset = queryset.filter(text_q)

    # Filter by date range
    date_from = request.GET.get('date_from', '').strip()
    date_to = request.GET.get('date_to', '').strip()
    if date_from:
        queryset = queryset.filter(commit_date__gte=date_from)
    if date_to:
        queryset = queryset.filter(commit_date__lte=date_to)

    # Filter by author — value is the raw git nick, label goes via AuthorAlias
    selected_author = request.GET.get('author', '').strip()
    if selected_author:
        queryset = queryset.filter(author=selected_author)

    # Distinct authors for filter dropdown: pairs (raw nick, display label)
    # sorted by the displayed label; nicks without an alias keep the nick.
    # Строится из ВСЕХ видимых записей (не только отфильтрованных), value —
    # сырой ник, фильтр author= не меняется. Список кэшируется
    # (ai/services/catalog.py); инвалидация — сигналы UpdateLog/AuthorAlias.
    from ..services.catalog import update_authors

    authors = update_authors()

    # Paginate
    from django.core.paginator import Paginator
    paginator = Paginator(queryset, 50)
    page_number = request.GET.get('page', 1)
    page_obj = paginator.get_page(page_number)

    # Rows prepared for display: author resolved through the alias directory
    # (unknown nick falls back to the raw nick).
    rows = [
        {
            "entry": entry,
            "display_author": author_map.get(entry.author, entry.author),
        }
        for entry in page_obj.object_list
    ]

    # each_context — обязателен: без него admin/base.html не рендерит
    # ЛЕВЫЙ БАР (is_nav_sidebar_enabled приходит только отсюда) и шапку.
    context = {
        **ai_admin_site.each_context(request),
        'title': 'Обновления',
        'page_obj': page_obj,
        'rows': rows,
        'search_query': search_query,
        'date_from': date_from,
        'date_to': date_to,
        'authors': authors,
        'selected_author': selected_author,
        'opts': UpdateLog._meta,
        # total_count — из пагинатора (paginator.count): отдельный
        # queryset.count() был лишним COUNT-запросом на каждый рендер.
        'total_count': paginator.count,
    }
    return render(request, 'admin/ai/updates.html', context)