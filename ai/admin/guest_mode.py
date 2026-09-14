"""Гостевой режим админки («Зайти как гость»).

Только для суперюзера: флаг в сессии понижает ВИД интерфейса до уровня
разработчика промптов — навигация, дашборд и роль в шапке. Права при этом
не режутся: прямые URL суперюзерских страниц продолжают работать (тот же
контракт, что у _HIDDEN_NAV_OBJECT_NAMES). Повторное нажатие возвращает
полную админку.
"""

from django.contrib import messages
from django.http import HttpResponseForbidden, HttpResponseRedirect

from .permissions import is_superuser_user
from .site import ai_admin_site

# Ключ сессии с флагом гостевого режима (аналог admin_fresh_auth).
SESSION_KEY = "ai_view_as_guest"


def is_viewing_as_guest(request) -> bool:
    """Визуальный гостевой режим включён (и включать его может только суперюзер)."""
    return bool(getattr(request.user, "is_superuser", False)) and bool(
        request.session.get(SESSION_KEY)
    )


@ai_admin_site.admin_view
def admin_toggle_guest_view(request):
    """POST-переключатель гостевого режима (кнопка в шапке админки)."""
    if request.method != "POST":
        return HttpResponseForbidden("Метод не поддерживается")
    if not is_superuser_user(request.user):
        return HttpResponseForbidden("Только для суперпользователя")
    was_guest = is_viewing_as_guest(request)
    if was_guest:
        request.session.pop(SESSION_KEY, None)
        messages.success(request, "Гостевой режим выключен — полная админка восстановлена.")
    else:
        request.session[SESSION_KEY] = True
        messages.success(
            request,
            "Включён режим «Как гость»: интерфейс как у разработчика промптов. "
            "Повторное нажатие кнопки вернёт полную админку.",
        )
    return HttpResponseRedirect("/ai/admin/")