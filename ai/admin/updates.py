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
from .site import ai_admin_site


@ai_admin_site.admin_view
def admin_updates_view(request):
    """Render the updates/commit history page (superusers only)."""
    if not request.user.is_superuser:
        return HttpResponseForbidden("Superuser access required")

    author_map = AuthorAlias.display_map()

    # Скрытые записи (отклонённые при интерактивной синхронизации / спрятанные
    # руками) на «Обновлениях» не показываются — управление ими в UpdateLogAdmin.
    visible = UpdateLog.objects.filter(hidden=False)
    queryset = visible.all()  # копия — фильтры ниже не должны трогать visible

    # Search by author or description. Поиск расширяется справочником: «Иванов»
    # находит коммиты ника, чей display_label содержит «Иванов».
    search_query = request.GET.get('q', '').strip()
    if search_query:
        aliased_nicks = (
            AuthorAlias.objects.filter(
                Q(full_name__icontains=search_query) | Q(group__icontains=search_query)
            )
            .values_list("nick", flat=True)
        )
        queryset = queryset.filter(
            Q(author__icontains=search_query) | Q(author__in=aliased_nicks)
        )

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
    # сырой ник, фильтр author= не меняется.
    authors = sorted(
        (
            (nick, author_map.get(nick, nick))
            for nick in visible.values_list('author', flat=True).distinct()
        ),
        key=lambda pair: pair[1].lower(),
    )

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

    context = {
        'title': 'Обновления',
        'page_obj': page_obj,
        'rows': rows,
        'search_query': search_query,
        'date_from': date_from,
        'date_to': date_to,
        'authors': authors,
        'selected_author': selected_author,
        'opts': UpdateLog._meta,
        'total_count': queryset.count(),
    }
    return render(request, 'admin/ai/updates.html', context)