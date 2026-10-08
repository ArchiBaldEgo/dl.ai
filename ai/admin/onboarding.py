"""Онбординг-wizard в админке: контекст для тур-подсказок + POST «просмотрено».

Аналог admin/pinned.py / guest_mode.py: тонкие view, вся логика флага —
в ai/services/onboarding.py. Контекст (ai_wizard) попадает в каждый шаблон
админки через AIAdminSite.each_context (site.py) и рендерится json_script'ом
в ai/templates/admin/base_site.html — его читает static/admin/js/ai-wizard.js.
"""

from django.http import JsonResponse
from django.views.decorators.http import require_http_methods

from ..constants import WIZARD_VERSION
from ..services.onboarding import (
    MARK_URL_ADMIN,
    record_wizard_seen,
    scope_from_request_body,
    should_show_wizard,
)
from .guest_mode import is_viewing_as_guest
from .permissions import can_access_admin, is_staff_or_superuser, is_superuser_user

# Пути «больших страниц» с собственным туром (первое совпадение побеждает).
# Большие страницы — общие туры без деления по правам: ролевые различия там
# режутся отсутствием недоступных элементов (#armRecordStats и т.п.).
_ADMIN_SCOPES_BY_PATH = (
    ("/ai/admin/arm/solve/", "admin_arm_solve"),
    ("/ai/admin/arm/models/", "admin_model_status"),
)

# Общий тур админки — свой для каждого уровня доступа: новая роль = новый
# scope без отметки → покажется; понижение роли вернёт старый scope с
# отметкой → заново не показывается (требование: демо-понижение не приставуче).
_ROLE_TO_SCOPE = {
    "pd": "admin_pd",
    "staff": "admin_staff",
    "super": "admin_super",
}


def admin_scope_for_path(path):
    """scope тура по URL админки: большие страницы — свой, остальное — «admin»."""
    for prefix, scope in _ADMIN_SCOPES_BY_PATH:
        if path.startswith(prefix):
            return scope
    return "admin"


def _admin_role_label(user):
    """Роль для шагов тура: "super" / "staff" / "pd"."""
    if is_superuser_user(user):
        return "super"
    if is_staff_or_superuser(user):
        return "staff"
    return "pd"


def wizard_context_for_request(request):
    """Конфиг тура для each_context: решение о показе принимает сервис.

    Гостевой режим («зайти как гость») тур не показывает и не отмечает —
    это предпросмотр глазами разработчика промптов, состояние суперюзера
    двигать нельзя.
    """
    user = request.user
    role = _admin_role_label(user)
    path_scope = admin_scope_for_path(request.path)
    scope = _ROLE_TO_SCOPE[role] if path_scope == "admin" else path_scope
    return {
        "scope": scope,
        "version": WIZARD_VERSION,
        "show": not is_viewing_as_guest(request) and should_show_wizard(user, scope),
        "role": role,
        "mark_url": MARK_URL_ADMIN,
    }


@require_http_methods(["POST"])
def admin_wizard_seen_view(request):
    """POST /ai/admin/wizard/seen/ — отметка «тур закрыт» (обёрнут admin_view)."""
    scope = scope_from_request_body(request.body)
    if scope is None:
        return JsonResponse({"ok": False, "error": "bad_scope"}, status=400)
    # Гостевой режим: собственное состояние суперюзера не двигаем.
    if is_viewing_as_guest(request):
        return JsonResponse({"ok": True})
    if not can_access_admin(request.user):
        return JsonResponse({"ok": False, "error": "Access denied"}, status=403)
    record_wizard_seen(request.user, scope)
    return JsonResponse({"ok": True})