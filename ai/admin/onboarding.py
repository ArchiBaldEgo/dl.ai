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

# Пути с собственным туром страницы (первое совпадение побеждает). Туры этих
# страниц рассказывают КОНТЕНТ страницы — без хрома: топ-бар и меню расписывает
# главный экран (wizard_context_for_request), чтобы не повторяться.
_ADMIN_SCOPES_BY_PATH = (
    ("/ai/admin/arm/solve/", "admin_arm_solve"),
    ("/ai/admin/arm/models/", "admin_model_status"),
    ("/ai/admin/ai/airequestlog/", "admin_logs"),
    ("/ai/admin/pinned-runs/", "admin_pinned_runs"),
    ("/ai/admin/ai/tasksolution/", "admin_tasksolution"),
    ("/ai/admin/ai/aiappsettings/", "admin_aiappsettings"),
    ("/ai/admin/updates/", "admin_updates"),
    ("/ai/admin/prompt-defaults/", "admin_prompt_defaults"),
    ("/ai/admin/prompt-regression/", "admin_regression"),
    ("/ai/admin/test-console/", "admin_test_console"),
    ("/ai/admin/prompts/my/", "admin_my_prompt"),
)

# Тур ГЛАВНОГО ЭКРАНА админки («весь бар» + каждый раздел меню) — свой для
# каждого уровня доступа: новая роль = новый scope без отметки → покажется;
# понижение роли вернёт старый scope с отметкой → заново не показывается
# (требование: демо-понижение не приставуче).
_ROLE_TO_SCOPE = {
    "pd": "admin_pd",
    "staff": "admin_staff",
    "super": "admin_super",
}

# Путь главного экрана (точное совпадение — только индекс, не стоковые таблицы).
_DASHBOARD_PATHS = ("/ai/admin/", "/ai/admin/index")


def admin_scope_for_path(path):
    """scope тура по URL админки.

    Свой scope — у главных страниц (`_ADMIN_SCOPES_BY_PATH`, префикс);
    «dashboard» — главный экран; None — прочие пути (стоковые таблицы,
    документация, login): автостарта там нет — главный экран уже расписал
    бар и все разделы меню.
    """
    for prefix, scope in _ADMIN_SCOPES_BY_PATH:
        if path.startswith(prefix):
            return scope
    if path == "/ai/admin/" or path.rstrip("/") == "/ai/admin/index":
        return "dashboard"
    return None


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
    role_scope = _ROLE_TO_SCOPE[role]
    path_scope = admin_scope_for_path(request.path)
    if path_scope is None:
        # Стоковые таблицы/доки: без автостарта. В payload всё равно отдаём
        # ролевой scope (data-атрибут/body-фоллбэк для ручного запуска), но
        # движок по show:false молчит.
        return {
            "scope": role_scope,
            "version": WIZARD_VERSION,
            "show": False,
            "role": role,
            "mark_url": MARK_URL_ADMIN,
        }
    scope = role_scope if path_scope == "dashboard" else path_scope
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