"""Admin view for the "Обновления" (Updates) section.

Shows the commit history stored in UpdateLog with filtering and search
by author and date. Only superusers can access this page.
"""

from django.shortcuts import render
from django.http import HttpResponseForbidden
from django.db.models import Q

from ..models import UpdateLog
from .site import ai_admin_site


@ai_admin_site.admin_view
def admin_updates_view(request):
    """Render the updates/commit history page (superusers only)."""
    if not request.user.is_superuser:
        return HttpResponseForbidden("Superuser access required")

    queryset = UpdateLog.objects.all()

    # Search by author or description
    search_query = request.GET.get('q', '').strip()
    if search_query:
        queryset = queryset.filter(
            Q(author__icontains=search_query) | Q(description__icontains=search_query)
        )

    # Filter by date range
    date_from = request.GET.get('date_from', '').strip()
    date_to = request.GET.get('date_to', '').strip()
    if date_from:
        queryset = queryset.filter(commit_date__gte=date_from)
    if date_to:
        queryset = queryset.filter(commit_date__lte=date_to)

    # Distinct authors for filter dropdown
    authors = (
        UpdateLog.objects.values_list('author', flat=True)
        .distinct()
        .order_by('author')
    )
    selected_author = request.GET.get('author', '').strip()
    if selected_author:
        queryset = queryset.filter(author=selected_author)

    # Paginate
    from django.core.paginator import Paginator
    paginator = Paginator(queryset, 50)
    page_number = request.GET.get('page', 1)
    page_obj = paginator.get_page(page_number)

    context = {
        'title': 'Обновления',
        'page_obj': page_obj,
        'search_query': search_query,
        'date_from': date_from,
        'date_to': date_to,
        'authors': authors,
        'selected_author': selected_author,
        'opts': UpdateLog._meta,
        'total_count': queryset.count(),
    }
    return render(request, 'admin/ai/updates.html', context)