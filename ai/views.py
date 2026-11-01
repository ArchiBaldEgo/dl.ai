"""Представления (views) AI-приложения.

Содержит:
- Страницы чата, решения задач и поиска ошибок (chat_view, decide_task_view, find_error_view).
- API-эндпоинты для получения языков, тем, промптов, данных задач dl.gsu.by.
- Прокси-эндпоинты к DL API (получение задач, отправка решений, проверка результатов).
- Транскрипцию аудио через Google Speech Recognition.
- Установку пароля для внешних пользователей (set_password_view).
- Проверку доступа (prompt_developer_access_required, _has_page_access).
"""

import json
import os
import hashlib
import io
import mimetypes
import logging
import threading
import time
import asyncio

import speech_recognition as sr
import tempfile
from django.contrib.auth import login
from django.shortcuts import redirect, render
from django.http import JsonResponse, HttpResponse
from django.http import FileResponse, Http404, HttpResponseForbidden, HttpResponseNotFound, HttpResponseNotModified
from django.db import ProgrammingError, models
from django.db.models import Q
from django.contrib.staticfiles import finders
from django.utils.html import strip_tags
from django.utils.http import http_date
from django.utils import timezone
from django.views.static import was_modified_since
from django.middleware import csrf
from functools import wraps

from django.views.decorators.http import require_http_methods  
from django.conf import settings
from .throttling import rate_limited
from .model_health import get_available_model_options, trigger_model_health_refresh_async
from .model_clients import registry
from .model_clients.registry import DEFAULT_MODEL_KEY, EXPERIMENTAL_MODEL_PREFIXES
from .models import (
    AIUserModel, AIAppSettings, ExternalDLAccount, ProgrammingLanguage,
    Prompt, SharedPrompt, Topic,
)
from .auth_backends import (
    ADMIN_EXTERNAL_AUTH_BACKEND,
    create_admin_user_with_password,
    get_admin_user_by_external_id,
    get_external_user_id_from_request,
    normalize_external_user_id,
)
from .admin.permissions import (
    add_user_to_builtin_ai_group,
    is_prompt_developer_user,
    is_superuser_user,
)
from .middleware import _dev_bypass_enabled
from .services.auth import ai_disabled_reason, can_use_ai, get_user_identity_for_log
from .services.docs import DocUnavailableError, read_chapter_markdown, render_chapter_html
from .services.user_models import (
    PROVIDER_BASE_URLS,
    admin_user_model_rows,
    delete_user_model,
    public_user_model_options,
    save_user_model,
    set_user_model_access,
    shared_user_model_options,
    user_model_options,
    user_model_rows,
)
from .services.user_directory import search_users_for_sharing
from .services.onboarding import (
    MARK_URL_USER,
    record_wizard_seen,
    scope_from_request_body,
    user_basics_seen,
    wizard_boot_payload,
)
from .dl_api_client import (
    DLApiUnavailable,
    DLForbiddenError,
    DLServerError,
    DLTaskNotFoundError,
    DLUnauthorizedError,
    dl_error_response,
    fetch_task_info,
    fetch_task_solution,
)
from .http_utils import resolve_dl_session_id, safe_relative_url
from .i18n import (
    client_strings,
    get_language_instruction,
    get_localized_name,
    get_localized_text,
    get_ui_language_suffix,
    user_models_strings,
    user_models_strings_all,
)
from .serializers import (
    programming_language as serialize_programming_language,
    prompt as serialize_prompt,
    shared_prompt as serialize_shared_prompt,
    shared_prompt_with_dates as serialize_shared_prompt_with_dates,
    topic as serialize_topic,
)


def _get_user_top_model_keys(request, limit=1):
    """Возвращает list of model_key для топ-N моделей пользователя по частоте использования.
    Считает по AIRequestLog (только success). Сопоставляет title из логов с key из registry.

    Результат кешируется per-user на 120 c (ключ ``ai:user_top:{user_id}:{limit}``),
    чтобы не делать per-render scan AIRequestLog, добавленный коммитом ed4d528.
    Топ-1 пользователя меняется редко; устаревание на 2 минуты незаметно, а перебор
    логов на каждом рендере стартовой страницы был главным регрессором скорости.
    На cache miss перебираются только последние 200 успешных запросов (вместо всей
    истории) — для активных пользователей это убирает скан тысяч строк.
    """
    try:
        from .models import AIRequestLog
        from .model_clients import registry
        from .constants import AI_CACHE_KEY_PREFIX
        from collections import Counter
        from django.core.cache import cache

        # Получить user_id
        user = getattr(request, 'user', None)
        user_id = getattr(user, 'pk', None) if user and not isinstance(user, str) else None
        if not user_id:
            return []

        # Кеш per-user. Пустой результат тоже кешируется (пользователь без логов).
        cache_key = f"{AI_CACHE_KEY_PREFIX}:user_top:{user_id}:{limit}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        # Построить map title → key (через публичный API реестра)
        title_to_key = registry.title_to_key()

        # Срез по epoch: если у AIAppSettings задан favorites_epoch, учитываем
        # только успешные запросы новее этой даты. Это даёт разовый сброс счётчика
        # фаворитов для всех (нет записей новее epoch → user_top пуст → строгий
        # алфавит), без удаления логов; новые запросы снова набирают топ-1.
        qs = AIRequestLog.objects.filter(status='success', user_id=user_id)
        try:
            epoch = AIAppSettings.get_solo().favorites_epoch
        except Exception:
            epoch = None
        if epoch:
            qs = qs.filter(sent_at__gte=epoch)

        # Посчитать частоту моделей для этого пользователя по последним 200 логам.
        # В model_names пишутся КЛЮЧИ реестра (consumers.py), так что считаем их
        # напрямую; title_to_key — фолбэк для записей времён, когда туда попадали
        # title'ы. Experimental-модели (EXPERIMENTAL_MODEL_PREFIXES) не поднимаем
        # фаворитами — они всегда в самом низу селектора отдельной группой.
        from .model_clients.registry import EXPERIMENTAL_MODEL_PREFIXES
        c = Counter()
        for mns in qs.order_by('-sent_at').values_list('model_names', flat=True)[:200]:
            if mns:
                for m in mns:
                    key = m if registry.get(m) else title_to_key.get(m)
                    if key and not key.startswith(EXPERIMENTAL_MODEL_PREFIXES):
                        c[key] += 1

        result = [key for key, _ in c.most_common(limit)]
        cache.set(cache_key, result, 120)  # 2 минуты
        return result
    except Exception:
        return []


logger = logging.getLogger(__name__)


def health_view(request):
    """Простой health-check эндпоинт: возвращает {"ok": true}."""
    return JsonResponse({"ok": True})


_groq_probe_lock = threading.Lock()
_groq_last_probe = 0.0


def _groq_enabled() -> bool:
    """True, если провайдер Groq включён через ``AI_ENABLE_GROQ``."""
    return getattr(settings, "AI_ENABLE_GROQ", False)


def _run_groq_probe():
    """Фоновый прогрев rate-limit кэша Groq (неблокирующий для HTTP-запроса)."""
    if not _groq_enabled():
        return
    try:
        from .model_clients.groq import probe_rate_limits
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(probe_rate_limits())
        finally:
            loop.close()
    except Exception:
        logger.exception("Background Groq rate-limit probe failed")


def _maybe_kick_groq_probe():
    """Запускает фоновый probe не чаще раза в 60с (дебаунс)."""
    if not _groq_enabled():
        return
    global _groq_last_probe
    with _groq_probe_lock:
        if time.time() - _groq_last_probe < 60:
            return
        _groq_last_probe = time.time()
    threading.Thread(target=_run_groq_probe, daemon=True).start()


@require_http_methods(["GET"])
def get_groq_limits_view(request):
    """API: возвращает текущие rate-limit-ы по всем Groq моделям.

    Данные берутся из in-memory кэша в ai.model_clients.groq._rate_limit_cache,
    который обновляется при каждом запросе к Groq API (см. _update_rate_limit_cache).

    Если в кэше есть данные — отдаёт их сразу. Если данных ещё нет (контейнер только
    запущен) — запускает НЕблокирующий фоновый probe (probe_rate_limits) и возвращает
    текущий (возможно пустой) кэш, чтобы фронтенд показал «загрузка…» и повторил через
    минуту (initModelLimitsWidget опрашивает раз в 60с). Дебаунс 60с предотвращает
    спам probe-ами при параллельных запросах.

    Когда ``AI_ENABLE_GROQ`` выключен (по умолчанию), всегда возвращает
    ``{"limits": {}}`` и не запускает probe — отключённый провайдер не трогается.
    """
    if not _groq_enabled():
        return JsonResponse({"limits": {}})

    from .model_clients.groq import get_rate_limits

    cache = get_rate_limits()
    if not cache:
        _maybe_kick_groq_probe()

    return JsonResponse({"limits": cache})


def prompt_developer_access_required(view_func):
    """Декоратор: суперпользователь ИЛИ member встроенной группы «Разработчик промптов»."""
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        user = getattr(request, "user", None)
        if not user or not user.is_authenticated:
            return HttpResponseForbidden("Prompt developer access required")
        if is_superuser_user(user) or is_prompt_developer_user(user):
            return view_func(request, *args, **kwargs)
        return HttpResponseForbidden("Prompt developer access required")

    return _wrapped


def _is_ai_app_enabled():
    """Проверяет, включено ли AI-приложение (через AIAppSettings). Возвращает True при ошибке БД."""
    try:
        return AIAppSettings.get_solo().is_enabled
    except ProgrammingError:
        return True


def _has_page_access(request):
    """Require a verified external user id and an active Django session.

    The external user id can come from any of:
    * ``request.user_info`` (filled in by ``ExternalAuthMiddleware`` after
      a successful DLSID lookup against ``EXTERNAL_AUTH_API_URL``),
    * the ``uid`` / ``userId`` query parameter (set by the dl.gsu.by
      toolbar links), or
    * one of the recognized cookies (``userId`` / ``user_id`` / ``userid`` /
      ``DLID``).

    If no external id is present on the request the user has not signed
    in on the main site yet, so we refuse access. We do NOT verify that
    the local Django username matches the external id — dl.gsu.by is
    the source of truth, the local session just follows whatever cookie
    the upstream reverse-proxy forwards.
    """
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return False
    if getattr(user, "is_active", True) is False:
        return False
    # Dev-bypass (DEBUG-only): /ai/assets/ и /health пропускаются middleware
    # как skip-пути, поэтому request.user_info там никогда не заполняется,
    # а кук userId/DLID (фолбэк ниже) вне dl.gsu.by не существует — локальный
    # запрос с валидной сессией получал 403. В prod путь недостижим:
    # _dev_bypass_enabled() обязан DEBUG.
    if _dev_bypass_enabled():
        return True
    return bool(get_external_user_id_from_request(request))


def _access_denied_reason(request) -> str:
    """Почему доступ к приложению закрыт (для 403-ответов и страниц).

    Пользователь должен видеть ПРИЧИНУ отказа, а не безликое «Authentication
    required»: чаще всего это отсутствие входа через dl.gsu.by (браузер без
    DLSID-куки), отключённая учётная запись или неподтверждённый внешний id.
    """
    user = getattr(request, "user", None)
    if not user or not getattr(user, "is_authenticated", False):
        return (
            "Доступ закрыт: нет активной сессии. Войдите на dl.gsu.by и "
            "откройте страницу DLAI заново."
        )
    if getattr(user, "is_active", True) is False:
        return "Доступ закрыт: учётная запись отключена администратором."
    return (
        "Доступ закрыт: не удалось подтвердить пользователя dl.gsu.by "
        "(браузер не передал DLSID). Войдите заново на dl.gsu.by."
    )


def _deny_access(request, *, as_json: bool = True):
    """403 с причиной: JSON для API-эндпоинтов, текст для страниц.

    ``Cache-Control: no-store`` — отказ не должен закэшироваться браузером:
    иначе после повторного входа страница/API ещё долго получает старый 403.
    """
    reason = _access_denied_reason(request)
    if as_json:
        response = JsonResponse({"error": reason}, status=403)
    else:
        response = HttpResponseForbidden(reason)
    response["Cache-Control"] = "no-store"
    return response


def _get_last_update_date():
    """Return the commit_date of the most recent UpdateLog entry, or None.

    Cached for 5 minutes to avoid a DB hit on every page render.
    """
    from django.core.cache import cache
    cache_key = "ai_last_update_date"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached if cached else None
    try:
        from .models import UpdateLog
        entry = UpdateLog.objects.first()
        result = entry.commit_date if entry else None
        cache.set(cache_key, result or "", 300)  # 5 minutes
        return result
    except Exception:
        return None


def _read_ai_state(request):
    """Parse the ``ai_state`` cookie (user selections) into a dict, or ``{}``.

    The cookie is written by the frontend (``static/admin/js/ai-common.js``) and
    carries the last active tab, interface language and selected model/topic/
    prompt so the server can pre-render the right language and redirect to the
    last tab (see :func:`ai_root_view`). Malformed cookies are ignored.
    """
    import json
    raw = request.COOKIES.get("ai_state") or ""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


def _ai_disabled_reason(request):
    """Причина, по которой AI пользователю недоступен (или ``None``).

    Тонкая обёртка над общим хелпером ``ai_disabled_reason``
    (ai/services/auth.py) — тот же текст отказа на странице, в WS и в
    HTTP-эндпоинтах отправки решения.
    """
    return ai_disabled_reason(request)


def _render_ai_page(request, template_name, extra_context=None, wizard_scope=None):
    """Рендерит страницу AI (chat/solve/find-error) с общим контекстом.

    Проверяет доступ, загружает список доступных моделей (с self-heal при пустом
    списке), сортирует их (топ-1 фаворит пользователя → модель по умолчанию →
    алфавит) и разбивает на группы favorite_models/other_models для
    optgroup-разделителя в шаблоне, и добавляет external_session_id +
    сохранённый из куки язык.

    wizard_scope — scope онбординг-wizard для этой страницы (ai/services/
    onboarding.py); с ним в контекст попадает конфиг тур-подсказок
    (ai_wizard → json_script в base_chat.html).
    """
    if not _has_page_access(request):
        return _deny_access(request, as_json=False)
    if not _is_ai_app_enabled():
        return HttpResponseNotFound("AI app is disabled")
    # Гейт DL «canUseAi»: AI в текущем курсе запрещён → честная страница
    # с объяснением (не молчаливая пустая страница).
    disabled_reason = _ai_disabled_reason(request)
    if disabled_reason:
        response = HttpResponseForbidden(disabled_reason)
        # Отказ не должен закэшироваться: иначе после снятия ограничения
        # страница ещё долго отдаёт старый 403 (как в _deny_access).
        response["Cache-Control"] = "no-store"
        return response
    available_models = get_available_model_options()
    # Self-heal: an empty list means the current 04:00 MSK window was scanned
    # while the upstream key/balance was broken and every model got marked
    # down. Fixing the key does not auto re-check (the scheduler only runs at
    # 04:00 MSK and force=False bails on a COMPLETED window), so without this
    # the chat page stays empty until the next daily window or a manual admin
    # refresh. Kick a non-blocking forced sweep; its in-process + DB guards
    # prevent concurrent runs, so repeated page loads don't stack sweeps.
    if not available_models:
        try:
            trigger_model_health_refresh_async()
        except Exception:
            logger.exception("Failed to trigger model health self-heal refresh")
    if available_models:
        # Топ-1 фаворит пользователя (по частоте использования) — наверх.
        user_top = _get_user_top_model_keys(request, limit=1)
        model_map = {item["key"]: item for item in available_models}
        # Experimental-модели не становятся фаворитом даже если их чаще всего
        # вызывали: они всегда в самом низу селектора (отдельная optgroup).
        user_picks = [
            model_map[k] for k in user_top
            if k in model_map and not k.startswith(EXPERIMENTAL_MODEL_PREFIXES)
        ]

        # Остальные — строго по алфавиту (title). Без отдельного приоритета
        # Web_DeepSeek: фаворит наверху, далее алфавит (включая Web_DeepSeek).
        # used_keys — только РЕАЛЬНО поднятые фавориты (без OR-исключённых,
        # иначе OR-модель выпадала бы из «Все модели»).
        used_keys = {m["key"] for m in user_picks}
        # Модель по умолчанию — первое место среди «остальных», только когда у
        # пользователя нет своего топ-1 фаворита (шаблон ставит ей selected).
        default_model_key = ""
        if not user_picks and DEFAULT_MODEL_KEY in model_map:
            default_model_key = DEFAULT_MODEL_KEY
            used_keys.add(default_model_key)
        rest = sorted(
            [item for item in available_models if item["key"] not in used_keys],
            key=lambda x: x["title"].lower(),
        )
        if default_model_key:
            rest.insert(0, model_map[default_model_key])

        # Experimental-модели (OpenRouter, registry.EXPERIMENTAL_MODEL_PREFIXES) —
        # отдельная optgroup В САМОМ НИЗУ селектора: их не поднимают ни фаворит,
        # ни алфавит, ни дефолт.
        experimental_models = [
            item for item in rest if item["key"].startswith(EXPERIMENTAL_MODEL_PREFIXES)
        ]
        rest = [item for item in rest if not item["key"].startswith(EXPERIMENTAL_MODEL_PREFIXES)]

        # Плоский список для обратной совместимости (тесты проверяют порядок).
        available_models = user_picks + rest + experimental_models
        # Группы для optgroup-разделителя в шаблоне: топ-1 фаворит + остальные
        # (rest уже отсортирован и содержит дефолт первым, если тот применён).
        favorite_models = user_picks[:1]
        other_models = rest
    else:
        favorite_models = []
        other_models = []
        experimental_models = []
        default_model_key = ""
    saved_state = _read_ai_state(request)
    external_session_id = request.session.get('external_session_id')
    # Личные модели: три optgroup внизу селектора — свои («Мои модели»),
    # адресно выданные («Доступные мне») и публичные. Приватные чужие модели
    # не попадают ни в одну группу (ai/services/user_models.py).
    user_models_options = user_model_options(request.user)
    shared_user_models = shared_user_model_options(request.user)
    public_user_models = public_user_model_options(request.user)
    # Клиентские строки трёхъязычного UI (ai/i18n.client_strings): язык — тот
    # же, что у #selectLang (ai_state cookie, «Русский» по умолчанию). JS
    # перелокализует DOM при каждой смене языка сам; серверный словарь нужен
    # первому рендеру и строкам, которые JS-апплаеры не трогают.
    context = {
        'available_models': available_models,
        'favorite_models': favorite_models,
        'other_models': other_models,
        'experimental_models': experimental_models,
        'user_models_options': user_models_options,
        'shared_user_models': shared_user_models,
        'public_user_models': public_user_models,
        'default_model_key': default_model_key,
        'external_session_id': external_session_id,
        'last_update_date': _get_last_update_date(),
        'saved_lang': saved_state.get('lang') or '',
        't': client_strings(saved_state.get('lang') or ''),
    }
    if wizard_scope:
        # Онбординг-wizard этой страницы: решение «показывать ли» принимает
        # сервис по флагу в БД (ai/services/onboarding.py), шаблон рендерит его
        # через json_script — ai-wizard.js конфиг читает с DOM. Роль отдаётся
        # движку для ролевых шагов (super/staff видят шаг про админ-панель),
        # basics_seen — были ли уже показаны «общие» шаги в туре другой страницы
        # (режут они только common-шаги solve/find_error; чат всегда полный).
        user = request.user
        if getattr(user, "is_superuser", False):
            role = "super"
        elif getattr(user, "is_staff", False):
            role = "staff"
        else:
            role = ""
        context["ai_wizard"] = wizard_boot_payload(
            user, wizard_scope, mark_url=MARK_URL_USER,
            role=role, basics_seen=user_basics_seen(user, wizard_scope),
        )
    if extra_context:
        context.update(extra_context)
    response = render(request, template_name, context)
    # HTML не кэшируется — браузер всегда видит свежий ?v= ассетов и подгружает
    # обновлённый JS/CSS после деплоя (ассеты ревалидируются через asset_view).
    response["Cache-Control"] = "no-cache"
    return response


def ai_root_view(request):
    """``/ai/`` — redirect to the user's last active tab from the ``ai_state`` cookie.

    Falls back to the chat page when the cookie is absent or holds an unknown
    tab, so the entry point always lands somewhere useful.
    """
    from django.shortcuts import redirect

    if not _has_page_access(request):
        return _deny_access(request, as_json=False)
    tab_map = {
        "solve": "/ai/solve-problem/",
        "find_error": "/ai/find-error/",
    }
    saved = _read_ai_state(request)
    tab = saved.get("tab") or ""
    target = tab_map.get(tab, "/ai/chat/")
    return redirect(target)


@rate_limited
@require_http_methods(["GET", "POST"])
def set_password_view(request):
    """Allow an externally authenticated admin user to set a first password."""
    next_url = safe_relative_url(
        request.POST.get("next") if request.method == "POST" else request.GET.get("next"),
        "/ai/admin/",
    )
    error_message = ""
    subtitle = "Для входа в админку введите пароль. Это однократная регистрация."
    target_user = None

    # Only accept an external id that has been validated by the external auth
    # middleware. Query-parameter uid/userId is not trustworthy on its own.
    external_user_id = get_external_user_id_from_request(request)
    if not external_user_id:
        # Fall back to the explicit hidden form field, but only when the
        # request already has a user_info from the DLSID flow.
        user_info = getattr(request, "user_info", None) or {}
        candidate = normalize_external_user_id(
            request.POST.get("external_user_id")
            or request.GET.get("uid")
            or request.GET.get("userId")
        )
        if candidate and candidate == normalize_external_user_id(user_info.get("userId")):
            external_user_id = candidate

    is_admin_registration = request.path.startswith("/ai/admin/set-password/") and bool(external_user_id)
    if is_admin_registration:
        target_user = get_admin_user_by_external_id(external_user_id)

    if request.method == "POST":
        new_password = request.POST.get("new_password") or ""
        new_password_confirm = request.POST.get("new_password_confirm") or ""

        if is_admin_registration and target_user and target_user.has_usable_password():
            # Вход по уже установленному паролю: группы НЕ перевыдаём —
            # состав правит суперюзер на «Группах доступа» (иначе отзыв
            # членства откатывался бы каждым логином).
            login(request, target_user, backend=ADMIN_EXTERNAL_AUTH_BACKEND)
            csrf.rotate_token(request)
            request.session["admin_fresh_auth"] = True
            return redirect(next_url)

        if not is_admin_registration:
            error_message = "Установка пароля вне админки больше не поддерживается."
        elif is_admin_registration and target_user and getattr(target_user, "is_active", True) is False:
            error_message = "Учётная запись заблокирована."
        elif is_admin_registration and target_user and target_user.has_usable_password():
            error_message = "Пользователь уже имеет пароль. Используйте обычный вход."
        elif new_password != new_password_confirm:
            error_message = "Пароли не совпадают."
        elif len(new_password) < 8:
            error_message = "Пароль должен быть не менее 8 символов."
        else:
            if target_user:
                target_user.set_password(new_password)
                target_user.save(update_fields=["password"])
                # Первичная установка пароля = зачисление в baseline-группу
                # «Разработчик промптов» (однократно, потом «липко»).
                add_user_to_builtin_ai_group(target_user)
            else:
                target_user = create_admin_user_with_password(external_user_id, new_password)
                add_user_to_builtin_ai_group(target_user)
            login(request, target_user, backend=ADMIN_EXTERNAL_AUTH_BACKEND)
            csrf.rotate_token(request)
            request.session["admin_fresh_auth"] = True
            return redirect(next_url)

        response = render(
            request,
            "ai/set-password.html",
            {
                "error_message": error_message,
                "next_url": next_url,
                "username": target_user.username if target_user else external_user_id,
                "subtitle": subtitle,
            },
        )
        response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response["Pragma"] = "no-cache"
        return response

    if is_admin_registration and target_user and target_user.has_usable_password():
        # Вход по уже установленному паролю: группы НЕ перевыдаём —
        # отзыв членства суперюзером не должен откатываться логином.
        login(request, target_user, backend=ADMIN_EXTERNAL_AUTH_BACKEND)
        csrf.rotate_token(request)
        request.session["admin_fresh_auth"] = True
        return redirect(next_url)

    response = render(
        request,
        "ai/set-password.html",
        {
            "error_message": error_message,
            "next_url": next_url,
            "username": target_user.username if target_user else external_user_id,
            "subtitle": subtitle,
        },
    )
    response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response["Pragma"] = "no-cache"
    return response


def chat_view(request):
    """Страница чата с AI-моделью.

    Принимает nid из DL-ссылки (кнопка «Чат с DLAI» на странице задачи:
    /ai/chat/?uid=…&nid=…) — тогда текст задачи подставляется в поле ввода
    (как на «Реши задачу»).
    """
    return _render_ai_page(
        request,
        'ai/chat.html',
        extra_context={"node_id": request.GET.get("nid", "")},
        wizard_scope="chat",
    )


def decide_task_view(request):
    """Страница решения задачи: принимает node_id из query-параметров."""
    return _render_ai_page(
        request,
        'ai/decide-task.html',
        extra_context={
            "node_id": request.GET.get("nid", ""),
        },
        wizard_scope="solve",
    )


def find_error_view(request):
    """Страница поиска ошибок в коде с помощью AI.

    Принимает nid из DL-ссылки (кнопка «В чём ошибка?» на странице задачи:
    /ai/find-error/?uid=…&nid=…) — раньше параметр игнорировался вовсе и
    условие задачи никогда не подставлялось в поле.
    """
    return _render_ai_page(
        request,
        'ai/find-error.html',
        extra_context={"node_id": request.GET.get("nid", "")},
        wizard_scope="find_error",
    )


# === «Мои AI-модели» — клиентская страница (/ai/my-models/) ================
#
# Личные модели пользователя (провайдер + модель + свой ключ) нужны не
# только сотрудникам админки: модель должна быть доступна ВСЕМ, кто
# пользуется чатом. Поэтому страница есть с двух сторон:
# * /ai/my-models/ — клиентская (обычный аутентифицированный пользователь,
#   гейт как у страниц чата: _has_page_access);
# * /ai/admin/user-models/ — админская (персонал, ai/admin/user_models.py).
# Логика операций — одна, в СЕРВИСЕ (ai/services/user_models.py): вьюхи
# только гейтят доступ и отдают шаблон (DRY).


@require_http_methods(["GET"])
def my_models_view(request):
    """Клиентская страница «Мои AI-модели»: список + форма добавления."""
    if not _has_page_access(request):
        return _deny_access(request, as_json=False)
    if not _is_ai_app_enabled():
        return HttpResponseNotFound("AI app is disabled")
    ui_lang = _read_ai_state(request).get("lang") or ""
    # Строки таблицы панели — ``user_models``; селектор моделей страницы —
    # ``user_models_options`` (раньше оба звались ``user_models``, из-за чего
    # optgroup рендерился с пустыми value — строки таблицы не опции селектора).
    return render(
        request,
        "ai/my_models.html",
        {
            "user_models": user_model_rows(request.user),
            "user_models_options": user_model_options(request.user),
            "shared_user_models": shared_user_model_options(request.user),
            "public_user_models": public_user_model_options(request.user),
            # Таблица «Все модели» — только реальному суперюзеру («Посмотреть
            # как…» её не показывает: эмуляция честная).
            "all_models": admin_user_model_rows() if is_superuser_user(request.user) else None,
            "provider_choices": AIUserModel.PROVIDER_CHOICES,
            "provider_base_urls": PROVIDER_BASE_URLS,
            # base_chat.html (шапка/селектор) читает t.* из client_strings,
            # панель — из user_models_strings; панельные ключи не конфликтуют
            # с клиентскими (title/intro панели в шапке не используются).
            "t": {**client_strings(ui_lang), **user_models_strings(ui_lang)},
            # Все языки панели — для переключения языка на клиенте
            # (JS-апплаер перерисовывает подписи при смене #selectLang).
            "um_i18n": user_models_strings_all(),
        },
    )


@require_http_methods(["POST"])
def my_model_save_view(request):
    """Создать/обновить СВОЮ личную модель (клиентская страница)."""
    if not _has_page_access(request):
        return _deny_access(request, as_json=True)
    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse({"ok": False, "message": "Некорректный запрос"}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({"ok": False, "message": "Некорректный запрос"}, status=400)
    ok, message, model_pk = save_user_model(request.user, payload)
    if not ok:
        status = 404 if message == "Модель не найдена" else 400
        return JsonResponse({"ok": False, "message": message}, status=status)
    return JsonResponse({"ok": True, "id": model_pk})


@require_http_methods(["POST"])
def my_model_delete_view(request):
    """Удалить СВОЮ личную модель (клиентская страница)."""
    if not _has_page_access(request):
        return _deny_access(request, as_json=True)
    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse({"ok": False, "message": "Некорректный запрос"}, status=400)
    ok, message = delete_user_model(request.user, payload.get("id"))
    if not ok:
        status = 404 if message == "Модель не найдена" else 400
        return JsonResponse({"ok": False, "message": message}, status=status)
    return JsonResponse({"ok": True})


@require_http_methods(["POST"])
def my_model_access_view(request):
    """Изменить доступ к модели (клиентская страница): владелец/суперюзер.

    Тело — ``{id, is_public, shared_with: [id…]}``. Меняются ТОЛЬКО эти два
    поля: конфигурация и ключи модели этой операцией не трогаются.
    """
    if not _has_page_access(request):
        return _deny_access(request, as_json=True)
    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse({"ok": False, "message": "Некорректный запрос"}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({"ok": False, "message": "Некорректный запрос"}, status=400)
    ok, message = set_user_model_access(
        request.user, payload.get("id"), payload.get("is_public"),
        payload.get("shared_with"),
    )
    if not ok:
        status = 404 if message == "Модель не найдена" else 400
        return JsonResponse({"ok": False, "message": message}, status=status)
    return JsonResponse({"ok": True})


@require_http_methods(["GET"])
def my_model_search_users_view(request):
    """Поиск получателей доступа по DL-id (точно) или ФИО/логину.

    Ответ — только ``{id, display, dl_id}``: без email, групп и прав.
    """
    if not _has_page_access(request):
        return _deny_access(request, as_json=True)
    users = search_users_for_sharing(request.GET.get("q", ""), request.user)
    return JsonResponse({"ok": True, "users": users})


@require_http_methods(["POST"])
def wizard_seen_view(request):
    """Отметка «онбординг-wizard закрыт» с клиентских страниц (ai-wizard.js).

    Любое закрытие тура (Готово/Скрыть/Escape) считается «просмотрено» —
    тур больше не надоедает. Идемпотентный: повторный POST не создаёт
    дубликат. Без @rate_limited: RateLimitMiddleware уже покрывает /ai/.
    """
    if not _has_page_access(request):
        return _deny_access(request, as_json=True)
    scope = scope_from_request_body(request.body)
    if scope is None:
        return JsonResponse({"ok": False, "error": "bad_scope"}, status=400)
    record_wizard_seen(request.user, scope)
    return JsonResponse({"ok": True})


def get_languages(request):
    """API: возвращает список всех языков программирования."""
    if not _has_page_access(request):
        return _deny_access(request, as_json=True)
    languages = [
        serialize_programming_language(lang)
        for lang in ProgrammingLanguage.objects.order_by('language_name')
    ]
    return JsonResponse(languages, safe=False)


def get_topics(request):
    """API: возвращает список всех тем с локализованными названиями."""
    if not _has_page_access(request):
        return _deny_access(request, as_json=True)
    ui_language = request.GET.get('ui_language', 'Русский')
    topics = [
        serialize_topic(topic, ui_language)
        for topic in Topic.objects.select_related("programming_language").order_by('topic_name_ru')
    ]
    return JsonResponse(topics, safe=False)


def get_prompts(request):
    """API: возвращает список промптов, доступных текущему пользователю."""
    if not _has_page_access(request):
        return _deny_access(request, as_json=True)

    ui_language = request.GET.get('ui_language', 'Русский')
    prompts = [
        serialize_prompt(p, ui_language)
        for p in Prompt.objects.select_related("topic", "topic__programming_language", "programming_language", "owner", "shared_prompt").order_by('prompt_name_ru', 'id')
    ]
    return JsonResponse(prompts, safe=False)


def get_shared_prompts(request):
    """Возвращает общие (shared) промпты с привязкой к языкам."""
    if not _has_page_access(request):
        return _deny_access(request, as_json=True)

    ui_language = request.GET.get('ui_language', 'Русский')
    language_id = request.GET.get('language_id')
    qs = SharedPrompt.objects.prefetch_related('programming_languages').filter(Q(mode__isnull=True) | Q(mode=''))

    if language_id:
        # Фильтруем: либо общий промпт привязан к этому языку, либо без привязки (для всех)
        qs = qs.filter(
            models.Q(programming_languages__id=language_id) | models.Q(programming_languages__isnull=True)
        ).distinct()

    shared = [
        serialize_shared_prompt_with_dates(sp, ui_language)
        for sp in qs
    ]
    return JsonResponse(shared, safe=False)


def get_problem_data(request):
    """Возвращает языки, темы, промпты и общие промпты одним запросом."""
    if not _has_page_access(request):
        return _deny_access(request, as_json=True)

    ui_language = request.GET.get('ui_language', 'Русский')

    languages = [
        serialize_programming_language(lang)
        for lang in ProgrammingLanguage.objects.order_by('language_name')
    ]
    topics = [
        serialize_topic(topic, ui_language)
        for topic in Topic.objects.select_related("programming_language").order_by('topic_name_ru')
    ]
    prompts = [
        serialize_prompt(p, ui_language)
        for p in Prompt.objects.select_related("topic", "topic__programming_language", "programming_language", "owner", "shared_prompt").order_by('prompt_name_ru', 'id')
    ]
    shared_prompts = [
        serialize_shared_prompt(sp, ui_language)
        for sp in SharedPrompt.objects.prefetch_related('programming_languages').filter(Q(mode__isnull=True) | Q(mode=''))
    ]

    return JsonResponse({
        'languages': languages,
        'topics': topics,
        'prompts': prompts,
        'shared_prompts': shared_prompts,
    })


def get_model_stats_view(request):
    """Накопленная статистика моделей по скоупу (язык[, тема]) — суффиксы
    селектора моделей «Реши задачу»/«В чём ошибка».

    GET /ai/api/model-stats/?progLng=<id>&topic=<id>. Тема уточняет скоуп до
    точной темы-строки; только язык → языковая строка (тема NULL) — общие
    данные по всем темам языка; без обоих → ОБЩИЕ данные по всем темам и
    языкам (агрегат scoped-строк модели — see get_model_stats_map). Нет
    строк выбранного скоупа → для таких моделей ключа в ``stats`` не будет
    («ничего»), фолбэков между скоупами нет.
    """
    if not _has_page_access(request):
        return _deny_access(request, as_json=True)

    try:
        language_id = int(request.GET.get("progLng") or "")
    except ValueError:
        language_id = None  # нет/битый progLng — общий скоуп (агрегат)
    try:
        topic_id = int(request.GET.get("topic") or "")
    except ValueError:
        topic_id = None

    from .services.model_stats import get_model_stats_map

    # Ключи зарегистрированных моделей: статистика не-зарегистрированных
    # ключей (легаси-переименования/удалённые провайдеры) не отдаётся.
    stats = get_model_stats_map(
        list(registry.keys()), language_id=language_id, topic_id=topic_id,
    )
    return JsonResponse({"success": True, "stats": stats})


#: Соль валидатора asset_view: одна на жизнь процесса (рестарт контейнера =
#: новый деплой). Несколько worker-процессов давали бы разные соли и ломали бы
#: 304-экономию между запросами; у нас Daphne ровно в один процесс.
_ASSET_BOOT_SALT = os.urandom(16)


def asset_view(request, asset_path):
    """Отдаёт статический файл из Django staticfiles по пути (для AI-страниц).

    Ассеты ревалидируются браузером (Cache-Control: no-cache + Last-Modified + 304),
    поэтому после деплоя браузер подхватывает обновлённый JS/CSS даже без ?v=-бампа.
    Условный запрос решается по ETag (md5 КОНТЕНТА + соль старта процесса, а
    не mtime): mtime при деплое мог не сдвинуться (таймстампы/WSL-копирование),
    и 304 отдавал СТАРЫЙ файл — «стили падали». Соль (сгенерирована при старте
    процесса) нужна против «отравленного» кэша браузера: если когда-то в кэш
    попало ПУСТОЕ тело с живым md5-валидатором, ревалидация честно отвечала
    304 и браузер навсегда рисовал без стилей (сам не лечился). Новый процесс
    = новая соль = валидатор не совпадёт = свежие байты гарантированно
    перекачаются; живой кэш продолжает работать (304 по-прежнему честные 304).
    Ревалидация по If-Modified-Since остаётся fallback'ом для кэшей без ETag.
    304 всегда несёт ETag + Cache-Control — иначе браузер без закэшированного
    тела рисует пустой ресурс.
    """
    if not _has_page_access(request):
        return _deny_access(request, as_json=False)
    asset_full_path = finders.find(asset_path)
    if not asset_full_path or not os.path.isfile(asset_full_path):
        raise Http404("Asset not found")

    with open(asset_full_path, "rb") as fh:
        data = fh.read()
    etag = f'"{hashlib.md5(_ASSET_BOOT_SALT + data).hexdigest()}"'

    if_none_match = request.META.get("HTTP_IF_NONE_MATCH", "")
    if if_none_match:
        # Срезаем W/ и кавычки: клиент может прислать слабый валидатор/список.
        candidates = {
            tag.strip().removeprefix('W/').strip('"')
            for tag in if_none_match.split(",")
        }
        if etag.strip('"') in candidates:
            not_modified = HttpResponseNotModified()
            not_modified["ETag"] = etag
            not_modified["Cache-Control"] = "no-cache"
            return not_modified
    else:
        statobj = os.stat(asset_full_path)
        if not was_modified_since(
            request.META.get("HTTP_IF_MODIFIED_SINCE"), statobj.st_mtime
        ):
            not_modified = HttpResponseNotModified()
            not_modified["ETag"] = etag
            not_modified["Cache-Control"] = "no-cache"
            return not_modified

    content_type, _ = mimetypes.guess_type(asset_path)
    response = FileResponse(io.BytesIO(data), content_type=content_type)
    response["Cache-Control"] = "no-cache"
    response["ETag"] = etag
    response["Last-Modified"] = http_date(os.stat(asset_full_path).st_mtime)
    return response

@require_http_methods(["GET"])
def get_task_info_view(request):
    """Proxy task metadata from the external DL REST API.

    Query parameters:
        nodeId (int, required): DL node identifier.
        removeHtmlTags (bool, default True): strip HTML from texts.
    """
    if not _has_page_access(request):
        return _deny_access(request, as_json=True)

    try:
        node_id = int(request.GET.get("nodeId", ""))
    except (ValueError, TypeError):
        return JsonResponse({"error": "nodeId обязателен и должен быть числом"}, status=400)

    remove_html_tags = request.GET.get("removeHtmlTags", "true").strip().lower() not in ("false", "0", "")

    session_id = request.GET.get("sessionId", "").strip()
    if not session_id:
        session_id = request.session.get("external_session_id", "").strip()
    if not session_id:
        session_id = resolve_dl_session_id(request)

    # Sanitize session_id — reject if it contains path separators or URL characters
    # that could be used for SSRF or injection into DL API requests.
    if session_id and any(c in session_id for c in "\\/\n\r\t "):
        return JsonResponse({"error": "Invalid session id format"}, status=400)

    # dl.gsu.by требует courseId в get-task-info: без него REST отвечает
    # 400 Bad Request, который _raise_for_status превращает в DLServerError
    # (наш 502). Берём курс пользователя из validated user_info (middleware
    # кладёт его на каждый запрос — courseID из get-user-info); явный
    # параметр запроса имеет приоритет.
    course_raw = request.GET.get("courseId", "").strip()
    if not course_raw.isdigit():
        user_info = getattr(request, "user_info", None) or {}
        course_raw = str(user_info.get("courseID") or "").strip()
    course_id = int(course_raw) if course_raw.isdigit() else None

    try:
        data = fetch_task_info(
            node_id, session_id=session_id, remove_html_tags=remove_html_tags,
            course_id=course_id,
        )
    except (DLUnauthorizedError, DLForbiddenError, DLTaskNotFoundError,
            DLApiUnavailable, DLServerError) as exc:
        return dl_error_response(exc)

    # DL's own HTML stripping (removeHtmlTags=true) sometimes yields an empty
    # statement for tasks whose condition is nonetheless visible on the DL site
    # (DL's stripper fails on the markup). Re-fetch omitting removeHtmlTags so DL
    # returns its default (raw, HTML) response, then strip HTML server-side so
    # the condition still loads on /ai/solve-problem/. Only triggers when the
    # stripped statement is empty — existing non-empty results are unchanged.
    if remove_html_tags and not (data.get("statement") or "").strip() \
            and not (data.get("currentStatement") or "").strip():
        try:
            raw = fetch_task_info(
                node_id, session_id=session_id, remove_html_tags=None,
                course_id=course_id,
            )
            raw_statement = (raw.get("statement") or raw.get("currentStatement") or "").strip()
            if raw_statement:
                stripped = strip_tags(raw_statement).strip()
                if stripped:
                    data["statement"] = stripped
        except (DLUnauthorizedError, DLForbiddenError, DLTaskNotFoundError,
                DLApiUnavailable, DLServerError):
            pass  # keep the original (empty-statement) response

    # DL складывает в statement вместе с условием мусор страницы задачи:
    # хвостовой ярлык кнопки «Подсмотреть решение» (и §nbsp-пустоту вокруг).
    # Чистим хвост — модели и пользователю ярлык не нужен. Только хвост:
    # остальное (включая заголовок-«шапку», который DL хранит в тексте
    # некоторых задач) — данные DL, их не трогаем.
    for key in ("statement", "currentStatement"):
        val = (data.get(key) or "").strip()
        if val:
            cleaned = val.replace("\u00a0", " ").rstrip()
            while cleaned.endswith("Подсмотреть решение"):
                cleaned = cleaned[: -len("Подсмотреть решение")].rstrip()
            if cleaned != val:
                data[key] = cleaned

    # Translate the task statement into the page's UI language. DL returns
    # statements in Russian, so for the English/French UI languages we
    # translate server-side via Google Translate (deep-translator, no API key)
    # and cache per (node_id, lang, source hash) so a page reload or a language
    # toggle reuses the cached translation instead of re-hitting Google.
    # Russian (and any unmapped value) is returned as-is.
    ui_language = request.GET.get("ui_language", "Русский").strip()
    target_lang = _TASK_TRANSLATION_LANG_MAP.get(ui_language)
    if target_lang:
        statement = (data.get("statement") or data.get("currentStatement") or "").strip()
        if statement:
            data["statement"] = _translate_task_statement(node_id, statement, target_lang)

    return JsonResponse(data)


# UI language (as used across the app: Russian/English/French) → deep-translator
# target code. Russian needs no translation (DL already serves the statement in
# Russian); the other two are translated via Google Translate on demand.
_TASK_TRANSLATION_LANG_MAP = {"English": "en", "French": "fr"}


def _translate_task_statement(node_id: int, text: str, target_lang: str) -> str:
    """Return ``text`` translated to ``target_lang`` (en/fr), cached per node.

    The cache key includes a short hash of the source text, so a statement that
    changes on the DL side is re-translated immediately rather than serving a
    stale cached translation. If translation fails (Google error / unsupported
    text), the original Russian ``text`` is returned unchanged — the page still
    shows the condition, just not translated.
    """
    import hashlib
    from django.core.cache import cache
    from .constants import AI_CACHE_KEY_PREFIX
    from .services.auto_translate import translate_text

    source_hash = hashlib.md5(text.encode("utf-8")).hexdigest()[:8]
    cache_key = f"{AI_CACHE_KEY_PREFIX}:task_stmt:{node_id}:{target_lang}:{source_hash}"
    cached = cache.get(cache_key)
    if cached:
        return cached

    translated = translate_text(text, target_lang)
    if not translated:
        return text  # graceful fallback: keep the original statement
    cache.set(cache_key, translated, timeout=60 * 60 * 24 * 30)  # 30 days
    return translated


@require_http_methods(["POST"])
def get_task_solution_view(request):
    """Proxy sample solution content from the external DL REST API.

    Request body (JSON):
        taskId (int, required): DL task identifier.
        fileExtension (str, required): solution file extension, e.g. .pas, .cpp, .py.
        sessionId (str, optional): DL session id; falls back to request session/cookie.
    """
    if not _has_page_access(request):
        return _deny_access(request, as_json=True)

    try:
        body = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Некорректный JSON в теле запроса"}, status=400)

    try:
        task_id = int(body.get("taskId"))
    except (ValueError, TypeError):
        return JsonResponse({"error": "taskId обязателен и должен быть числом"}, status=400)

    file_extension = (body.get("fileExtension") or "").strip()
    if not file_extension:
        return JsonResponse({"error": "fileExtension обязателен"}, status=400)

    session_id = body.get("sessionId", "").strip()
    if not session_id:
        session_id = request.session.get("external_session_id", "").strip()
    if not session_id:
        session_id = resolve_dl_session_id(request)
    if not session_id:
        return JsonResponse({"error": "sessionId обязателен"}, status=400)

    try:
        data = fetch_task_solution(session_id, task_id, file_extension)
    except (DLUnauthorizedError, DLForbiddenError, DLTaskNotFoundError,
            DLApiUnavailable, DLServerError) as exc:
        return dl_error_response(exc)

    return JsonResponse(data)


@require_http_methods(["POST"])
def send_solution_view(request):
    """Submit AI-generated code to DL for automated testing.

    Request body (JSON):
        nodeId (int, required): DL node id to submit to.
        code (str, required): solution source code.
        fileExtension (str, required): e.g. .pas, .cpp, .py.
        progLanguageName (str, optional): fallback — язык из селектора страницы
            («Реши задачу»); расширение резолвится через _guess_extension.
        progLanguageId (int, optional): fallback — id ProgrammingLanguage, имя
            берётся из БД, затем то же _guess_extension.
        modelKey (str, optional): модель, сгенерировавшая код, — для
            подписанного имени файла ``<модель>_<task_id>.<ext>``.
        fileName (str, optional): имя файла; если не задано — строится из
            modelKey (или последней модели решения) и taskId (fallback nodeId).
        sessionId (str, optional): falls back to session/cookie.
    """
    if not _has_page_access(request):
        return _deny_access(request, as_json=True)
    # Гейт DL «canUseAi»: AI в текущем курсе запрещён → отправка решения
    # на проверку бессмысленна (пользователь не сможет и сгенерировать код).
    disabled_reason = _ai_disabled_reason(request)
    if disabled_reason:
        return JsonResponse({"error": disabled_reason}, status=403)

    try:
        body = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Некорректный JSON"}, status=400)

    try:
        node_id = int(body.get("nodeId"))
    except (ValueError, TypeError):
        return JsonResponse({"error": "nodeId обязателен и должен быть числом"}, status=400)

    code = (body.get("code") or "").strip()
    if not code:
        return JsonResponse({"error": "code обязателен"}, status=400)

    # fileExtension можно не передавать явно: клиент шлёт язык программирования
    # из селектора страницы, расширение резолвится серверно через единый
    # источник соответствия язык→расширение (_guess_extension, task_registry).
    file_extension = (body.get("fileExtension") or "").strip()
    # ID языка парсим всегда, когда передан (даже вместе с именем): он нужен
    # не только для расширения, но и кэшу решённых задач
    # (TaskSolution.programming_language_id) — иначе кэш не находится.
    prog_language_id = None
    if body.get("progLanguageId") not in (None, ""):
        try:
            prog_language_id = int(body["progLanguageId"])
        except (ValueError, TypeError):
            prog_language_id = None
    if not file_extension:
        lang_name = (body.get("progLanguageName") or "").strip()
        if not lang_name and prog_language_id:
            try:
                from .models import ProgrammingLanguage
                lang_name = (ProgrammingLanguage.objects.filter(
                    pk=prog_language_id,
                ).values_list("language_name", flat=True).first() or "")
            except (ValueError, TypeError):
                lang_name = ""
        from .services.task_registry import _guess_extension
        file_extension = _guess_extension(lang_name)
        if not file_extension:
            return JsonResponse(
                {"error": "Выберите язык программирования — по нему определяется расширение файла"},
                status=400,
            )

    session_id = body.get("sessionId", "").strip()
    if not session_id:
        session_id = request.session.get("external_session_id", "").strip()
    if not session_id:
        session_id = resolve_dl_session_id(request)
    if not session_id:
        return JsonResponse({"error": "sessionId обязателен"}, status=400)

    # courseId обязателен по контракту REST API send-solution. Берём из тела,
    # иначе резолвим активный курс пользователя из DL-сессии.
    course_id = 0
    raw_course = body.get("courseId")
    try:
        course_id = int(raw_course) if raw_course not in (None, "") else 0
    except (ValueError, TypeError):
        return JsonResponse({"error": "courseId должен быть числом"}, status=400)
    if not course_id:
        from .admin.arm import _resolve_active_course_id_for_session
        course_id, _course_err = _resolve_active_course_id_for_session(session_id)
        course_id = course_id or 0

    # Имя временного файла решения (контракт DL v1.4.2, SendSolutionRequest.fileName)
    # и машина проверки (delta) — необязательные, приходят из тела.
    file_name = str(body.get("fileName") or "").strip()[:200]
    if not file_name:
        # Подписанное имя по умолчанию — ``<модель>_<task_id>.<ext>``:
        # в DL сразу видно, чья это попытка и по какой задаче. taskId берём из
        # локальной записи Task (get-task-info); если задачи ещё нет в ARM —
        # fallback на nodeId узла. Модель — из тела (клиент знает выбранную в
        # селекторе), иначе из последней генерации решения для узла;
        # чистка/транслитерация — build_dl_file_name.
        from .models import Task
        file_task_id = (
            Task.objects.filter(node_id=node_id)
            .values_list("task_id", flat=True).first()
        ) or node_id
        model_label = str(body.get("modelTitle") or "").strip()[:200]
        if not model_label:
            model_key = str(body.get("modelKey") or "").strip()
            if model_key:
                from .model_clients.registry import registry
                entry = registry.get(model_key) or {}
                model_label = entry.get("title") or model_key
        if not model_label:
            from .services.solution_cache import last_solve_model
            _key, model_label = last_solve_model(node_id)
        model_label = model_label or "dlai"
        from .dl_api_client import build_dl_file_name
        file_name = build_dl_file_name(f"{model_label}_{file_task_id}", file_extension) or ""
    delta = str(body.get("delta") or "").strip()[:255]

    try:
        from .dl_api_client import send_solution_to_dl
        data = send_solution_to_dl(
            session_id, node_id, code, file_extension,
            course_id=course_id, file_name=file_name, delta=delta,
        )
    except (DLUnauthorizedError, DLApiUnavailable, DLServerError) as exc:
        return dl_error_response(exc)

    # Решение отправлено на тестирование самим пользователем со страницы —
    # фиксируем тестирование в журнале (mode=testing) и пишем кэш решённых
    # задач (после успешного теста код переиспользуется без вызова модели).
    # Сбой статистики не должен ломать ответ клиенту.
    try:
        from .models import AIRequestLog
        from .services.solution_cache import last_solve_model, record_submission
        identity = get_user_identity_for_log(request.user, getattr(request, "user_info", None))
        queue_id = data.get("queueId")
        queue_id = int(queue_id) if queue_id else None
        model_key, model_title = last_solve_model(node_id)
        test_log = AIRequestLog.objects.create(
            user=identity["user"],
            username=identity["username"],
            external_user_id=identity["external_user_id"],
            user_full_name=identity["user_full_name"],
            source=AIRequestLog.SOURCE_HTTP,
            mode=AIRequestLog.MODE_TESTING,
            sent_at=timezone.now(),
            model_names=[model_key] if model_key else [],
            message=code,
            task_node_id=node_id,
        )
        record_submission(
            node_id, code,
            programming_language_id=prog_language_id,
            file_extension=file_extension,
            course_id=course_id,
            identity=identity,
            queue_id=queue_id,
            test_log=test_log,
            session_id=session_id,
        )
    except Exception:
        logger.exception("Failed to record solution submission for node %s", node_id)

    return JsonResponse(data)


@require_http_methods(["POST"])
def get_solution_result_view(request):
    """Poll DL for the result of a submitted solution.

    Request body (JSON):
        queueId (int, required): id returned by send-solution.
        sessionId (str, optional): falls back to session/cookie.
    """
    if not _has_page_access(request):
        return _deny_access(request, as_json=True)
    # Тот же гейт DL «canUseAi», что и на отправке: опрос результата
    # проверки решения — часть AI-сценария.
    disabled_reason = _ai_disabled_reason(request)
    if disabled_reason:
        return JsonResponse({"error": disabled_reason}, status=403)

    try:
        body = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Некорректный JSON"}, status=400)

    try:
        queue_id = int(body.get("queueId"))
    except (ValueError, TypeError):
        return JsonResponse({"error": "queueId обязателен и должен быть числом"}, status=400)

    session_id = body.get("sessionId", "").strip()
    if not session_id:
        session_id = request.session.get("external_session_id", "").strip()
    if not session_id:
        session_id = resolve_dl_session_id(request)
    if not session_id:
        return JsonResponse({"error": "sessionId обязателен"}, status=400)

    try:
        from .dl_api_client import get_solution_result_from_dl
        data = get_solution_result_from_dl(session_id, queue_id)
    except (DLUnauthorizedError, DLTaskNotFoundError, DLApiUnavailable, DLServerError) as exc:
        return dl_error_response(exc)

    # Финальный вердикт DL закрывает кэш решённых задач: passed → код
    # сохраняется для повторной выдачи, статистика модели инкрементируется
    # (AIModelStats). Сбой учёта не должен ломать ответ клиенту.
    if data.get("isFinished"):
        try:
            from .services.solution_cache import record_result
            record_result(queue_id, data.get("comment") or "")
        except Exception:
            logger.exception("Failed to record solution result for queue %s", queue_id)

    return JsonResponse(data)


@require_http_methods(["POST"])
def get_dl_test_data_view(request):
    """Входные/эталонные данные проваленного теста DL (ручная проверка).

    Request body (JSON):
        queueId (int, required): id, полученный от send-solution.
        comment (str, optional): вердикт DL; если он «решено», в DL не ходим
            (незачем запрашивать данные у успешного решения).
        sessionId (str, optional): falls back to session/cookie.

    Данные теста — необязательное дополнение к отчёту: отказ/сбой DL не
    поднимается наружу, отдаётся пустой список. Фронтенд рисует блоки
    «Входные данные» / «Выходные данные (эталон)».
    """
    if not _has_page_access(request):
        return _deny_access(request, as_json=True)
    disabled_reason = _ai_disabled_reason(request)
    if disabled_reason:
        return JsonResponse({"error": disabled_reason}, status=403)

    try:
        body = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Некорректный JSON"}, status=400)
    if not isinstance(body, dict):
        return JsonResponse({"error": "Некорректный JSON"}, status=400)

    try:
        queue_id = int(body.get("queueId"))
    except (ValueError, TypeError):
        return JsonResponse(
            {"error": "queueId обязателен и должен быть числом"}, status=400,
        )

    from .dl_api_client import DL_VERDICT_SOLVED, dl_verdict_from_comment

    comment = str(body.get("comment") or "").strip()
    if comment and dl_verdict_from_comment(comment) == DL_VERDICT_SOLVED:
        return JsonResponse({"ok": True, "solved": True, "tests": []})

    session_id = str(body.get("sessionId") or "").strip()
    if not session_id:
        session_id = request.session.get("external_session_id", "").strip()
    if not session_id:
        session_id = resolve_dl_session_id(request)
    if not session_id:
        return JsonResponse({"error": "sessionId обязателен"}, status=400)

    from .services.dl_test_data import fetch_failed_test_data

    try:
        tests = fetch_failed_test_data(session_id, queue_id, comment)
    except Exception:  # noqa: BLE001 — данные теста не обязательны
        logger.exception("Не удалось получить данные теста DL (queue %s)", queue_id)
        tests = []
    return JsonResponse({"ok": True, "solved": False, "tests": tests})


@prompt_developer_access_required
@require_http_methods(["POST"])
def transcribe_audio(request):
    audio_file = request.FILES.get('audio')
    if not audio_file:
        return JsonResponse({'success': False, 'error': 'No audio file provided'})

    # Prevent abuse via oversized uploads.
    max_size_mb = getattr(settings, "AI_TRANSCRIBE_MAX_SIZE_MB", 10)
    if audio_file.size and audio_file.size > max_size_mb * 1024 * 1024:
        return JsonResponse({'success': False, 'error': f'Audio file too large (max {max_size_mb} MB)'})

    # Validate MIME type — only allow audio formats we process.
    allowed_content_types = {'audio/webm', 'audio/ogg', 'audio/wav', 'audio/mpeg', 'audio/mp3', 'audio/mp4', 'audio/x-wav', 'application/octet-stream'}
    if audio_file.content_type and audio_file.content_type not in allowed_content_types:
        return JsonResponse({'success': False, 'error': f'Unsupported audio format: {audio_file.content_type}'})

    # Validate file extension as secondary check.
    allowed_extensions = {'.webm', '.ogg', '.wav', '.mp3', '.mp4', '.m4a'}
    file_ext = os.path.splitext(audio_file.name)[1].lower()
    if file_ext and file_ext not in allowed_extensions:
        return JsonResponse({'success': False, 'error': f'Unsupported file extension: {file_ext}'})

    language = request.POST.get('language', 'Russian')
    
    # Сохраняется временный файл
    with tempfile.NamedTemporaryFile(suffix='.webm', delete=False) as tmp:
        for chunk in audio_file.chunks():
            tmp.write(chunk)
        tmp_path = tmp.name

    try:
        # Конвертируется webm в wav (ffmpeg берём из pip-пакета imageio-ffmpeg, не из apt)
        from pydub import AudioSegment
        import imageio_ffmpeg
        AudioSegment.converter = imageio_ffmpeg.get_ffmpeg_exe()

        audio = AudioSegment.from_file(tmp_path, format='webm')

        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as wav_tmp:
            audio.export(wav_tmp.name, format='wav')
            wav_path = wav_tmp.name

        import speech_recognition as sr
        recognizer = sr.Recognizer()

        with sr.AudioFile(wav_path) as source:
            audio_data = recognizer.record(source)

        lang_map = {
            'Russian': 'ru-RU',
            'English': 'en-US',
            'French': 'fr-FR'
        }

        text = recognizer.recognize_google(audio_data, language=lang_map.get(language, 'en-US'))

        # Чистим временные файлы
        os.unlink(tmp_path)
        os.unlink(wav_path)

        return JsonResponse({'success': True, 'text': text})

    except sr.UnknownValueError:
        return JsonResponse({'success': False, 'error': 'Не удалось разобрать речь'})
    except sr.RequestError as e:
        return JsonResponse({'success': False, 'error': f'Ошибка сервиса распознавания: {e}'})
    except Exception as e:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        return JsonResponse({'success': False, 'error': str(e)})


@require_http_methods(["GET"])
def chat_user_docs_view(request):
    """Инструкция пользователя для модалки «?» в шапке чата.

    Рендер главы «Инструкция для пользователя» (DOCX.md) в HTML
    (ai/services/docs.py). Гейт как у страниц чата (_has_page_access → 403):
    доступна любому аутентифицированному DL-пользователю, роль не нужна.

    ?lang= — язык интерфейса (значение атрибута language селекта #selectLang,
    напр. "Russian"/"English"/"French"); глава переводится (en/fr, Google),
    ответ несёт lang и fingerprint (mtime, size) для поллинга изменений.
    """
    if not _has_page_access(request):
        return JsonResponse(
            {'success': False, 'error': _access_denied_reason(request)}, status=403,
        )
    lang = get_ui_language_suffix(request.GET.get('lang', ''))
    try:
        rendered = render_chapter_html("user", lang=lang)
    except DocUnavailableError as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=404)
    return JsonResponse({
        'success': True,
        'title': rendered['title'],
        'html': rendered['html'],
        'lang': rendered['lang'],
        'translated': rendered.get('translated', True),
        'fingerprint': rendered['fingerprint'],
        'download_url': f'/ai/docs/download/?lang={lang}',
    })


@require_http_methods(["GET"])
def chat_user_docs_download_view(request):
    """Скачивание инструкции пользователя как .md-файла (с учётом ?lang=)."""
    if not _has_page_access(request):
        return JsonResponse(
            {'success': False, 'error': _access_denied_reason(request)}, status=403,
        )
    lang = get_ui_language_suffix(request.GET.get('lang', ''))
    try:
        data = read_chapter_markdown("user", lang=lang)
    except DocUnavailableError as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=404)
    from urllib.parse import quote
    filename = quote(data['filename'])
    response = HttpResponse(data['text'], content_type='text/markdown; charset=utf-8')
    response['Content-Disposition'] = f"attachment; filename=\"docs.md\"; filename*=UTF-8''{filename}"
    return response
