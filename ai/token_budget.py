"""Token budget reporting for the model status page.

The total limit and issue date are stored in ``AIModelTokenBudget`` (maintained
by an admin). Spent tokens are aggregated from ``AIRequestLog.tokens`` for
requests sent on or after the budget's ``issued_at``. Per-provider attribution
is approximate: ``AIRequestLog.tokens`` is not split by provider today, so a
budget consumes the global token sum within its window. When a per-model token
field is introduced, this module is the single place to make the attribution
exact.
"""

from django.db.models import Sum

from .models import AIModelTokenBudget, AIRequestLog


def get_token_budget_rows() -> list[dict]:
    """Return one row per configured budget with spent/remaining computed."""
    budgets = AIModelTokenBudget.objects.all().order_by("label")
    rows = []
    for budget in budgets:
        spent = (
            AIRequestLog.objects.filter(sent_at__date__gte=budget.issued_at)
            .aggregate(total=Sum("tokens"))
            .get("total")
        ) or 0
        spent = int(spent)
        remaining = max(0, budget.total_limit - spent)
        rows.append(
            {
                "label": budget.label,
                "total_limit": budget.total_limit,
                "issued_at": budget.issued_at,
                "spent": spent,
                "remaining": remaining,
                "notes": budget.notes,
            }
        )
    return rows