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
import logging
import threading
import time
import asyncio

import speech_recognition as sr
import tempfile
from django.contrib.auth import login
from django.shortcuts import redirect, render
from django.http import JsonResponse
from django.http import FileResponse, Http404, HttpResponseForbidden, HttpResponseNotFound, HttpResponseNotModified
from django.db import ProgrammingError, models
from django.db.models import Q
from django.contrib.staticfiles import finders
from django.utils.html import strip_tags
from django.utils.http import http_date
from django.views.static import was_modified_since
from django.middleware import csrf
from functools import wraps

from django.views.decorators.http import require_http_methods  
from django.conf import settings
from .throttling import rate_limited
from .model_health import get_available_model_options, trigger_model_health_refresh_async
from .models import ProgrammingLanguage, Topic, Prompt, SharedPrompt, AIAppSettings, ExternalDLAccount
from .auth_backends import (
    ADMIN_EXTERNAL_AUTH_BACKEND,
    create_admin_user_with_password,
    ensure_prompt_developer_group,
    get_admin_user_by_external_id,
    get_external_user_id_from_request,
    normalize_external_user_id,
)
from .constants import PROMPT_DEVELOPER_GROUP
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
from .i18n import get_language_instruction, get_localized_name, get_localized_text
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

        # Посчитать частоту моделей для этого пользователя по последним 200 логам
        c = Counter()
        for mns in qs.order_by('-sent_at').values_list('model_names', flat=True)[:200]:
            if mns:
                for m in mns:
                    key = title_to_key.get(m)
                    if key:
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
    """Декоратор: разрешает доступ только суперпользователям и членам группы prompt_developer."""
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        user = getattr(request, "user", None)
        if not user or not user.is_authenticated:
            return HttpResponseForbidden("Prompt developer access required")
        if user.is_superuser or user.groups.filter(name=PROMPT_DEVELOPER_GROUP).exists():
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
    return bool(get_external_user_id_from_request(request))


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


def _render_ai_page(request, template_name, extra_context=None):
    """Рендерит страницу AI (chat/solve/find-error) с общим контекстом.

    Проверяет доступ, загружает список доступных моделей (с self-heal при пустом
    списке), сортирует их (топ-1 фаворит пользователя → алфавит) и разбивает на
    группы favorite_models/other_models для optgroup-разделителя в шаблоне, и
    добавляет external_session_id + сохранённый из куки язык.
    """
    if not _has_page_access(request):
        return HttpResponseForbidden("Authentication required")
    if not _is_ai_app_enabled():
        return HttpResponseNotFound("AI app is disabled")
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
        user_picks = [model_map[k] for k in user_top if k in model_map]

        # Остальные — строго по алфавиту (title). Без отдельного приоритета
        # Web_DeepSeek: фаворит наверху, далее алфавит (включая Web_DeepSeek).
        used_keys = set(user_top)
        rest = sorted(
            [item for item in available_models if item["key"] not in used_keys],
            key=lambda x: x["title"].lower(),
        )

        # Плоский список для обратной совместимости (тесты проверяют порядок).
        available_models = user_picks + rest
        # Группы для optgroup-разделителя в шаблоне: топ-1 фаворит + остальные.
        favorite_models = user_picks[:1]
        other_models = sorted(rest + user_picks[1:], key=lambda x: x["title"].lower())
    else:
        favorite_models = []
        other_models = []
    saved_state = _read_ai_state(request)
    external_session_id = request.session.get('external_session_id')
    context = {
        'available_models': available_models,
        'favorite_models': favorite_models,
        'other_models': other_models,
        'external_session_id': external_session_id,
        'last_update_date': _get_last_update_date(),
        'saved_lang': saved_state.get('lang') or '',
    }
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
        return HttpResponseForbidden("Authentication required")
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
            ensure_prompt_developer_group(target_user)
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
                ensure_prompt_developer_group(target_user)
            else:
                target_user = create_admin_user_with_password(external_user_id, new_password)
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
        ensure_prompt_developer_group(target_user)
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
    """Страница чата с AI-моделью."""
    return _render_ai_page(request, 'ai/chat.html')


def decide_task_view(request):
    """Страница решения задачи: принимает node_id из query-параметров."""
    return _render_ai_page(
        request,
        'ai/decide-task.html',
        extra_context={
            "node_id": request.GET.get("nid", ""),
        },
    )


def find_error_view(request):
    """Страница поиска ошибок в коде с помощью AI."""
    return _render_ai_page(request, 'ai/find-error.html')


def get_languages(request):
    """API: возвращает список всех языков программирования."""
    if not _has_page_access(request):
        return HttpResponseForbidden("Authentication required")
    languages = [
        serialize_programming_language(lang)
        for lang in ProgrammingLanguage.objects.order_by('language_name')
    ]
    return JsonResponse(languages, safe=False)


def get_topics(request):
    """API: возвращает список всех тем с локализованными названиями."""
    if not _has_page_access(request):
        return HttpResponseForbidden("Authentication required")
    ui_language = request.GET.get('ui_language', 'Русский')
    topics = [
        serialize_topic(topic, ui_language)
        for topic in Topic.objects.select_related("programming_language").order_by('topic_name')
    ]
    return JsonResponse(topics, safe=False)


def get_prompts(request):
    """API: возвращает список промптов, доступных текущему пользователю."""
    if not _has_page_access(request):
        return HttpResponseForbidden("Authentication required")

    ui_language = request.GET.get('ui_language', 'Русский')
    prompts = [
        serialize_prompt(p, ui_language)
        for p in Prompt.objects.select_related("topic", "topic__programming_language", "owner", "shared_prompt").order_by('prompt_name', 'id')
    ]
    return JsonResponse(prompts, safe=False)


def get_shared_prompts(request):
    """Возвращает общие (shared) препромпты с привязкой к языкам."""
    if not _has_page_access(request):
        return HttpResponseForbidden("Authentication required")

    ui_language = request.GET.get('ui_language', 'Русский')
    language_id = request.GET.get('language_id')
    qs = SharedPrompt.objects.prefetch_related('programming_languages').filter(Q(mode__isnull=True) | Q(mode=''))

    if language_id:
        # Фильтруем: либо общий препромпт привязан к этому языку, либо без привязки (для всех)
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
        return HttpResponseForbidden("Authentication required")

    ui_language = request.GET.get('ui_language', 'Русский')

    languages = [
        serialize_programming_language(lang)
        for lang in ProgrammingLanguage.objects.order_by('language_name')
    ]
    topics = [
        serialize_topic(topic, ui_language)
        for topic in Topic.objects.select_related("programming_language").order_by('topic_name')
    ]
    prompts = [
        serialize_prompt(p, ui_language)
        for p in Prompt.objects.select_related("topic", "topic__programming_language", "owner", "shared_prompt").order_by('prompt_name', 'id')
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


def asset_view(request, asset_path):
    """Отдаёт статический файл из Django staticfiles по пути (для AI-страниц).

    Ассеты ревалидируются браузером (Cache-Control: no-cache + Last-Modified + 304),
    поэтому после деплоя браузер подхватывает обновлённый JS/CSS даже без ?v=-бампа.
    """
    if not _has_page_access(request):
        return HttpResponseForbidden("Authentication required")
    asset_full_path = finders.find(asset_path)
    if not asset_full_path or not os.path.isfile(asset_full_path):
        raise Http404("Asset not found")

    statobj = os.stat(asset_full_path)
    mtime = statobj.st_mtime
    if not was_modified_since(request.META.get("HTTP_IF_MODIFIED_SINCE"), mtime):
        return HttpResponseNotModified()

    response = FileResponse(open(asset_full_path, "rb"))
    response["Cache-Control"] = "no-cache"
    response["Last-Modified"] = http_date(mtime)
    return response

@require_http_methods(["GET"])
def get_task_info_view(request):
    """Proxy task metadata from the external DL REST API.

    Query parameters:
        nodeId (int, required): DL node identifier.
        removeHtmlTags (bool, default True): strip HTML from texts.
    """
    if not _has_page_access(request):
        return HttpResponseForbidden("Authentication required")

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

    try:
        data = fetch_task_info(node_id, session_id=session_id, remove_html_tags=remove_html_tags)
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
            raw = fetch_task_info(node_id, session_id=session_id, remove_html_tags=None)
            raw_statement = (raw.get("statement") or raw.get("currentStatement") or "").strip()
            if raw_statement:
                stripped = strip_tags(raw_statement).strip()
                if stripped:
                    data["statement"] = stripped
        except (DLUnauthorizedError, DLForbiddenError, DLTaskNotFoundError,
                DLApiUnavailable, DLServerError):
            pass  # keep the original (empty-statement) response

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
        return HttpResponseForbidden("Authentication required")

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
        sessionId (str, optional): falls back to session/cookie.
    """
    if not _has_page_access(request):
        return HttpResponseForbidden("Authentication required")

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

    try:
        from .dl_api_client import send_solution_to_dl
        data = send_solution_to_dl(session_id, node_id, code, file_extension, course_id=course_id)
    except (DLUnauthorizedError, DLApiUnavailable, DLServerError) as exc:
        return dl_error_response(exc)

    return JsonResponse(data)


@require_http_methods(["POST"])
def get_solution_result_view(request):
    """Poll DL for the result of a submitted solution.

    Request body (JSON):
        queueId (int, required): id returned by send-solution.
        sessionId (str, optional): falls back to session/cookie.
    """
    if not _has_page_access(request):
        return HttpResponseForbidden("Authentication required")

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

    return JsonResponse(data)


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
