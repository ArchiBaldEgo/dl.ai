"""Промежуточное ПО (middleware) для внешней аутентификации и CSRF.

ExternalAuthMiddleware: проверяет DLSID-куку, обращается к внешнему API
(dl.gsu.by) для получения информации о пользователе, автосоздаёт Django-пользователя
и привязывает сессию. При невалидном DLSID — редирект на dl.gsu.by.

CsrfSessionFallbackMiddleware: восстанавливает CSRF-токен из сессии при отсутствии куки.
"""

import hashlib
import os
import threading
import time
from urllib.parse import unquote

from django.conf import settings
from django.contrib.auth import login
from django.core.cache import cache
from django.db import close_old_connections
from django.http import JsonResponse, HttpResponseRedirect
from django.middleware import csrf
from .external_account import get_or_create_user_from_external
from .external_auth import (
    ExternalAuthMisconfigured,
    ExternalAuthUnauthorized,
    ExternalAuthUnavailable,
    fetch_external_user_info,
    get_external_auth_api_url,
    get_external_session_cookie_name,
)
import logging

logger = logging.getLogger(__name__)


def _is_admin_path(path):
    """Проверяет, является ли путь путём админки (/ai/admin или /ai/admin/...)."""
    normalized = (path or "/").rstrip("/") or "/"
    return normalized == "/ai/admin" or normalized.startswith("/ai/admin/")


def _normalize_path(path: str | None) -> str:
    """Нормализует путь: убирает trailing slash, пустой путь возвращает как '/'."""
    normalized = (path or "/").rstrip("/") or "/"
    return normalized


def _is_optional_auth_path(path: str) -> bool:
    """Пути, где отсутствие DLSID не вызывает редирект на dl.gsu.by.

    Только для точек входа админки (login/logout/set-password), где пользователь
    должен иметь доступ к форме до аутентификации через внешний API.
    """
    normalized = _normalize_path(path)
    return _is_admin_path(normalized)


_DEV_TRUTHY = {"1", "true", "yes", "y", "on"}


def _dev_bypass_enabled() -> bool:
    """Dev-bypass аутентификации: только при DEBUG, иначе флаг игнорируется.

    AI_DEV_AUTH_BYPASS=1 локально провижинит fake-суперпользователя без
    DLSID-куки и без обращения к dl.gsu.by. В prod (DEBUG=False) любое
    значение флага молча игнорируется.
    """
    if not getattr(settings, "DEBUG", False):
        return False
    return os.getenv("AI_DEV_AUTH_BYPASS", "").strip().lower() in _DEV_TRUTHY


def _dev_bypass_user_info() -> dict:
    """Fake user_info для dev-bypass (userId/login переопределяются env)."""
    return {
        "userId": os.getenv("AI_DEV_AUTH_USERID", "999999"),
        "login": os.getenv("AI_DEV_AUTH_LOGIN", "dev_admin"),
        "firstName": "Dev",
        "lastName": "Admin",
    }


class CsrfSessionFallbackMiddleware:
    """Восстанавливает CSRF-токен из сессии при отсутствии куки.

    При CSRF_USE_SESSIONS=True Django хранит токен в сессии. Если кука
    отсутствует (например, после очистки куки), middleware восстанавливает
    токен из сессии, чтобы формы продолжали работать.
    """
    def __init__(self, get_response):
        self.get_response = get_response
        self.use_sessions = bool(getattr(settings, "CSRF_USE_SESSIONS", False))
        self.primary_cookie_name = getattr(settings, "CSRF_COOKIE_NAME", "csrftoken")
        self.fallback_cookie_names = []
        if self.primary_cookie_name != "csrftoken":
            self.fallback_cookie_names.append("csrftoken")

    def __call__(self, request):
        if self.use_sessions and hasattr(request, "session"):
            if csrf.CSRF_SESSION_KEY not in request.session:
                for name in [self.primary_cookie_name, *self.fallback_cookie_names]:
                    value = request.COOKIES.get(name)
                    if value:
                        request.session[csrf.CSRF_SESSION_KEY] = value
                        request.session.modified = True
                        break
        return self.get_response(request)


class ExternalAuthMiddleware:
    """Промежуточное ПО внешней аутентификации через dl.gsu.by.

    Поток:
    1. Извлекает DLSID-куку из запроса.
    2. Если DLSID отсутствует — редирект на dl.gsu.by (кроме админских путей).
    3. Проверяет DLSID через внешний API (fetch_external_user_info).
    4. Автосоздаёт Django-пользователя через get_or_create_user_from_external.
    5. Привязывает сессию к подтверждённому пользователю (защита от stale-сессий).
    6. Кэширует user_info в сессии для повторных запросов.

    Результат работы доступен через request.user_info и request._ai_provisioned_user.
    """
    def __init__(self, get_response):
        self.get_response = get_response
        self.api_url = get_external_auth_api_url()
        self.session_cookie_name = get_external_session_cookie_name()
        self.redirect_url = os.getenv("EXTERNAL_AUTH_REDIRECT_URL", "https://dl.gsu.by")
        skip_paths = os.getenv("EXTERNAL_AUTH_SKIP_PATHS", "")
        self.skip_paths = self._build_skip_paths(skip_paths)
        self.cache_session_key = "external_session_id"
        self.cache_user_key = "external_user_info"
        self.cache_fetched_at_key = "external_user_info_fetched_at"
        # Маркеры уже выполненного провижининга: (внешний userId, pk Django-юзера).
        # Совпали с подтверждённым user_info и текущей сессией — значит, этот же
        # DLSID уже создал/привязал этого пользователя, и повторный
        # get_or_create_user_from_external (SELECT + возможный HTTP-обогащение)
        # на каждом запросе не нужен.
        self.provision_external_key = "ai_provisioned_external_id"
        self.provision_user_key = "ai_provisioned_user_pk"
        # How long a cached user_info is trusted without revalidating the DLSID
        # against dl.gsu.by (seconds). Default 60s; 0 = revalidate on every
        # request. Revalidation happens in the background (stale-while-
        # revalidate), so the request itself never waits for dl.gsu.by while a
        # cached identity exists.
        try:
            self.auth_cache_ttl = int(os.getenv("AI_AUTH_CACHE_TTL", "60"))
        except (TypeError, ValueError):
            self.auth_cache_ttl = 60
        # How long the last confirmed user_info is kept as a stale fallback
        # (Django cache). Survives session loss and dl.gsu.by downtime.
        try:
            self.auth_stale_ttl = int(os.getenv("AI_AUTH_STALE_TTL", "86400"))
        except (TypeError, ValueError):
            self.auth_stale_ttl = 86400
        logger.info(f"Middleware init: skip_paths={self.skip_paths}")

    def _build_skip_paths(self, raw_paths: str) -> list[str]:
        default_paths = ["/health", "/ai/assets/", "/ai/api/groq-limits"]
        entries = [*default_paths, *[p.strip() for p in raw_paths.split(",") if p.strip()]]
        normalized = []
        for path in entries:
            candidate = _normalize_path(path)
            if candidate not in normalized:
                normalized.append(candidate)
        return normalized

    def _is_skipped_path(self, request_path: str) -> bool:
        normalized = _normalize_path(request_path)
        for path in self.skip_paths:
            if normalized == path or normalized.startswith(path + "/"):
                return True
        return False

    def _get_cached_user_info(self, request, session_id: str) -> dict | None:
        if not hasattr(request, "session"):
            return None
        cached_session_id = request.session.get(self.cache_session_key)
        if cached_session_id != session_id:
            return None
        cached_user_info = request.session.get(self.cache_user_key)
        if isinstance(cached_user_info, dict) and cached_user_info:
            return cached_user_info
        return None

    def _session_provision_is_fresh(self, request, user_info: dict) -> bool:
        """Сессия уже привязана к этому же внешнему пользователю?

        Маркеры пишет сам middleware после успешного провижининга. Сессия
        серверная (подписана/в БД), поэтому маркеру можно доверять: подделать
        его без валидного DLSID нельзя, а сам DLSID всё равно проверяется
        внешним API не реже ``AI_AUTH_CACHE_TTL``.
        """
        if not hasattr(request, "session"):
            return False
        user = getattr(request, "user", None)
        if user is None or not getattr(user, "is_authenticated", False):
            return False
        user_pk = getattr(user, "pk", None)
        if user_pk is None:
            return False
        external_id = str((user_info or {}).get("userId") or "")
        if not external_id or external_id == "None":
            return False
        marker_external = request.session.get(self.provision_external_key)
        marker_user = request.session.get(self.provision_user_key)
        return marker_external == external_id and marker_user == user_pk

    def _attach_cached_user_info(self, request) -> None:
        if not hasattr(request, "session"):
            return
        cached_user_info = request.session.get(self.cache_user_key)
        if isinstance(cached_user_info, dict) and cached_user_info:
            request.user_info = cached_user_info

    def _cached_fetched_at(self, request) -> float:
        """Unix-таймштамп последнего успешного fetch_external_user_info (0 если нет)."""
        if not hasattr(request, "session"):
            return 0.0
        try:
            return float(request.session.get(self.cache_fetched_at_key, 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    def _store_cached_user_info(
        self, request, session_id: str, user_info: dict, fetched_at: float | None = None
    ) -> None:
        if not hasattr(request, "session"):
            return
        stamp = time.time() if fetched_at is None else float(fetched_at)
        changed = (
            request.session.get(self.cache_session_key) != session_id
            or request.session.get(self.cache_user_key) != user_info
            or request.session.get(self.cache_fetched_at_key) != stamp
        )
        request.session[self.cache_session_key] = session_id
        request.session[self.cache_user_key] = user_info
        request.session[self.cache_fetched_at_key] = stamp
        if changed:
            request.session.modified = True

    # ── Stale-while-revalidate (Django cache) ───────────────────────────────
    # DLSID в ключах не хранится: только sha256. Значение — последний
    # подтверждённый user_info + timestamp; сессия остаётся быстрым L1-кэшем,
    # Django cache — L2, переживающий потерю/рестарт сессии.
    def _cache_digest(self, session_id: str) -> str:
        return hashlib.sha256(str(session_id or "").encode("utf-8")).hexdigest()

    def _user_cache_key(self, session_id: str) -> str:
        return f"ai_auth_user:{self._cache_digest(session_id)}"

    def _refresh_lock_key(self, session_id: str) -> str:
        return f"ai_auth_refresh:{self._cache_digest(session_id)}"

    def _invalid_cache_key(self, session_id: str) -> str:
        return f"ai_auth_invalid:{self._cache_digest(session_id)}"

    def _cache_external_user_info(
        self, session_id: str, user_info: dict, fetched_at: float | None = None
    ) -> None:
        try:
            cache.set(
                self._user_cache_key(session_id),
                {
                    "user_info": user_info,
                    "fetched_at": time.time() if fetched_at is None else float(fetched_at),
                },
                timeout=self.auth_stale_ttl,
            )
        except Exception:  # Redis/LocMem сбой не должен ломать запрос
            logger.warning("Failed to store external user_info in Django cache", exc_info=True)

    def _load_external_user_info(self, session_id: str) -> tuple[dict | None, float]:
        try:
            payload = cache.get(self._user_cache_key(session_id))
        except Exception:
            logger.warning("Failed to read external user_info from Django cache", exc_info=True)
            return None, 0.0
        if not isinstance(payload, dict):
            return None, 0.0
        user_info = payload.get("user_info")
        if not isinstance(user_info, dict) or not user_info:
            return None, 0.0
        try:
            fetched_at = float(payload.get("fetched_at") or 0)
        except (TypeError, ValueError):
            fetched_at = 0.0
        return user_info, fetched_at

    def _read_cached_user_info(
        self, request, session_id: str
    ) -> tuple[dict | None, float]:
        """Лучший доступный кэш: сессия (L1) или Django cache (L2), кто свежее."""
        session_info = self._get_cached_user_info(request, session_id)
        session_at = self._cached_fetched_at(request) if session_info else 0.0
        cached_info, cached_at = self._load_external_user_info(session_id)
        if cached_info and (not session_info or cached_at > session_at):
            # Фоновое обновление успело раньше — подтянуть его в сессию
            # (её читает WS-consumer).
            self._store_cached_user_info(
                request, session_id, cached_info, fetched_at=cached_at
            )
            return cached_info, cached_at
        return session_info, session_at

    def _is_marked_invalid(self, session_id: str) -> bool:
        try:
            return bool(cache.get(self._invalid_cache_key(session_id)))
        except Exception:
            return False

    def _schedule_user_info_refresh(self, session_id: str) -> None:
        """Запустить фоновую ревалидацию DLSID, не блокируя запрос.

        Один refresh на DLSID: lock в Django cache не даёт толпе запросов
        (страница + параллельные API/ассеты) одновременно дёргать dl.gsu.by.
        """
        if not session_id:
            return
        lock_key = self._refresh_lock_key(session_id)
        try:
            if not cache.add(lock_key, 1, timeout=60):
                return
            thread = threading.Thread(
                target=self._refresh_user_info,
                args=(session_id, lock_key),
                name="ai-auth-refresh",
                daemon=True,
            )
            thread.start()
        except Exception:
            logger.warning("Failed to schedule external auth refresh", exc_info=True)
            try:
                cache.delete(lock_key)
            except Exception:
                pass

    def _refresh_user_info(self, session_id: str, lock_key: str) -> None:
        """Фоновая ревалидация: обновляет L2-кэш, сессию — на следующем запросе."""
        try:
            user_info = fetch_external_user_info(session_id, api_url=self.api_url)
            if isinstance(user_info, dict) and user_info:
                self._cache_external_user_info(session_id, user_info)
                try:
                    cache.delete(self._invalid_cache_key(session_id))
                except Exception:
                    pass
                logger.debug(
                    "External user_info refreshed in background (userId=%s)",
                    user_info.get("userId"),
                )
        except ExternalAuthUnauthorized:
            # Не логиним/редиректим из фонового потока: ставим tombstone,
            # следующий HTTP-запрос пойдёт блокирующим путём и обработает 401.
            try:
                cache.set(
                    self._invalid_cache_key(session_id),
                    1,
                    timeout=max(self.auth_cache_ttl, 60),
                )
            except Exception:
                pass
        except ExternalAuthUnavailable:
            logger.warning("Background DLSID revalidation failed: dl.gsu.by unavailable")
        except ExternalAuthMisconfigured as exc:
            logger.error(f"Background DLSID revalidation misconfigured: {exc}")
        except Exception:
            logger.exception("Background DLSID revalidation failed")
        finally:
            try:
                cache.delete(lock_key)
            except Exception:
                pass
            try:
                close_old_connections()
            except Exception:
                pass

    def _redirect_or_optional(self, request, request_path: str):
        """Return the response when the request is unauthenticated.

        Optional auth paths continue; everything else is redirected.
        """
        if _is_optional_auth_path(request_path):
            return self.get_response(request)
        return HttpResponseRedirect(self.redirect_url)

    def _provision_from_user_info(self, request, user_info: dict):
        """Провижинит и сессионно логинит пользователя по внешнему user_info.

        Общий «хвост» обычного пути и dev-bypass: get_or_create_user_from_external,
        перелогин сессии под подтверждённого пользователя (защита от stale-сессий),
        rotate CSRF, маркер admin_fresh_auth, request._ai_provisioned_user.
        """
        request.user_info = user_info

        # Быстрый путь: тот же DLSID уже провижинил этого же пользователя в этой
        # сессии (маркеры пишутся ниже, после успешного провижининга), а
        # user_info только что подтверждён/взят из свежего кэша. Пропускаем
        # SELECT по ExternalDLAccount и возможное блокирующее HTTP-обогащение
        # профиля. Смена userId в DLSID-кэше даёт несовпадение маркера → полный
        # путь с перепривязкой сессии.
        if self._session_provision_is_fresh(request, user_info):
            request._ai_provisioned_user = request.user
            return self.get_response(request)

        # Auto-provision user if needed. We do this for /ai/admin/...
        # paths too, even though the admin views have their own
        # permission checks: the admin's has_permission() verifies
        # that request.user matches the user the DLSID chain just
        # authenticated, and that rebind only works when we go all
        # the way through provisioning.
        try:
            user, created = get_or_create_user_from_external(user_info)
            if user:
                # Always rebind the session to the user the API just
                # confirmed. This is the only way to defend against
                # a stale Django session (e.g. a superuser from
                # yesterday) being reused under a different DLSID.
                current = getattr(request, "user", None)
                current_pk = getattr(current, "pk", None) if current is not None else None
                is_authed = bool(getattr(current, "is_authenticated", False))
                if not is_authed or current_pk != user.pk:
                    login(request, user, backend='django.contrib.auth.backends.ModelBackend')
                    csrf.rotate_token(request)
                    # Устанавливаем маркер свежей аутентификации для админки
                    request.session["admin_fresh_auth"] = True
                # Stash the freshly-provisioned user so downstream
                # code (notably AIAdminSite.has_permission) can verify
                # the request user matches the DLSID chain.
                request._ai_provisioned_user = user
                if hasattr(request, "session"):
                    request.session[self.provision_external_key] = str(
                        user_info.get("userId") or ""
                    )
                    request.session[self.provision_user_key] = user.pk
                    request.session.modified = True
                if created:
                    logger.info(f"New user provisioned: {user.username} (external_id={user_info.get('userId')})")
        except Exception as e:
            logger.exception(f"User provisioning failed: {e}")
            return JsonResponse({"error": "User provisioning failed"}, status=500)

        return self.get_response(request)

    def _process_dev_bypass(self, request, user_info: dict):
        """DEBUG-ONLY: провижинит и логинит локального fake-пользователя без DLSID.

        Все инварианты — как у обычного пути (request.user_info, login +
        rotate, _ai_provisioned_user); дополнительно кладёт
        session["external_user_info"] — тот же ключ кэша middleware, его
        читает WS-фолбэк consumers._resolve_course_id. В сессию
        external_session_id не пишем: DL-фичи не должны считать, что у
        запроса есть валидная DL-сессия.
        """
        if hasattr(request, "session"):
            request.session[self.cache_user_key] = user_info
            request.session.modified = True
        response = self._provision_from_user_info(request, user_info)
        # Локальное удобство: промоутим на каждый запрос (idempotent) —
        # bypass-юзер мог уже существовать (копия прод-БД, прошлый прогон).
        user = getattr(request, "_ai_provisioned_user", None)
        if user is not None and not (user.is_superuser and user.is_staff):
            user.is_superuser = True
            user.is_staff = True
            user.save(update_fields=["is_superuser", "is_staff"])
        # AIAdminSite.admin_view уводит на set-password при unusable
        # password — ручной шаг, которого dev-bypass избегает.
        if user is not None and not user.has_usable_password():
            user.set_password(os.getenv("AI_DEV_AUTH_PASSWORD", "dev-local"))
            user.save(update_fields=["password"])
        return response

    def __call__(self, request):
        # Пропуск путей
        request_path = _normalize_path(request.path)
        if self._is_skipped_path(request_path):
            return self.get_response(request)

        # An already-authenticated Django session does NOT mean the
        # request is fresh — the DLSID chain might have changed (user
        # signed in as someone else on dl.gsu.by) or a stale session
        # cookie from a different account could be sitting in the
        # browser. Always revalidate against the external API and
        # rebind the local session to the user the API just confirmed.
        raw_session_id = request.COOKIES.get(self.session_cookie_name)
        if not raw_session_id:
            # No DLSID at all — anything other than admin entry points
            # redirects to dl.gsu.by (или dev-bypass при DEBUG).
            if _dev_bypass_enabled():
                return self._process_dev_bypass(request, _dev_bypass_user_info())
            return self._redirect_or_optional(request, request_path)

        session_id = unquote(raw_session_id)
        logger.debug("Session ID decoded")

        try:
            # Tombstone после фонового 401: кэш не доверяем, идём блокирующим
            # путём — он сделает logout/redirect (или подтвердит, что DLSID снова
            # валиден, и снимет tombstone).
            marked_invalid = self._is_marked_invalid(session_id)
            cached_user_info = None
            fetched_at = 0.0
            if not marked_invalid:
                cached_user_info, fetched_at = self._read_cached_user_info(
                    request, session_id
                )
            if cached_user_info:
                # Stale-while-revalidate: отдаём кэш СРАЗУ; если он старше TTL —
                # обновляем DLSID в фоне, не задерживая рендер страницы.
                user_info = cached_user_info
                if self.auth_cache_ttl <= 0 or (
                    time.time() - fetched_at
                ) >= self.auth_cache_ttl:
                    self._schedule_user_info_refresh(session_id)
            else:
                # Кэша нет (или он помечен невалидным) — единственный
                # блокирующий путь к dl.gsu.by.
                user_info = fetch_external_user_info(session_id, api_url=self.api_url)
                self._store_cached_user_info(request, session_id, user_info)
                self._cache_external_user_info(session_id, user_info)
                try:
                    cache.delete(self._invalid_cache_key(session_id))
                except Exception:
                    pass
            logger.debug("External user_info fetched (userId=%s)", (user_info or {}).get("userId"))
        except ExternalAuthUnauthorized:
            # DLSID is no longer valid — drop the local session and
            # bounce the user back to dl.gsu.by so they re-authenticate.
            # Dev-bypass: не логаутим и не зацикливаемся на редиректе —
            # перелогинит fake-пользователем.
            try:
                cache.delete(self._user_cache_key(session_id))
                cache.set(
                    self._invalid_cache_key(session_id),
                    1,
                    timeout=max(self.auth_cache_ttl, 60),
                )
            except Exception:
                pass
            if _dev_bypass_enabled():
                return self._process_dev_bypass(request, _dev_bypass_user_info())
            from django.contrib.auth import logout as auth_logout
            if request.user.is_authenticated:
                auth_logout(request)
            return self._redirect_or_optional(request, request_path)
        except ExternalAuthMisconfigured as exc:
            logger.error(f"External auth misconfigured: {exc}")
            if _dev_bypass_enabled():
                return self._process_dev_bypass(request, _dev_bypass_user_info())
            if _is_optional_auth_path(request_path):
                return self.get_response(request)
            return JsonResponse(
                {"error": "Authentication service misconfigured"},
                status=500,
            )
        except ExternalAuthUnavailable as exc:
            # dl.gsu.by недоступен — падаем на stale-кэш (graceful degradation):
            # пользуемся последним подтверждённым user_info, не закрывая доступ.
            logger.error(f"Request to external API failed: {exc}")
            cached_user_info, _cached_at = self._read_cached_user_info(
                request, session_id
            )
            if cached_user_info:
                user_info = cached_user_info
            elif _dev_bypass_enabled():
                return self._process_dev_bypass(request, _dev_bypass_user_info())
            elif _is_optional_auth_path(request_path):
                return self.get_response(request)
            else:
                return JsonResponse({"error": "Authentication service unavailable"}, status=503)

        return self._provision_from_user_info(request, user_info)
