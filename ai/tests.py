# Тестовый файл: мок-атрибуты на request (``request.user_info = …``,
# ``request.user = SimpleNamespace(…)``) — штатная Django-идиома (атрибуты
# выставляет middleware/тест, тайп-чекер про них не знает). Глушим
# соответствующие проверки pyright/Pylance на весь файл.
# pyright: basic, reportAttributeAccessIssue=false, reportAssignmentIssue=false
from django.contrib.admin.sites import AdminSite
from django.contrib.messages.storage.cookie import CookieStorage
from django.contrib.auth import SESSION_KEY, get_user_model
from django.contrib.auth.models import AnonymousUser
from django.contrib.sessions.middleware import SessionMiddleware
from django.test import SimpleTestCase, RequestFactory, TestCase, TransactionTestCase, override_settings
from pathlib import Path
import json
import requests
from datetime import datetime, timedelta
import re
import tempfile
import threading
import time
import uuid

from django.http import HttpResponse
from django.http.response import Http404
from django.db import ProgrammingError
from django.utils import timezone
from asgiref.sync import async_to_sync, sync_to_async
from unittest.mock import AsyncMock, MagicMock, patch
from types import SimpleNamespace
import os

# Dev-bypass аутентификации выключен на весь тестовый процесс: прогон с
# обычными settings наследует локальный .env (load_dotenv в DjangoTest/settings.py),
# и без-DLSID тесты middleware провижинили бы fake-юзера вместо 302.
# DevAuthBypassTests включает флаг точечно через patch.dict.
os.environ["AI_DEV_AUTH_BYPASS"] = "0"

from ai.admin import PromptAdmin, PromptForm
from ai.middleware import ExternalAuthMiddleware
from ai.i18n import get_localized_name, get_ui_language_suffix
from ai.external_account import get_or_create_user_from_external
from ai.models import (
    AuthorAlias,
    AIDevTask,
    AIRequestLog,
    AIPinnedBatchRun,
    AIWizardSeen,
    ArmPromptBinding,
    ExternalDLAccount,
    ProgrammingLanguage,
    Prompt,
    SharedPrompt,
    Task,
    TaskSolution,
    Topic,
    UpdateLog,
)
from ai.services import (
    ConversationHistory,
    LogWriter,
    MessageComposer,
    ModelCaller,
    PromptResolver,
    get_user_identity_for_log,
)
from ai.throttling import RateLimiter, get_request_user_id, rate_limited
from ai.views import chat_view, get_problem_data, get_prompts, set_password_view
from ai.dl_api_client import _decode_response_json


def make_prompt_developer(user):
    """Зачисляет юзера во встроенную группу доступа «Разработчик промптов».

    Прежний способ — Django-Group `prompt_developer` — удалён: все проверки
    прав резолвят ключи AIAccessGroup (см. ai/admin/permissions.py).
    """
    from ai.admin.permissions import add_user_to_builtin_ai_group
    from ai.constants import BUILTIN_GROUP_PROMPT_DEV
    return add_user_to_builtin_ai_group(user, BUILTIN_GROUP_PROMPT_DEV)


def make_developer(user):
    """Зачисляет юзера во встроенную группу «Разработчик» (page.tasks и пр.)."""
    from ai.admin.permissions import add_user_to_builtin_ai_group
    from ai.constants import BUILTIN_GROUP_DEVELOPER
    return add_user_to_builtin_ai_group(user, BUILTIN_GROUP_DEVELOPER)


def make_ai_group_member(user, group_name):
    """Зачисляет юзера в произвольную группу доступа (обычно создана тестом)."""
    from ai.models import AIAccessGroup
    group, _ = AIAccessGroup.objects.get_or_create(name=group_name)
    user.ai_access_groups.add(group)
    return group


class ChatViewTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def _chat_request(self, user=None):
        request = self.factory.get("/ai/chat/")
        request.user = user if user is not None else SimpleNamespace(
            is_authenticated=True, is_active=True, username="chat-user",
        )
        request.session = {}
        request.user_info = {"userId": "chat-user"}
        request.COOKIES = {"userId": "chat-user"}
        return request

    @patch("ai.views.AIAppSettings.get_solo", side_effect=ProgrammingError)
    @patch("ai.views.get_available_model_options", return_value=[])
    def test_chat_view_does_not_fail_when_ai_settings_table_missing(self, _mock_models, _mock_get_solo):
        request = self._chat_request()
        with patch("ai.views.render", return_value=HttpResponse("ok")):
            response = chat_view(request)
        self.assertEqual(response.status_code, 200)

    @patch("ai.views.AIAppSettings.get_solo", return_value=SimpleNamespace(is_enabled=False))
    @patch("ai.views.get_available_model_options", return_value=[])
    def test_chat_view_returns_404_when_ai_app_disabled(self, _mock_models, _mock_get_solo):
        request = self._chat_request()
        response = chat_view(request)
        self.assertEqual(response.status_code, 404)

    def test_chat_view_requires_auth_or_uid(self):
        request = self.factory.get("/ai/chat/")
        request.user = SimpleNamespace(is_authenticated=False)
        request.session = {}
        response = chat_view(request)
        self.assertEqual(response.status_code, 403)

    def test_chat_view_requires_matching_user_info(self):
        # No user_info at all → 403.
        request = self.factory.get("/ai/chat/")
        request.user = SimpleNamespace(is_authenticated=True, is_active=True, username="alice")
        request.session = {}
        request.user_info = None
        request.COOKIES = {}
        with patch("ai.views.AIAppSettings.get_solo", return_value=SimpleNamespace(is_enabled=True)), \
             patch("ai.views.get_available_model_options", return_value=[]):
            response = chat_view(request)
        self.assertEqual(response.status_code, 403)

    def test_chat_view_rejects_session_mismatch(self):
        # Authenticated Django session but no DLSID / DLID / uid at all
        # on the request → 403.
        request = self.factory.get("/ai/chat/")
        request.user = SimpleNamespace(is_authenticated=True, is_active=True, username="alice")
        request.session = {}
        request.user_info = None
        request.COOKIES = {}
        with patch("ai.views.AIAppSettings.get_solo", return_value=SimpleNamespace(is_enabled=True)), \
             patch("ai.views.get_available_model_options", return_value=[]):
            response = chat_view(request)
        self.assertEqual(response.status_code, 403)


@override_settings(SESSION_ENGINE="django.contrib.sessions.backends.signed_cookies")
class ExternalAuthMiddlewareTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.middleware = ExternalAuthMiddleware(lambda req: HttpResponse("ok"))
        self.user_model = get_user_model()

    def _add_session(self, request):
        SessionMiddleware(lambda req: None).process_request(request)

    def test_test_panel_login_no_longer_skips_external_auth(self):
        request = self.factory.get("/ai/test-panel/login/")
        self._add_session(request)

        response = self.middleware(request)

        # Middleware now treats /ai/test-panel/login/ as a regular path:
        # no DLSID, no user_info → 302 redirect to dl.gsu.by.
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "https://dl.gsu.by")

    def test_authenticated_user_without_dlsid_is_redirected(self):
        # A live Django session alone is NOT enough — the user must
        # also have a DLSID. Otherwise a stale session cookie from a
        # superuser would grant access under someone else's identity.
        request = self.factory.get("/ai/chat/")
        self._add_session(request)
        request.user = SimpleNamespace(is_authenticated=True, pk=1)

        response = self.middleware(request)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "https://dl.gsu.by")

    def test_session_is_rebound_when_dlsid_belongs_to_different_user(self):
        # Stale Django session belongs to pk=1 (e.g. a former superuser),
        # but the current DLSID authenticates user 42. Middleware must
        # rebind the session so the local user matches the external one.
        from django.contrib.auth.models import User
        from ai.models import ExternalDLAccount

        stale = User.objects.create_user(
            username="stale-superuser",
            password="x",
            is_superuser=True,
            is_staff=True,
        )
        request = self.factory.get("/ai/chat/")
        self._add_session(request)
        request.user = stale
        request.COOKIES["DLSID"] = "session-123"
        # Fresh user so the real provisioning path (not a mock) runs
        # end-to-end; базовую группу создаст сам провижининг.
        fresh = User.objects.create_user(username="alice", password="x")
        ExternalDLAccount.objects.create(user=fresh, external_user_id="42")

        with patch(
            "ai.middleware.fetch_external_user_info",
            return_value={"userId": 42, "login": "alice", "firstName": "Alice"},
        ):
            response = self.middleware(request)

        self.assertEqual(response.status_code, 200)
        # After middleware the request must be bound to the fresh user,
        # not the stale superuser.
        self.assertEqual(request.user.pk, fresh.pk)
        self.assertEqual(request.user.username, "alice")

    def test_cached_user_info_skips_external_call(self):
        # Even for admin paths the middleware now provisions a local user.
        from ai.models import ExternalDLAccount
        u = self.user_model.objects.create_user(username="user_42", password="x")
        ExternalDLAccount.objects.create(user=u, external_user_id="42")

        request = self.factory.get("/ai/admin/")
        self._add_session(request)
        request.COOKIES["DLSID"] = "session-123"
        request.session["external_session_id"] = "session-123"
        request.session["external_user_info"] = {"userId": "42"}
        # Свежий кэш (< TTL) — ревалидация DLSID не требуется.
        request.session["external_user_info_fetched_at"] = time.time()

        with patch("ai.middleware.fetch_external_user_info") as fetch_user_info:
            response = self.middleware(request)

        self.assertEqual(response.status_code, 200)
        fetch_user_info.assert_not_called()
        self.assertEqual(request.user_info, {"userId": "42"})

    def test_stale_cached_user_info_revalidates_dlsid(self):
        # Кэш старше TTL → middleware заново проверяет DLSID через внешний API.
        from ai.models import ExternalDLAccount
        u = self.user_model.objects.create_user(username="user_42b", password="x")
        ExternalDLAccount.objects.create(user=u, external_user_id="42")

        request = self.factory.get("/ai/admin/")
        self._add_session(request)
        request.COOKIES["DLSID"] = "session-123"
        request.session["external_session_id"] = "session-123"
        request.session["external_user_info"] = {"userId": "42"}
        # Протухший кэш: fetched_at далеко в прошлом → (now - fetched_at) >= TTL.
        request.session["external_user_info_fetched_at"] = time.time() - 3600

        with patch(
            "ai.middleware.fetch_external_user_info",
            return_value={"userId": "42", "login": "alice"},
        ) as fetch_user_info:
            response = self.middleware(request)

        self.assertEqual(response.status_code, 200)
        fetch_user_info.assert_called_once()
        # Кэш обновлён свежим user_info и свежим timestamp.
        self.assertEqual(request.session["external_user_info"], {"userId": "42", "login": "alice"})

    def test_unavailable_external_api_falls_back_to_stale_cache(self):
        # dl.gsu.by недоступен, но есть свежий кэш → graceful degradation,
        # доступ сохраняется (не 503).
        from ai.models import ExternalDLAccount
        from ai.external_auth import ExternalAuthUnavailable
        u = self.user_model.objects.create_user(username="user_42c", password="x")
        ExternalDLAccount.objects.create(user=u, external_user_id="42")

        request = self.factory.get("/ai/admin/")
        self._add_session(request)
        request.COOKIES["DLSID"] = "session-123"
        request.session["external_session_id"] = "session-123"
        request.session["external_user_info"] = {"userId": "42"}
        # Протухший кэш провоцирует ревалидацию, которая упадёт с Unavailable.
        request.session["external_user_info_fetched_at"] = time.time() - 3600

        with patch(
            "ai.middleware.fetch_external_user_info",
            side_effect=ExternalAuthUnavailable("dl.gsu.by down"),
        ):
            response = self.middleware(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(request.user_info, {"userId": "42"})

    def test_unavailable_external_api_returns_503_without_cache(self):
        # dl.gsu.by недоступен И кэша нет → 503 (некому предоставить доступ).
        from ai.external_auth import ExternalAuthUnavailable

        request = self.factory.get("/ai/chat/")
        self._add_session(request)
        request.COOKIES["DLSID"] = "session-123"

        with patch(
            "ai.middleware.fetch_external_user_info",
            side_effect=ExternalAuthUnavailable("dl.gsu.by down"),
        ):
            response = self.middleware(request)

        self.assertEqual(response.status_code, 503)


class DevAuthBypassTests(TestCase):
    """AI_DEV_AUTH_BYPASS: fake-суперпользователь без DLSID, только при DEBUG.

    Инварианты bypass: request.user_info + session["external_user_info"]
    (WS-фолбэк), login + rotate CSRF, _ai_provisioned_user, usable password
    (иначе admin_view уводит на set-password). При DEBUG=False или без флага
    middleware ведёт себя как раньше (302).
    """

    def setUp(self):
        self.factory = RequestFactory()
        self.middleware = ExternalAuthMiddleware(lambda req: HttpResponse("ok"))
        self.user_model = get_user_model()
        # Базовое состояние — флаг выключен; отдельные тесты включают точечно.
        env = patch.dict(os.environ, {"AI_DEV_AUTH_BYPASS": ""})
        env.start()
        self.addCleanup(env.stop)

    def _add_session(self, request):
        SessionMiddleware(lambda req: None).process_request(request)

    @override_settings(DEBUG=True)
    def test_bypass_provisions_local_superuser_without_dlsid(self):
        request = self.factory.get("/ai/chat/")
        self._add_session(request)
        request.user = AnonymousUser()

        with patch.dict(os.environ, {"AI_DEV_AUTH_BYPASS": "1"}):
            response = self.middleware(request)

        self.assertEqual(response.status_code, 200)
        user = request._ai_provisioned_user
        self.assertTrue(user.is_authenticated)
        self.assertTrue(user.is_superuser)
        self.assertTrue(user.is_staff)
        self.assertEqual(user.username, "dev_admin")
        self.assertTrue(user.has_usable_password())
        self.assertEqual(request.user_info["userId"], "999999")
        self.assertEqual(request.user.pk, user.pk)
        self.assertTrue(ExternalDLAccount.objects.filter(external_user_id="999999").exists())
        self.assertEqual(request.session["external_user_info"]["userId"], "999999")
        self.assertTrue(request.session["admin_fresh_auth"])

    @override_settings(DEBUG=True)
    def test_bypass_reuses_existing_user(self):
        # Два запроса с независимыми сессиями — один и тот же юзер,
        # дублей username нет, права сохраняются.
        with patch.dict(os.environ, {"AI_DEV_AUTH_BYPASS": "1"}):
            first = self.factory.get("/ai/chat/")
            self._add_session(first)
            first.user = AnonymousUser()
            self.middleware(first)

            second = self.factory.get("/ai/chat/")
            self._add_session(second)
            second.user = AnonymousUser()
            self.middleware(second)

        self.assertEqual(self.user_model.objects.filter(username="dev_admin").count(), 1)
        self.assertEqual(second._ai_provisioned_user.pk, first._ai_provisioned_user.pk)
        self.assertTrue(second._ai_provisioned_user.is_superuser)

    @override_settings(DEBUG=True)
    def test_bypass_promotes_existing_non_superuser(self):
        # Юзер dev_admin уже существовал (копия БД / прошлый прогон) со своим
        # ExternalDLAccount на 999999, но без прав — bypass повышает его
        # (промоут idempotent, не только при created=True).
        existed = self.user_model.objects.create_user(username="dev_admin", password="x")
        ExternalDLAccount.objects.create(user=existed, external_user_id="999999")
        request = self.factory.get("/ai/chat/")
        self._add_session(request)
        request.user = AnonymousUser()

        with patch.dict(os.environ, {"AI_DEV_AUTH_BYPASS": "1"}):
            self.middleware(request)

        user = self.user_model.objects.get(username="dev_admin")
        self.assertEqual(request._ai_provisioned_user.pk, existed.pk)
        self.assertTrue(user.is_superuser)
        self.assertTrue(user.is_staff)

    @override_settings(DEBUG=False)
    def test_bypass_inert_when_debug_false(self):
        # Prod-защита: любой флаг при DEBUG=False игнорируется — редирект как раньше.
        request = self.factory.get("/ai/chat/")
        self._add_session(request)
        request.user = AnonymousUser()

        with patch.dict(os.environ, {"AI_DEV_AUTH_BYPASS": "1"}):
            response = self.middleware(request)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "https://dl.gsu.by")
        self.assertEqual(ExternalDLAccount.objects.count(), 0)

    def test_bypass_inert_without_flag(self):
        request = self.factory.get("/ai/chat/")
        self._add_session(request)
        request.user = AnonymousUser()

        with override_settings(DEBUG=True):
            response = self.middleware(request)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(ExternalDLAccount.objects.count(), 0)

    @override_settings(DEBUG=True)
    def test_bypass_does_not_shadow_valid_dlsid_session(self):
        # Валидная DLSID-кука — обычный путь, bypass не мешает и
        # ничего не повышает в правах.
        request = self.factory.get("/ai/chat/")
        self._add_session(request)
        request.user = AnonymousUser()
        request.COOKIES["DLSID"] = "session-123"

        with patch.dict(os.environ, {"AI_DEV_AUTH_BYPASS": "1"}):
            with patch(
                "ai.middleware.fetch_external_user_info",
                return_value={"userId": "77", "login": "alice", "firstName": "Alice"},
            ):
                response = self.middleware(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(request.user_info["userId"], "77")
        self.assertFalse(request.user.is_superuser)
        self.assertFalse(ExternalDLAccount.objects.filter(external_user_id="999999").exists())

    @override_settings(DEBUG=True)
    def test_bypass_fallback_on_dlsid_unauthorized(self):
        from ai.external_auth import ExternalAuthUnauthorized

        request = self.factory.get("/ai/chat/")
        self._add_session(request)
        request.user = AnonymousUser()
        request.COOKIES["DLSID"] = "stale-session"

        with patch.dict(os.environ, {"AI_DEV_AUTH_BYPASS": "1"}):
            with patch(
                "ai.middleware.fetch_external_user_info",
                side_effect=ExternalAuthUnauthorized("invalid DLSID"),
            ):
                response = self.middleware(request)

        # Протухшая DLSID не зацикливает на редиректе: bypass перелогинит
        # fake-пользователем, локальная сессия не сломана.
        self.assertEqual(response.status_code, 200)
        self.assertTrue(request.user.is_authenticated)
        self.assertEqual(request.user_info["userId"], "999999")

    @override_settings(DEBUG=True)
    def test_bypass_end_to_end_chat_and_admin(self):
        # Интеграционно, через реальный стек middleware: чат отвечает без
        # куки, админка не уводит на login/set-password/dl.gsu.by.
        # Триггер self-heal свипа глушим: он стартует поток с отдельным
        # соединением, которое коммитит AIModelHealthRun(window_date=сегодня)
        # в обход TestCase-rollback и ломает последующие ModelHealth-тесты.
        with patch.dict(os.environ, {"AI_DEV_AUTH_BYPASS": "1"}):
            with patch("ai.views.trigger_model_health_refresh_async"):
                chat = self.client.get("/ai/chat/")
                self.assertEqual(chat.status_code, 200)

                admin_page = self.client.get("/ai/admin/")
                self.assertEqual(admin_page.status_code, 200)
                self.assertNotEqual(admin_page.get("Location", ""), "/ai/admin/set-password/")
                self.assertNotIn("dl.gsu.by", admin_page.get("Location", ""))

    @override_settings(DEBUG=True)
    def test_bypass_does_not_affect_skipped_paths(self):
        # /health в skip-листе — провижининга нет, пользователь анонимный.
        request = self.factory.get("/health")
        self._add_session(request)
        request.user = AnonymousUser()

        with patch.dict(os.environ, {"AI_DEV_AUTH_BYPASS": "1"}):
            response = self.middleware(request)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(request.user.is_authenticated)
        self.assertEqual(ExternalDLAccount.objects.count(), 0)


class AdminExternalAuthTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user_model = get_user_model()

    def _request(self, method="get", path="/ai/admin/", data=None, user_id="12345"):
        request = getattr(self.factory, method)(path, data=data or {})
        SessionMiddleware(lambda req: None).process_request(request)
        request.user = AnonymousUser()
        request.user_info = {"userId": user_id}
        return request

    def test_has_permission_rejects_anonymous(self):
        from ai.admin.site import ai_admin_site
        request = self._request()
        self.assertFalse(ai_admin_site.has_permission(request))

    def test_has_permission_rejects_session_user_mismatch(self):
        from ai.admin.site import ai_admin_site
        user = self.user_model.objects.create_user(
            username="other-user",
            password="initial-pass",
        )
        # Request has no user_info, no uid, no DLID cookie — i.e. the
        # DLSID chain is broken. has_permission must refuse, regardless
        # of the local session.
        request = self._request(user_id=None)
        request.user = user
        self.assertFalse(ai_admin_site.has_permission(request))

    def test_has_permission_rejects_stale_session_under_other_dlsid(self):
        # The local session is bound to user 99, but the DLSID chain
        # on the current request authenticates user 12345. This is
        # exactly the cross-account bug: a stale superuser session
        # must NOT grant access on someone else's DLSID.
        from ai.admin.site import ai_admin_site
        stale = self.user_model.objects.create_user(
            username="99",
            password="initial-pass",
            is_superuser=True,
            is_staff=True,
        )
        legit = self.user_model.objects.create_user(username="12345", password="initial-pass")
        make_prompt_developer(legit)
        request = self._request(user_id="12345")
        request.user = stale
        self.assertFalse(ai_admin_site.has_permission(request))

    def test_has_permission_accepts_matching_prompt_developer(self):
        from ai.admin.site import ai_admin_site
        from ai.models import ExternalDLAccount
        user = self.user_model.objects.create_user(
            username="12345",
            password="initial-pass",
        )
        make_prompt_developer(user)
        ExternalDLAccount.objects.create(user=user, external_user_id="12345")
        request = self._request(user_id="12345")
        request.user = user
        self.assertTrue(ai_admin_site.has_permission(request))

    def test_get_admin_user_uses_external_account_not_username(self):
        """A username matching the external id must not win over ExternalDLAccount."""
        from ai.auth_backends import get_admin_user_by_external_id
        from ai.models import ExternalDLAccount

        # A colliding username with a password: the old lookup returned this
        # user and caused an admin <-> set-password redirect loop.
        self.user_model.objects.create_user(username="186638", password="pass-123")
        # The real user mapped by the external API.
        real_user = self.user_model.objects.create_user(username="real-186638")
        ExternalDLAccount.objects.create(user=real_user, external_user_id="186638")

        resolved = get_admin_user_by_external_id("186638")
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.pk, real_user.pk)

    def test_new_external_user_sets_password_once_and_is_created(self):
        request = self._request(
            method="post",
            path="/ai/admin/set-password/",
            data={
                "next": "/ai/admin/",
                "new_password": "strong-pass-123",
                "new_password_confirm": "strong-pass-123",
            },
            user_id="67890",
        )

        response = set_password_view(request)

        user = self.user_model.objects.get(username="67890")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/ai/admin/")
        self.assertTrue(user.check_password("strong-pass-123"))
        self.assertEqual(request.session[SESSION_KEY], str(user.pk))

    def test_admin_external_users_are_assigned_prompt_developer_group(self):
        request = self._request(
            method="post",
            path="/ai/admin/set-password/",
            data={
                "next": "/ai/admin/",
                "new_password": "strong-pass-123",
                "new_password_confirm": "strong-pass-123",
            },
            user_id="24680",
        )

        set_password_view(request)

        # Зачисление при первой установке пароля — во встроенную группу
        # доступа «Разработчик промптов» (Django-Group больше не используется).
        from ai.constants import BUILTIN_GROUP_PROMPT_DEV
        from ai.models import AIAccessGroup

        group = AIAccessGroup.objects.get(name=BUILTIN_GROUP_PROMPT_DEV)
        user = self.user_model.objects.get(username="24680")
        self.assertTrue(user.ai_access_groups.filter(pk=group.pk).exists())

    def test_mapped_external_user_without_password_sets_password_on_existing_user(self):
        user = self.user_model.objects.create_user(username="external-login")
        user.set_unusable_password()
        user.save(update_fields=["password"])
        ExternalDLAccount.objects.create(
            user=user,
            external_user_id="13579",
            external_login="external-login",
        )
        request = self._request(
            method="post",
            path="/ai/admin/set-password/",
            data={
                "next": "/ai/admin/ai/prompt/add/",
                "new_password": "strong-pass-123",
                "new_password_confirm": "strong-pass-123",
            },
            user_id="13579",
        )

        response = set_password_view(request)
        user.refresh_from_db()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/ai/admin/ai/prompt/add/")
        self.assertTrue(user.check_password("strong-pass-123"))
        self.assertEqual(self.user_model.objects.filter(username="13579").count(), 0)


class ExternalAccountStudentInfoTests(TestCase):
    """Email и блок education из get-user-info (dl-rest-api.json) сохраняются
    в ExternalDLAccount: при создании и при повторном входе."""

    def setUp(self):
        self.user_model = get_user_model()

    @staticmethod
    def _payload(user_id="42", email="student@gsu.by", education=None):
        payload = {
            "userId": user_id,
            "login": f"user{user_id}",
            "firstName": "Иван",
            "lastName": "Иванов",
        }
        if email is not None:
            payload["email"] = email
        if education is not None:
            payload["education"] = education
        return payload

    def test_create_stores_email_and_education(self):
        user, created = get_or_create_user_from_external(self._payload(
            education={
                "form": "очная",
                "schoolId": "10",
                "schoolKind": "вуз",
                "schoolNo": "1234",
                "groupMaskId": "Г-2026-1",
                "formLetter": "Б",
            },
        ))
        self.assertTrue(created)
        account = ExternalDLAccount.objects.get(external_user_id="42")
        self.assertEqual(account.dl_email, "student@gsu.by")
        self.assertEqual(account.education_form, "очная")
        self.assertEqual(account.education_school_id, "10")
        self.assertEqual(account.education_school_kind, "вуз")
        self.assertEqual(account.education_school_no, "1234")
        self.assertEqual(account.education_group_mask_id, "Г-2026-1")
        self.assertEqual(account.education_form_letter, "Б")

    def test_relogin_updates_student_info(self):
        get_or_create_user_from_external(self._payload(
            email="old@gsu.by",
            education={"form": "очная", "schoolNo": "1", "formLetter": "А"},
        ))
        get_or_create_user_from_external(self._payload(
            email="new@gsu.by",
            education={
                "form": "заочная",
                "schoolId": "10",
                "schoolKind": "вуз",
                "schoolNo": "1234",
                "groupMaskId": "Г-2027-2",
                "formLetter": "В",
            },
        ))

        account = ExternalDLAccount.objects.get(external_user_id="42")
        self.assertEqual(account.dl_email, "new@gsu.by")
        self.assertEqual(account.education_form, "заочная")
        self.assertEqual(account.education_school_no, "1234")
        self.assertEqual(account.education_group_mask_id, "Г-2027-2")
        self.assertEqual(account.education_form_letter, "В")

    def test_relogin_empty_email_does_not_wipe_stored_value(self):
        # DL может не отдать email/education в каком-то ответе — пустое
        # значение из API не затирает то, что уже сохранено.
        get_or_create_user_from_external(self._payload(
            email="keep@gsu.by",
            education={"form": "очная", "schoolNo": "5"},
        ))
        get_or_create_user_from_external(self._payload(email=None, education=None))

        account = ExternalDLAccount.objects.get(external_user_id="42")
        self.assertEqual(account.dl_email, "keep@gsu.by")
        self.assertEqual(account.education_form, "очная")
        self.assertEqual(account.education_school_no, "5")

    def test_missing_email_and_education_leave_blank_fields(self):
        get_or_create_user_from_external(self._payload(email=None, education=None))

        account = ExternalDLAccount.objects.get(external_user_id="42")
        self.assertEqual(account.dl_email, "")
        self.assertEqual(account.education_form, "")
        self.assertEqual(account.education_school_id, "")
        self.assertEqual(account.education_school_kind, "")
        self.assertEqual(account.education_school_no, "")
        self.assertEqual(account.education_group_mask_id, "")
        self.assertEqual(account.education_form_letter, "")

    def test_non_dict_education_is_ignored(self):
        get_or_create_user_from_external(self._payload(education="мусор"))

        account = ExternalDLAccount.objects.get(external_user_id="42")
        self.assertEqual(account.education_form, "")


class AdminPermissionsTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user_model = get_user_model()

    def test_prompt_developer_cannot_view_shared_prompt_module(self):
        from ai.admin.models import SharedPromptAdmin
        from django.contrib.admin.sites import AdminSite
        user = self.user_model.objects.create_user(username="alice", password="x")
        make_prompt_developer(user)
        request = self.factory.get("/ai/admin/ai/sharedprompt/")
        request.user = user
        admin = SharedPromptAdmin(SharedPrompt, AdminSite())
        self.assertFalse(admin.has_module_permission(request))

    def test_staff_user_can_view_shared_prompt_module(self):
        from ai.admin.models import SharedPromptAdmin
        from django.contrib.admin.sites import AdminSite
        user = self.user_model.objects.create_user(
            username="bob", password="x", is_staff=True,
        )
        request = self.factory.get("/ai/admin/ai/sharedprompt/")
        request.user = user
        admin = SharedPromptAdmin(SharedPrompt, AdminSite())
        self.assertTrue(admin.has_module_permission(request))

    def test_app_list_hides_staff_only_models_for_prompt_developer(self):
        from ai.admin.site import ai_admin_site
        from ai.admin.permissions import filter_app_list_for_user
        from ai.models import Prompt as PromptModel, SharedPrompt as SharedPromptModel
        from django.contrib.auth import get_user_model as get_user
        User = get_user()
        user = self.user_model.objects.create_user(username="carol", password="x")
        make_prompt_developer(user)
        request = self.factory.get("/ai/admin/")
        request.user = user
        request._ai_admin_registry = ai_admin_site._registry
        app_list = [
            {
                "app_label": "ai",
                "name": "AI",
                "app_url": "/ai/admin/ai/",
                "models": [
                    {"object_name": "Prompt", "name": "Prompt",
                     "admin_url": "/ai/admin/ai/prompt/", "add_url": "",
                     "view_only": False, "_model_cls": PromptModel},
                    {"object_name": "SharedPrompt", "name": "SharedPrompt",
                     "admin_url": "/ai/admin/ai/sharedprompt/", "add_url": "",
                     "view_only": False, "_model_cls": SharedPromptModel},
                ],
            },
            {
                "app_label": "auth",
                "name": "Auth",
                "app_url": "/ai/admin/auth/",
                "models": [
                    {"object_name": "User", "name": "User",
                     "admin_url": "/ai/admin/auth/user/", "add_url": "",
                     "view_only": False, "_model_cls": User},
                ],
            },
        ]
        filtered = filter_app_list_for_user(app_list, request)
        # Non-AI app (auth) without a custom link for this user must be dropped.
        labels = [app["app_label"] for app in filtered]
        self.assertNotIn("auth", labels)
        # The AI app must keep Prompt, drop SharedPrompt.
        ai_app = next(app for app in filtered if app["app_label"] == "ai")
        names = [m["object_name"] for m in ai_app["models"]]
        self.assertIn("Prompt", names)
        self.assertNotIn("SharedPrompt", names)


class PromptAdminAccessTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.prompt_admin = PromptAdmin(Prompt, AdminSite())
        user_model = get_user_model()

        self.staff_user = user_model.objects.create_user(
            username="staff_user",
            password="test-pass",
            is_staff=True,
        )
        self.prompt_developer = user_model.objects.create_user(
            username="prompt_dev",
            password="test-pass",
        )
        self.second_prompt_developer = user_model.objects.create_user(
            username="prompt_dev_second",
            password="test-pass",
        )
        self.other_prompt_developer = user_model.objects.create_user(
            username="prompt_dev_other",
            password="test-pass",
        )

        make_prompt_developer(self.prompt_developer)
        make_prompt_developer(self.second_prompt_developer)
        make_prompt_developer(self.other_prompt_developer)

        self.editable_prompt = Prompt.objects.create(
            prompt_name_ru="Editable prompt",
            prompt_text_ru="Editable prompt text",
            owner=self.prompt_developer,
        )
        self.readonly_prompt = Prompt.objects.create(
            prompt_name_ru="Readonly prompt",
            prompt_text_ru="Readonly prompt text",
            owner=self.other_prompt_developer,
        )
        self.legacy_assigned_prompt = Prompt.objects.create(
            prompt_name_ru="Legacy assigned prompt",
            prompt_text_ru="Legacy assigned prompt text",
        )
        self.editable_prompt.editors.add(self.prompt_developer)
        self.legacy_assigned_prompt.editors.add(self.prompt_developer)

    def _build_request(self, user, query_params=None):
        query_params = query_params or {}
        request = self.factory.get("/ai/admin/ai/prompt/", data=query_params)
        request.user = user
        request.user_info = {"userId": user.username}
        request.COOKIES = {"userId": user.username}
        return request

    def test_prompt_developer_can_edit_owned_or_assigned_prompt(self):
        request = self._build_request(self.prompt_developer)

        self.assertTrue(self.prompt_admin.has_change_permission(request, self.editable_prompt))
        self.assertTrue(self.prompt_admin.has_change_permission(request, self.legacy_assigned_prompt))
        self.assertFalse(self.prompt_admin.has_change_permission(request, self.readonly_prompt))
        self.assertTrue(self.prompt_admin.has_view_permission(request, self.readonly_prompt))
        self.assertTrue(self.prompt_admin.has_delete_permission(request, self.editable_prompt))
        self.assertFalse(self.prompt_admin.has_delete_permission(request, self.readonly_prompt))

    def test_prompt_developer_can_add_prompt_and_becomes_owner(self):
        request = self._build_request(self.prompt_developer)
        self.assertTrue(self.prompt_admin.has_add_permission(request))

        new_prompt = Prompt(prompt_name_ru="My prompt", prompt_text_ru="My prompt text")
        self.prompt_admin.save_model(request, new_prompt, form=None, change=False)
        new_prompt.refresh_from_db()

        self.assertEqual(new_prompt.owner_id, self.prompt_developer.id)
        self.assertTrue(new_prompt.editors.filter(pk=self.prompt_developer.pk).exists())

    def test_second_prompt_developer_can_add_and_own_prompt(self):
        request = self._build_request(self.second_prompt_developer)
        self.assertTrue(self.prompt_admin.has_add_permission(request))

        prompt = Prompt(prompt_name_ru="Second prompt", prompt_text_ru="Second prompt text")
        self.prompt_admin.save_model(request, prompt, form=None, change=False)
        prompt.refresh_from_db()

        self.assertEqual(prompt.owner_id, self.second_prompt_developer.id)
        self.assertTrue(prompt.editors.filter(pk=self.second_prompt_developer.pk).exists())

    def test_prompt_developer_fields_are_readonly_for_foreign_prompt(self):
        request = self._build_request(self.prompt_developer)

        readonly_fields = self.prompt_admin.get_readonly_fields(request, self.readonly_prompt)
        editable_fields = self.prompt_admin.get_readonly_fields(request, self.editable_prompt)

        self.assertEqual(editable_fields, ())
        self.assertEqual(
            readonly_fields,
            (
                "programming_language", "topic",
                "prompt_name_ru", "prompt_name_en", "prompt_name_fr",
                "shared_prompt", "prompt_text_override",
                "prompt_text_ru", "prompt_text_en", "prompt_text_fr",
            ),
        )

    def test_prompt_developer_queryset_shows_all_prompts(self):
        request = self._build_request(self.prompt_developer)
        prompt_ids = set(self.prompt_admin.get_queryset(request).values_list("id", flat=True))

        self.assertEqual(
            prompt_ids,
            {
                self.editable_prompt.id,
                self.readonly_prompt.id,
                self.legacy_assigned_prompt.id,
            },
        )

    def test_prompt_developer_queryset_can_filter_mine(self):
        request = self._build_request(self.prompt_developer, query_params={"mine": "1"})
        prompt_ids = set(self.prompt_admin.get_queryset(request).values_list("id", flat=True))

        self.assertEqual(prompt_ids, {self.editable_prompt.id, self.legacy_assigned_prompt.id})

    def test_staff_user_sees_only_own_prompts(self):
        request = self._build_request(self.staff_user)

        self.assertTrue(self.prompt_admin.has_add_permission(request))
        self.assertFalse(self.prompt_admin.has_change_permission(request, self.readonly_prompt))
        self.assertTrue(self.prompt_admin.has_view_permission(request, self.readonly_prompt))

    def test_get_prompts_api_returns_only_current_user_prompts(self):
        request = self._build_request(self.prompt_developer)
        response = get_prompts(request)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Editable prompt")
        self.assertContains(response, "Readonly prompt")
        self.assertContains(response, "Legacy assigned prompt")

    def test_superuser_queryset_shows_all_prompts(self):
        superuser = get_user_model().objects.create_superuser(
            username="super_user",
            password="test-pass",
        )
        own_prompt = Prompt.objects.create(
            prompt_name_ru="Superuser prompt",
            prompt_text_ru="Superuser prompt text",
            owner=superuser,
        )
        request = self._build_request(superuser)
        prompt_ids = set(self.prompt_admin.get_queryset(request).values_list("id", flat=True))

        self.assertEqual(
            prompt_ids,
            {
                self.editable_prompt.id,
                self.readonly_prompt.id,
                self.legacy_assigned_prompt.id,
                own_prompt.id,
            },
        )
        self.assertTrue(self.prompt_admin.has_view_permission(request, self.readonly_prompt))
        self.assertTrue(self.prompt_admin.has_change_permission(request, self.readonly_prompt))
        self.assertTrue(self.prompt_admin.has_change_permission(request, own_prompt))
        self.assertTrue(self.prompt_admin.has_delete_permission(request, self.readonly_prompt))
        self.assertTrue(self.prompt_admin.has_delete_permission(request, own_prompt))
        self.assertEqual(self.prompt_admin.get_readonly_fields(request, self.readonly_prompt), ())

    def test_superuser_queryset_can_filter_mine(self):
        superuser = get_user_model().objects.create_superuser(
            username="super_user_mine",
            password="test-pass",
        )
        own_prompt = Prompt.objects.create(
            prompt_name_ru="Superuser prompt mine",
            prompt_text_ru="Superuser prompt text",
            owner=superuser,
        )
        request = self._build_request(superuser, query_params={"mine": "1"})
        # «Мои» — намерение юзера, не ACL (2026-10-19: с baseline
        # «Видеть все промпты» фильтр mine должен работать у РОЛИ ЛЮБОГО
        # уровня, включая суперпользователя).
        prompt_ids = set(self.prompt_admin.get_queryset(request).values_list("id", flat=True))

        self.assertEqual(prompt_ids, {own_prompt.id})


class PromptFormTests(TestCase):
    def setUp(self):
        self.python_language = ProgrammingLanguage.objects.create(language_name="Python")
        self.c_language = ProgrammingLanguage.objects.create(language_name="C")
        self.python_topic = Topic.objects.create(
            topic_name_ru="Loops",
            programming_language=self.python_language,
        )
        self.c_topic = Topic.objects.create(
            topic_name_ru="Pointers",
            programming_language=self.c_language,
        )

    def test_form_sets_programming_language_from_prompt_topic(self):
        prompt = Prompt.objects.create(
            topic=self.python_topic,
            prompt_name_ru="Prompt",
            prompt_text_ru="Body",
        )

        form = PromptForm(instance=prompt)

        self.assertEqual(form.fields["programming_language"].initial, self.python_language.id)
        self.assertQuerySetEqual(
            form.fields["topic"].queryset,
            [self.python_topic],
            transform=lambda item: item,
        )

    def test_form_filters_topics_by_selected_language(self):
        # mode обязателен в форме с «Разделения промптов по режимам» (132a3ef):
        # без него form.is_valid() падает на 'This field is required.'
        form = PromptForm(
            data={
                "programming_language": str(self.python_language.id),
                "topic": str(self.python_topic.id),
                "prompt_name_ru": "Prompt",
                "prompt_text_ru": "Body",
                "mode": Prompt.MODE_SOLVE,
            }
        )

        self.assertTrue(form.is_valid())
        self.assertQuerySetEqual(
            form.fields["topic"].queryset,
            [self.python_topic],
            transform=lambda item: item,
        )

    def test_form_validates_topic_belongs_to_selected_language(self):
        form = PromptForm(
            data={
                "programming_language": str(self.python_language.id),
                "topic": str(self.c_topic.id),
                "prompt_name_ru": "Prompt",
                "prompt_text_ru": "Body",
            }
        )

        self.assertFalse(form.is_valid())
        self.assertIn("topic", form.errors)


class LocalizationHelpersTests(TestCase):
    def test_ui_language_suffix_mapping(self):
        self.assertEqual(get_ui_language_suffix("Русский"), "ru")
        self.assertEqual(get_ui_language_suffix("English"), "en")
        self.assertEqual(get_ui_language_suffix("Français"), "fr")
        self.assertEqual(get_ui_language_suffix("Russian"), "ru")

    def test_get_localized_name_falls_back(self):
        topic = Topic(topic_name_ru="Рус", topic_name_en="Eng", topic_name_fr="Fra")
        self.assertEqual(get_localized_name(topic, "Русский", "topic_name"), "Рус")
        self.assertEqual(get_localized_name(topic, "English", "topic_name"), "Eng")
        self.assertEqual(get_localized_name(topic, "Français", "topic_name"), "Fra")
        self.assertEqual(get_localized_name(topic, "Unknown", "topic_name"), "Рус")

    def test_get_localized_name_empty_fields_no_recursion(self):
        """Пустые *_ru поля НЕ роняют str() бесконечной рекурсией.

        __str__ Prompt/Topic/SharedPrompt делегируют в get_localized_name —
        старый фолбэк ``str(obj)`` зацикливался и давал RecursionError → 500
        на /admin/ai/prompt/add/ (log_addition) и на любом str() записи без
        имени (в т.ч. changelist). Фолбэк — безопасная подпись по pk.
        """
        # Без pk — подпись классом.
        self.assertEqual(get_localized_name(Prompt(), "", "prompt_name"), "Prompt")
        self.assertEqual(str(Prompt(prompt_name_ru="")), "Prompt")
        # С pk — «Класс #pk» (Prompt.__str__ имеет свой фолбэк «Prompt #id»,
        # но хелпер сам больше не рекурсирует для Topic/SharedPrompt).
        self.assertEqual(str(Prompt(prompt_name_ru="", pk=83)), "Prompt #83")
        self.assertEqual(str(Topic(topic_name_ru="", pk=5)), "Topic #5")
        shared = SharedPrompt(prompt_name_ru="", prompt_name_en="", prompt_name_fr="", pk=7)
        self.assertEqual(str(shared), "[Общий] SharedPrompt #7")
        # Сериализаторы поверх того же хелпера — тоже без рекурсии.
        from ai import serializers
        self.assertEqual(serializers.topic(Topic(pk=5), "Русский")["name"], "Topic #5")
        self.assertEqual(serializers.prompt(Prompt(pk=83), "Русский")["name"], "Prompt #83")


class PromptEffectiveTextTests(TestCase):
    def setUp(self):
        self.pl = ProgrammingLanguage.objects.create(language_name="Python")

    def test_effective_text_uses_ui_language(self):
        prompt = Prompt(
            prompt_name_ru="P",
            prompt_text_ru="Рус {language}",
            prompt_text_en="Eng {language}",
        )
        self.assertEqual(prompt.get_effective_text("Русский", "Python"), "Рус Python")
        self.assertEqual(prompt.get_effective_text("English", "Python"), "Eng Python")

    def test_shared_prompt_text_uses_ui_language(self):
        # Базовые поля удалены: цепочка локализации _en → _ru. Без _en английский
        # UI видит русский текст (fallback на _ru).
        shared = SharedPrompt(prompt_name_ru="S", prompt_text_ru="Рус {language}")
        prompt = Prompt(prompt_name_ru="P", shared_prompt=shared)
        self.assertEqual(prompt.get_effective_text("Русский", "C++"), "Рус C++")
        self.assertEqual(prompt.get_effective_text("English", "C++"), "Рус C++")


class ProblemDataApiUiLanguageTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user = get_user_model().objects.create_user(username="api_user", password="test-pass")

    def _request(self, ui_language=""):
        params = {}
        if ui_language:
            params["ui_language"] = ui_language
        request = self.factory.get("/ai/api/problem-data/", data=params)
        request.user = self.user
        request.user_info = {"userId": self.user.username}
        request.COOKIES = {"userId": self.user.username}
        return request

    def test_problem_data_localizes_topic_and_prompt_names(self):
        pl = ProgrammingLanguage.objects.create(language_name="Python")
        topic = Topic.objects.create(
            topic_name_ru="Русская тема",
            topic_name_en="English topic",
            programming_language=pl,
        )
        Prompt.objects.create(
            topic=topic,
            prompt_name_ru="Русский промпт",
            prompt_name_en="English prompt",
            prompt_text_ru="text",
        )

        response_en = get_problem_data(self._request("English"))
        data_en = json.loads(response_en.content)
        self.assertEqual(data_en["topics"][0]["name"], "English topic")
        self.assertEqual(data_en["prompts"][0]["name"], "English prompt")

        response_ru = get_problem_data(self._request("Русский"))
        data_ru = json.loads(response_ru.content)
        self.assertEqual(data_ru["topics"][0]["name"], "Русская тема")
        self.assertEqual(data_ru["prompts"][0]["name"], "Русский промпт")

    def test_prompt_name_falls_back_to_russian_when_en_missing(self):
        """When an admin has not filled prompt_name_en, the English UI must
        fall back to the Russian/base name (not disappear). This is the data
        situation behind 'preprompt names don't translate to English' — the
        localization pipeline is correct; the English names simply need to be
        entered in the admin (prompt_name_en field)."""
        pl = ProgrammingLanguage.objects.create(language_name="Python")
        topic = Topic.objects.create(
            topic_name_ru="Base topic",
            topic_name_en="English topic",
            programming_language=pl,
        )
        Prompt.objects.create(
            topic=topic,
            prompt_name_ru="Русский оригинал",
            prompt_name_en="",  # no English translation entered
            prompt_text_ru="text",
        )

        data_en = json.loads(get_problem_data(self._request("English")).content)
        # English name missing -> falls back to the base (Russian) name.
        self.assertEqual(data_en["prompts"][0]["name"], "Русский оригинал")


class AIRequestLogModelTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="log_user",
            password="test-pass",
            first_name="Log",
            last_name="User",
        )

    def test_create_log_from_websocket(self):
        log = AIRequestLog.objects.create(
            user=self.user,
            username=self.user.username,
            external_user_id="42",
            user_full_name=self.user.get_full_name(),
            client_id="client-1",
            source=AIRequestLog.SOURCE_WEBSOCKET,
            mode=AIRequestLog.MODE_CHAT,
            sent_at=timezone.now(),
            model_names=["DeepSeek-R1"],
            message="hello",
            programming_language_id=1,
            programming_language_name="Python",
            topic_id=2,
            topic_name="Loops",
            prompt_id=3,
            prompt_name="Helper",
        )
        log.refresh_from_db()
        self.assertEqual(log.user_full_name, "Log User")
        self.assertEqual(log.model_names, ["DeepSeek-R1"])
        # Миграция 0029: дефолт статуса — error. Запись лога создаётся в момент
        # отправки запроса (LogWriter.create) без status и считается ошибкой,
        # пока update_success не закроет её успехом при непустом ответе модели.
        # Здесь create() без update_* — статус остаётся дефолтным (error).
        self.assertEqual(log.status, AIRequestLog.STATUS_ERROR)
        self.assertEqual(log.mode, AIRequestLog.MODE_CHAT)
        self.assertEqual(log.get_mode_display(), "Чат")
        self.assertEqual(log.programming_language_name, "Python")
        self.assertEqual(log.topic_name, "Loops")
        self.assertEqual(log.prompt_name, "Helper")


class ModelClientRegistryTests(SimpleTestCase):
    def test_registry_contains_expected_models(self):
        from ai.model_clients import registry

        # Default (always-on) providers: Web DeepSeek pool.
        for key in ("Web_DeepSeek", "Web_DeepSeek_Thinking"):
            self.assertIsNotNone(registry.get(key), f"Missing registry entry for {key}")
            self.assertTrue(callable(registry.handler(key)))

    def test_sambanova_models_complete(self):
        # SambaNova is gated behind AI_ENABLE_SAMBANOVA (off by default at import
        # time), so the global registry won't contain it — verify the builder
        # function returns all 10 declared models via a local registry instance.
        from ai.model_clients.registry import _sambanova_models, ModelRegistry

        samba = ModelRegistry(_sambanova_models())
        expected = {
            "DeepSeek_R1_Distill_Llama_70B", "DeepSeek_V3_1", "DeepSeek_V3_1_cb",
            "DeepSeek_V3_2", "Llama_4_Maverick_17B_128E_Instruct",
            "Meta_Llama_3_3_70B_Instruct", "MiniMax_M2_5", "MiniMax_M2_7",
            "Gemma_3_12b_it", "Gpt_oss_120b",
        }
        self.assertEqual(set(samba.keys()), expected)
        for key in expected:
            self.assertIsNotNone(samba.get(key), f"Missing sambanova entry for {key}")
            self.assertTrue(callable(samba.handler(key)))

    def test_model_caller_resolves_backward_compatible_aliases(self):
        # Legacy aliases live in ModelCaller._resolve_legacy_alias, NOT in the
        # registry (see CLAUDE.md: «legacy aliases resolved in ModelCaller»).
        from ai.services.model_caller import _resolve_legacy_alias

        self.assertEqual(_resolve_legacy_alias("DeepSeek_R1"), "DeepSeek_R1_Distill_Llama_70B")
        self.assertEqual(_resolve_legacy_alias("Meta_Llama_3_1_70B_Instruct"), "Meta_Llama_3_3_70B_Instruct")
        self.assertEqual(_resolve_legacy_alias("Mixtral_8x22b"), "Llama_4_Maverick_17B_128E_Instruct")


class DLApiClientEncodingTests(SimpleTestCase):
    """Unit tests for DL API response decoding and mojibake repair."""

    def _decode(self, text: str, encoding: str = "utf-8") -> dict:
        class FakeResponse:
            content = text.encode(encoding)

        return _decode_response_json(FakeResponse())

    def test_repairs_cp866_bytes_presented_as_cp1251(self):
        garbled = (
            "XXVII Љ®¬­¤­л© зҐ¬ЇЁ®­­в иЄ®«м­­ЁЄ®ў "
            "‘­Єв-ЏҐўҐаЎгаЈ Ї® Їа®Ја¬¬Ёа®ў­Ё®"
        )
        payload = json.dumps({"statement": garbled}, ensure_ascii=False)
        result = self._decode(payload)
        repaired = result["statement"]

        self.assertNotEqual(repaired, garbled)
        cyrillic = sum(1 for c in repaired if "Ѐ" <= c <= "ӿ")
        self.assertGreater(cyrillic, 30)
        self.assertIn("XXVII", repaired)
        self.assertTrue(repaired.startswith("XXVII "))

    def test_does_not_corrupt_valid_utf8_cyrillic(self):
        normal = (
            "XXVII Командный чемпионат школьников "
            "Санкт-Петербург по программированию"
        )
        payload = json.dumps({"statement": normal}, ensure_ascii=False)
        result = self._decode(payload)
        self.assertEqual(result["statement"], normal)

    def test_leaves_ascii_text_unchanged(self):
        payload = json.dumps({"statement": "Hello, world!"}, ensure_ascii=False)
        result = self._decode(payload)
        self.assertEqual(result["statement"], "Hello, world!")

    def test_preserves_utf8_nbsp_spaces_in_english_statement(self):
        """English statements that use non-breaking spaces (U+00A0) for spacing
        must not be turned into the '┬а' mojibake.

        nbsp is UTF-8 bytes [0xC2, 0xA0]; the cp1251 candidate decodes those as
        Cyrillic В + nbsp and used to win scoring (В gets the +2 Cyrillic
        bonus), after which the CP866 repair converted В+nbsp into '┬а'.
        """
        statement = "International\xa0Olympiad\xa0in\xa0Informatics"
        payload = json.dumps({"statement": statement}, ensure_ascii=False)
        result = self._decode(payload)
        self.assertNotIn("┬а", result["statement"])
        self.assertNotIn("В\xa0", result["statement"])
        self.assertIn("International", result["statement"])
        self.assertIn("Olympiad", result["statement"])

    def test_repairs_mixed_cp866_payload_with_replacement_chars(self):
        """A payload with U+FFFD mixed with cp1251 codepoints must not crash.

        Some PDF-derived responses contain Unicode replacement characters
        alongside the CP866-via-cp1251 mojibake. The repair must drop the
        replacement chars and still recover readable Cyrillic.
        """
        # "Привет мир!" as CP866 bytes interpreted as cp1251 codepoints,
        # plus a trailing U+FFFD.
        garbled = "ЏаЁўҐв ¬Ёа!" + chr(0xFFFD)
        payload = json.dumps({"statement": garbled}, ensure_ascii=False)
        result = self._decode(payload)
        # The result should contain Cyrillic and no replacement characters.
        self.assertNotIn(chr(0xFFFD), result["statement"])
        self.assertIn("Привет", result["statement"])
        self.assertIn("мир", result["statement"])

    def test_repairs_real_curl_captured_payload(self):
        """The captured /restapi/get-task-info payload decodes correctly."""
        curl_path = Path(__file__).resolve().parent.parent / "curl.txt"
        if not curl_path.exists():
            self.skipTest("curl.txt fixture not found")

        raw = curl_path.read_bytes()
        # The first line of the file is the JSON response.
        json_bytes = raw.split(b"\n")[0]

        class FakeResponse:
            content = json_bytes

        result = _decode_response_json(FakeResponse())
        self.assertEqual(result["taskId"], 221905)
        self.assertEqual(result["name"], "Прибытие короля")
        self.assertIn(
            "XXVII Командный чемпионат школьников Санкт-Петербурга",
            result["statement"],
        )
        # The statement is long and should contain plenty of Cyrillic.
        cyrillic = sum(1 for c in result["statement"] if "Ѐ" <= c <= "ӿ")
        self.assertGreater(cyrillic, 1000)

    def test_non_json_response_error_includes_status_and_snippet(self):
        """When DL returns a 2xx with a non-JSON body (HTML error page, empty
        body, login redirect, …), the error must carry the HTTP status and a
        body snippet so the operator can diagnose it — not a bare «некорректный
        JSON»."""
        from ai.dl_api_client import DLServerError, _decode_response_json

        class FakeResponse:
            status_code = 200
            content = b"<!DOCTYPE html><html><body>413 Request Entity Too Large</body></html>"

        with self.assertRaises(DLServerError) as cm:
            _decode_response_json(FakeResponse())
        msg = str(cm.exception)
        self.assertIn("код 200", msg)
        self.assertIn("413 Request Entity Too Large", msg)

    def test_non_json_empty_response_error_notes_empty_body(self):
        from ai.dl_api_client import DLServerError, _decode_response_json

        class FakeResponse:
            status_code = 200
            content = b""

        with self.assertRaises(DLServerError) as cm:
            _decode_response_json(FakeResponse())
        self.assertIn("пустой ответ", str(cm.exception))

    def test_raise_for_status_handles_generic_4xx_with_body(self):
        """Unmapped 4xx (400/405/413/422) must raise a typed error with the body
        snippet instead of falling through to JSON parsing of an HTML page."""
        from ai.dl_api_client import _raise_for_status, DLServerError

        class FakeResponse:
            status_code = 413
            content = b"<html>nginx 413 Too Large</html>"

        with self.assertRaises(DLServerError) as cm:
            _raise_for_status(FakeResponse())
        msg = str(cm.exception)
        self.assertIn("413", msg)
        self.assertIn("nginx 413 Too Large", msg)

    def test_raise_for_status_still_maps_known_statuses(self):
        from ai.dl_api_client import (
            _raise_for_status,
            DLUnauthorizedError,
            DLForbiddenError,
            DLTaskNotFoundError,
            DLServerError,
        )

        class R:
            def __init__(self, code, body=b""):
                self.status_code = code
                self.content = body

        self.assertRaises(DLUnauthorizedError, _raise_for_status, R(401))
        self.assertRaises(DLForbiddenError, _raise_for_status, R(403))
        self.assertRaises(DLTaskNotFoundError, _raise_for_status, R(404))
        self.assertRaises(DLServerError, _raise_for_status, R(500))
        # 200 — no raise.
        _raise_for_status(R(200))


class ConversationHistoryTests(TestCase):
    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.history = ConversationHistory(max_messages=4)

    def test_get_returns_empty_list_for_unknown_user(self):
        self.assertEqual(self.history.get("user-1"), [])

    def test_add_exchange_appends_messages(self):
        self.history.append("user-1", {"role": "user", "content": "hello"})
        self.history.append("user-1", {"role": "assistant", "content": "hi"})
        self.assertEqual(
            self.history.get("user-1"),
            [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
            ],
        )

    def test_history_caps_at_max_messages(self):
        for user_msg, assistant_msg in (("a", "A"), ("b", "B"), ("c", "C")):
            self.history.append("user-1", {"role": "user", "content": user_msg})
            self.history.append("user-1", {"role": "assistant", "content": assistant_msg})
        # max_messages=4, so after three exchanges (6 messages) we keep the last 4:
        # [assistant A, user b, assistant B, user c, assistant C] -> capped to 4
        # -> [user b, assistant B, user c, assistant C]
        history = self.history.get("user-1")
        self.assertEqual(len(history), 4)
        self.assertEqual(history[0]["content"], "b")

    def test_reset_clears_history(self):
        self.history.append("user-1", {"role": "user", "content": "hello"})
        self.history.append("user-1", {"role": "assistant", "content": "hi"})
        self.history.reset("user-1")
        self.assertEqual(self.history.get("user-1"), [])


class MessageComposerTests(TestCase):
    def setUp(self):
        self.composer = MessageComposer()

    async def test_chat_mode_appends_prompt_when_provided(self):
        data = {
            "type": "1",
            "message": "hello",
            "preprompt": "shared_999",
            "language": "English",
        }
        with patch.object(
            self.composer.resolver,
            "resolve_text",
            new=AsyncMock(return_value="Think step by step."),
        ):
            message, mode = await self.composer.compose(data)
        self.assertEqual(message, "hello\n\nRespond only in English.\n\nPreprompt: Think step by step.")
        self.assertEqual(mode, AIRequestLog.MODE_CHAT)

    async def test_solve_mode_uses_default_message_when_no_shared_prompt(self):
        data = {
            "type": "2",
            "message": "sum numbers",
            "language": "English",
            "programming_language_name": "Python",
            "topic_name": "Loops",
        }
        with patch.object(
            self.composer.resolver,
            "resolve_text",
            new=AsyncMock(return_value=None),
        ):
            with patch("ai.services.message_composer.get_default_shared_prompt", new=AsyncMock(return_value=None)):
                message, mode = await self.composer.compose(data)
        self.assertIn("Python", message)
        self.assertIn("Loops", message)
        self.assertIn("sum numbers", message)
        self.assertEqual(mode, AIRequestLog.MODE_SOLVE)


class ModelCallerTests(SimpleTestCase):
    async def test_returns_error_for_unknown_model(self):
        result = await ModelCaller().call("hi", "client", "Unknown_Model")
        self.assertTrue(result.is_error)
        self.assertIn("не найдена", result.response_text)

    async def test_returns_success_for_known_model(self):
        registry_mock = MagicMock()
        registry_mock.get.return_value = True
        registry_mock.handler.return_value = AsyncMock(return_value=("answer", 42))
        registry_mock.title.return_value = "Test Model"

        result = await ModelCaller(registry_mock).call("hi", "client", "Known")
        self.assertFalse(result.is_error)
        self.assertEqual(result.response_text, "answer")
        self.assertEqual(result.tokens, 42)
        self.assertEqual(result.model_title, "Test Model")

    async def test_propagates_is_error_from_client_3tuple(self):
        # Clients signal a 200-OK-but-error body (rate limit, «все боты
        # заняты», parse failure, …) via a 3rd tuple element. ModelCaller must
        # propagate it so the consumer routes to update_error — replacing the
        # old text-marker heuristic that mis-flagged legit answers discussing
        # «ошибка». A 2-tuple (no 3rd element) stays is_error=False (back-compat).
        registry_mock = MagicMock()
        registry_mock.get.return_value = True
        registry_mock.handler.return_value = AsyncMock(
            return_value=("Все боты заняты", 0, True)
        )
        registry_mock.title.return_value = "Web DeepSeek"

        result = await ModelCaller(registry_mock).call("hi", "client", "Known")
        self.assertTrue(result.is_error)
        self.assertEqual(result.response_text, "Все боты заняты")
        self.assertEqual(result.tokens, 0)

    async def test_passes_think_switch_to_thinking_provider(self):
        # Кнопка Think (Ollama): think=true/false уходит в хендлер kwarg'ом —
        # только у провайдера с API-переключателем (supports_think=True).
        registry_mock = MagicMock()
        registry_mock.get.return_value = True
        handler = AsyncMock(return_value=("ответ", 5, False))
        registry_mock.handler.return_value = handler
        registry_mock.title.return_value = "Ollama Model"

        with patch("ai.services.model_caller.supports_think", return_value=True):
            await ModelCaller(registry_mock).call("hi", "client", "Ollama_X", think=False)
        handler.assert_awaited_with("hi", "client", think=False)

        with patch("ai.services.model_caller.supports_think", return_value=True):
            await ModelCaller(registry_mock).call("hi", "client", "Ollama_X", think=True)
        handler.assert_awaited_with("hi", "client", think=True)

    async def test_think_ignored_for_providers_without_switch_and_on_default(self):
        # Провайдер без переключателя (web-пулы, OpenRouter) и think=None
        # (кнопка скрыта/поле не пришло) — вызов БЕЗ kwarg: поле think в запрос
        # к такому провайдеру недоступно, None — поведение модели по умолчанию.
        registry_mock = MagicMock()
        registry_mock.get.return_value = True
        handler = AsyncMock(return_value=("ответ", 5, False))
        registry_mock.handler.return_value = handler
        registry_mock.title.return_value = "Web Pool"

        # think=False у не-ollama провайдера → kwarg не проходит.
        with patch("ai.services.model_caller.supports_think", return_value=False):
            await ModelCaller(registry_mock).call("hi", "client", "Web_X", think=False)
        handler.assert_awaited_with("hi", "client")

        # think=None у ollama → тоже вызов без kwarg (дефолт модели).
        with patch("ai.services.model_caller.supports_think", return_value=True):
            await ModelCaller(registry_mock).call("hi", "client", "Ollama_X", think=None)
        handler.assert_awaited_with("hi", "client")


class ModelKeyResolutionTests(SimpleTestCase):
    """Резолв-цепочка имени модели (model_caller._resolve_model_key): точный
    ключ → legacy-алиас → нормализованный ключ/title. Куки ai_state старых
    пользователей хранят переименованные ключи (живут 1 год) и title'ы —
    всё это должно сводиться к актуальному ключу, а не к ошибке «не найдена»."""

    def _resolve(self, value):
        from ai.model_clients import registry
        from ai.services.model_caller import _resolve_model_key
        return _resolve_model_key(registry, value)

    def test_exact_key_wins(self):
        self.assertEqual(self._resolve("Web_DeepSeek"), "Web_DeepSeek")

    def test_case_and_separator_variants(self):
        for variant in ("web deepseek", "Web-DeepSeek", "WEB_DEEPSEEK", "web_deepseek"):
            self.assertEqual(self._resolve(variant), "Web_DeepSeek", variant)

    def test_default_key_variant(self):
        from ai.model_clients.registry import DEFAULT_MODEL_KEY
        self.assertEqual(self._resolve("ollama gemma 4 cloud"), DEFAULT_MODEL_KEY)

    def test_title_with_mixed_case(self):
        # «Web Deepseek» — реальный ввод пользователя (без большой S) — title.
        self.assertEqual(self._resolve("Web Deepseek"), "Web_DeepSeek")

    def test_unknown_returns_empty(self):
        self.assertEqual(self._resolve("Nonexistent_Model_Xyz"), "")
        self.assertEqual(self._resolve(""), "")

    async def test_call_error_echoes_raw_input(self):
        """При полном промахе текст ошибки называет ТО, что пришло (диагностика)."""
        result = await ModelCaller().call("hi", "client", "Garbage_Name")
        self.assertTrue(result.is_error)
        self.assertIn("Garbage_Name", result.response_text)


class RateLimiterTests(SimpleTestCase):
    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.limit = 2
        self.limiter = RateLimiter(ws_limit=self.limit, http_limit=self.limit, window_seconds=60)

    def test_allows_requests_under_limit(self):
        self.assertTrue(self.limiter.is_allowed_ws("user-1"))
        self.assertTrue(self.limiter.is_allowed_ws("user-1"))

    def test_blocks_requests_over_limit(self):
        self.limiter.is_allowed_ws("user-1")
        self.limiter.is_allowed_ws("user-1")
        self.assertFalse(self.limiter.is_allowed_ws("user-1"))

    def test_limits_are_isolated_by_user(self):
        self.limiter.is_allowed_ws("user-1")
        self.limiter.is_allowed_ws("user-1")
        self.assertTrue(self.limiter.is_allowed_ws("user-2"))

    def test_rate_limited_decorator_returns_429_when_over_limit(self):
        request = RequestFactory().get("/ai/api/prompts/")
        request.user = SimpleNamespace(is_authenticated=True, pk=1)
        request.user_info = {"userId": "1"}
        request.headers = {"Accept": "application/json"}

        custom_limiter = RateLimiter(ws_limit=2, http_limit=2, window_seconds=60)

        @rate_limited
        def sample_view(request):
            return HttpResponse("ok")

        with patch("ai.throttling.rate_limiter", custom_limiter):
            sample_view(request)
            sample_view(request)
            response = sample_view(request)
        self.assertEqual(response.status_code, 429)

    def test_poll_counter_is_separate_from_http_counter(self):
        from django.core.cache import cache
        from ai.throttling import RateLimiter

        cache.clear()
        # http_limit=2, poll_limit=5 — poll requests must not consume the
        # action budget and get their own (higher) bound.
        limiter = RateLimiter(ws_limit=2, http_limit=2, window_seconds=60, poll_limit=5)
        # Saturate the poll counter beyond the http limit.
        for _ in range(3):
            self.assertTrue(limiter.is_allowed_poll("user-1"))
        # The http (action) counter is untouched: still 2 actions allowed.
        self.assertTrue(limiter.is_allowed_http("user-1"))
        self.assertTrue(limiter.is_allowed_http("user-1"))
        self.assertFalse(limiter.is_allowed_http("user-1"))

    def test_poll_request_path_detected(self):
        from ai.throttling import _is_poll_request

        def mk(method, path):
            req = RequestFactory().get(path) if method == "GET" else RequestFactory().post(path)
            req.method = method
            return req

        self.assertTrue(_is_poll_request(mk("GET", "/ai/admin/arm/models/state/")))
        # Non-poll paths and non-GET methods are not poll requests.
        self.assertFalse(_is_poll_request(mk("GET", "/ai/api/problem-data/")))
        self.assertFalse(_is_poll_request(mk("POST", "/ai/admin/arm/models/state/")))


class RateLimitMiddlewarePollTests(SimpleTestCase):
    """The middleware must route read-only polling endpoints through the
    separate poll counter so background polling never 429s real actions."""

    def setUp(self):
        from django.core.cache import cache
        cache.clear()

    def _request(self, path, method="GET"):
        request = RequestFactory().get(path) if method == "GET" else RequestFactory().post(path)
        request.method = method
        request.user = SimpleNamespace(is_authenticated=True, pk=1)
        request.user_info = {"userId": "1"}
        request.COOKIES = {"userId": "1"}
        request.headers = {"Accept": "application/json"}
        return request

    def test_poll_requests_do_not_consume_http_action_budget(self):
        from ai.throttling import RateLimitMiddleware, RateLimiter

        custom_limiter = RateLimiter(ws_limit=2, http_limit=2, window_seconds=60, poll_limit=10)
        calls = {"n": 0}

        def get_response(request):
            calls["n"] += 1
            return HttpResponse("ok")

        middleware = RateLimitMiddleware(get_response)
        middleware.enabled = True
        with patch("ai.throttling.rate_limiter", custom_limiter):
            # 5 poll requests — all pass (poll_limit=10) and don't touch http.
            for _ in range(5):
                self.assertEqual(middleware(self._request("/ai/admin/arm/models/state/")).status_code, 200)
            # Action budget (http_limit=2) is still fully available.
            self.assertEqual(middleware(self._request("/ai/api/problem-data/")).status_code, 200)
            self.assertEqual(middleware(self._request("/ai/api/problem-data/")).status_code, 200)
            # 3rd action now 429s — proves polls did not consume it.
            self.assertEqual(middleware(self._request("/ai/api/problem-data/")).status_code, 429)
        self.assertEqual(calls["n"], 7)


class UserIdentityForLogTests(TestCase):
    def test_extracts_external_id_from_user_with_account(self):
        user = get_user_model().objects.create_user(
            username="local",
            password="test-pass",
            first_name="First",
            last_name="Last",
        )
        ExternalDLAccount.objects.create(user=user, external_user_id="ext-42", external_login="local")
        identity = get_user_identity_for_log(user, None)
        self.assertEqual(identity["external_user_id"], "ext-42")
        self.assertEqual(identity["user_full_name"], "First Last")

    def test_extracts_from_external_info_string(self):
        identity = get_user_identity_for_log("ext-42", {"firstName": "Alice", "lastName": "Smith"})
        self.assertEqual(identity["external_user_id"], "ext-42")
        self.assertEqual(identity["user_full_name"], "Alice Smith")


class ModelCapabilitiesTests(SimpleTestCase):
    """ТЗ B: registry.capabilities shape and per-model annotations."""

    def test_reasoning_models_are_marked_reasoning(self):
        from ai.model_clients import registry
        from ai.model_clients.registry import _sambanova_models, ModelRegistry

        # Web_DeepSeek_Thinking is in the default (always-on) registry.
        self.assertTrue(registry.capabilities("Web_DeepSeek_Thinking")["reasoning"])
        # SambaNova reasoning model — via a local registry (gated globally).
        samba = ModelRegistry(_sambanova_models())
        caps = samba.capabilities("DeepSeek_R1_Distill_Llama_70B")
        self.assertTrue(caps["reasoning"], "DeepSeek_R1_Distill_Llama_70B should be reasoning")
        self.assertTrue(caps["text"])
        self.assertFalse(caps["vision"])

    def test_plain_text_models_are_not_reasoning(self):
        from ai.model_clients import registry

        caps = registry.capabilities("DeepSeek_V3_1")
        self.assertFalse(caps["reasoning"])
        self.assertTrue(caps["text"])
        self.assertFalse(caps["vision"])

    def test_unknown_key_gets_conservative_default(self):
        from ai.model_clients import registry

        caps = registry.capabilities("does_not_exist")
        self.assertEqual(caps, {"text": True, "vision": False, "reasoning": False})

    def test_every_entry_exposes_three_boolean_capabilities(self):
        from ai.model_clients import registry

        for key in registry.keys():
            caps = registry.capabilities(key)
            self.assertEqual(set(caps.keys()), {"text", "vision", "reasoning"})
            for value in caps.values():
                self.assertIsInstance(value, bool)


class ArmReportTests(SimpleTestCase):
    """ТЗ D: _build_summary / _build_report aggregation and ordering."""

    def _result(self, key, status, duration, tokens=0):
        return {
            "model_key": key,
            "model_title": key,
            "status": status,
            "duration": duration,
            "tokens": tokens,
        }

    def test_summary_aggregates_and_sorts_by_percent_then_duration(self):
        from ai.arm_runner import _build_summary

        results = [
            self._result("A", "ok", 3.0, tokens=10),
            self._result("B", "error", 1.0, tokens=5),
            self._result("A", "ok", 5.0, tokens=20),
        ]
        summary = _build_summary(results)
        # A: 2/2 solved = 100%, avg (3+5)/2 = 4.0, tokens 30
        # B: 0/1 solved = 0%, avg 1.0, tokens 5
        self.assertEqual(summary[0]["model_key"], "A")
        self.assertEqual(summary[0]["solved"], 2)
        self.assertEqual(summary[0]["total"], 2)
        self.assertEqual(summary[0]["percent_solved"], 100.0)
        self.assertEqual(summary[0]["avg_duration"], 4.0)
        self.assertEqual(summary[0]["tokens"], 30)
        self.assertEqual(summary[1]["model_key"], "B")
        self.assertEqual(summary[1]["percent_solved"], 0.0)

    def test_summary_tiebreak_orders_by_fastest_average_duration(self):
        from ai.arm_runner import _build_summary

        results = [
            self._result("Slow", "ok", 5.0),
            self._result("Fast", "ok", 2.0),
        ]
        summary = _build_summary(results)
        # Both 100% solved; faster average wins.
        self.assertEqual(summary[0]["model_key"], "Fast")
        self.assertEqual(summary[1]["model_key"], "Slow")

    def test_build_report_none_for_empty_results(self):
        from ai.arm_runner import _build_report

        self.assertIsNone(_build_report([]))

    def test_build_report_includes_summary_and_counts(self):
        from ai.arm_runner import _build_report

        results = [
            self._result("A", "ok", 2.0, tokens=10),
            self._result("B", "error", 4.0, tokens=7),
        ]
        report = _build_report(results)
        self.assertEqual(report["models_total"], 2)
        self.assertEqual(report["success_count"], 1)
        self.assertEqual(report["error_count"], 1)
        self.assertEqual(report["tokens_total"], 17)
        self.assertEqual(report["fastest_model"], "A")
        self.assertIn("summary", report)
        self.assertEqual(len(report["summary"]), 2)


class AutorecoveryTests(TestCase):
    """ТЗ E: _maybe_autorecover_web_deepseek gating, annotation, and never-raise."""

    def _down_row(self, window_date, key="Web_DeepSeek"):
        from ai.models import AIModelAvailability
        return AIModelAvailability.objects.create(
            model_key=key,
            model_title="Web DeepSeek",
            is_available=False,
            window_date=window_date,
            last_message="down",
        )

    @override_settings(AI_WEB_DEEPSEEK_AUTORECOVERY=False)
    def test_disabled_flag_skips_restart_even_when_down(self):
        from ai.model_health import _maybe_autorecover_web_deepseek, get_health_window_date

        window_date = get_health_window_date()
        row = self._down_row(window_date)
        with patch("ai.model_health.restart_bot_pool") as mock_restart:
            _maybe_autorecover_web_deepseek({}, window_date)
            mock_restart.assert_not_called()
        row.refresh_from_db()
        self.assertEqual(row.last_message, "down")

    # override_settings, а не env-зависимость: локальный режим (ai/env_mode.py)
    # ставит AI_WEB_DEEPSEEK_AUTORECOVERY=0, когда прокси нет, — без оверрайда
    # тест краснеет на чистой локальной базе (это и есть «Автоподъём... not
    # found in 'down'»: gate рано выходит и аннотации не пишет).
    @override_settings(AI_WEB_DEEPSEEK_AUTORECOVERY=True)
    def test_restart_failure_annotates_pool_unavailable(self):
        from ai.model_health import _maybe_autorecover_web_deepseek, get_health_window_date

        window_date = get_health_window_date()
        row = self._down_row(window_date)
        with patch("ai.model_health.restart_bot_pool", return_value=False):
            _maybe_autorecover_web_deepseek({}, window_date)
        row.refresh_from_db()
        self.assertIn("Автоподъём не удался", row.last_message)

    @override_settings(AI_WEB_DEEPSEEK_AUTORECOVERY=True)
    def test_restart_success_annotates_ok(self):
        from ai.model_health import _maybe_autorecover_web_deepseek, get_health_window_date

        window_date = get_health_window_date()
        row = self._down_row(window_date)
        with patch("ai.model_health.restart_bot_pool", return_value=True), \
                patch("ai.model_health._check_one_model",
                      return_value={"is_available": True, "last_message": "2",
                                    "last_http_code": 200, "response_time_ms": 1}), \
                patch("ai.model_health.time.sleep"):
            _maybe_autorecover_web_deepseek({}, window_date)
        row.refresh_from_db()
        self.assertIn("[автоподъём: ок]", row.last_message)

    def test_no_restart_when_web_deepseek_is_up(self):
        from ai.models import AIModelAvailability
        from ai.model_health import _maybe_autorecover_web_deepseek, get_health_window_date

        window_date = get_health_window_date()
        for key in ("Web_DeepSeek", "Web_DeepSeek_Thinking"):
            AIModelAvailability.objects.create(
                model_key=key, model_title=key, is_available=True, window_date=window_date,
            )
        with patch("ai.model_health.restart_bot_pool") as mock_restart:
            _maybe_autorecover_web_deepseek({}, window_date)
            mock_restart.assert_not_called()


class ArmSolveModelOptionsTests(TestCase):
    """get_arm_solve_model_options: весь каталог Web_/Ollama_ + пометки доступности.

    Список чекбоксов на /arm/solve/ больше не режется по is_available: модель
    без строки AIModelAvailability за текущее окно (только что добавленная,
    до первой проверки в 04:00) всё равно должна попасть в список — иначе
    она невидима до следующего дня. Проверки: полный состав, пометки
    («» / «неактивна» / «не проверена»), доступные первыми, чужие префиксы вне.
    """

    def _row(self, key, is_available):
        from ai.models import AIModelAvailability
        from ai.model_health import get_health_window_date
        return AIModelAvailability.objects.create(
            model_key=key,
            model_title=key,
            is_available=is_available,
            window_date=get_health_window_date(),
        )

    def _options(self):
        from ai.model_health import get_arm_solve_model_options
        return get_arm_solve_model_options()

    def _arm_keys(self):
        from ai.model_health import MODEL_CATALOG_KEYS, is_arm_solve_model, is_hidden_from_selectors
        return [
            k for k in MODEL_CATALOG_KEYS
            if is_arm_solve_model(k) and not is_hidden_from_selectors(k)
        ]

    def test_all_catalog_arm_models_returned_without_rows(self):
        # Ни одной строки доступности за окно — все Web_/Ollama_-модели каталога
        # всё равно в списке (раньше новые модели появлялись только на след. день).
        options = self._options()
        by_key = {opt["key"]: opt for opt in options}
        self.assertEqual(set(by_key), set(self._arm_keys()))
        for opt in options:
            self.assertFalse(opt["is_available"])
            self.assertEqual(opt["availability_note"], "не проверена")

    def test_checked_models_get_notes_and_flags(self):
        keys = self._arm_keys()
        self.assertGreaterEqual(len(keys), 3)
        available_key, failed_key, missing_key = keys[0], keys[1], keys[2]
        self._row(available_key, True)
        self._row(failed_key, False)

        by_key = {opt["key"]: opt for opt in self._options()}
        self.assertTrue(by_key[available_key]["is_available"])
        self.assertEqual(by_key[available_key]["availability_note"], "")
        self.assertFalse(by_key[failed_key]["is_available"])
        self.assertEqual(by_key[failed_key]["availability_note"], "неактивна")
        self.assertFalse(by_key[missing_key]["is_available"])
        self.assertEqual(by_key[missing_key]["availability_note"], "не проверена")

    def test_available_models_sort_first_then_alphabetical(self):
        keys = self._arm_keys()
        self._row(keys[-1], True)  # единственная доступная — она и первая
        options = self._options()
        self.assertEqual(options[0]["key"], keys[-1])
        titles = [opt["title"] for opt in options[1:]]
        self.assertEqual(titles, sorted(titles, key=str.lower))

    def test_titles_and_capabilities_come_from_registry(self):
        from ai.model_clients import registry
        for opt in self._options():
            self.assertEqual(opt["title"], registry.title(opt["key"]))
            self.assertEqual(opt["capabilities"], registry.capabilities(opt["key"]))

    def test_only_web_and_ollama_keys_present(self):
        from ai.model_health import ARM_SOLVE_MODEL_PREFIXES
        for opt in self._options():
            self.assertTrue(opt["key"].startswith(ARM_SOLVE_MODEL_PREFIXES))


class ModelHealthGuardTests(TestCase):
    """Serialization guard for run_model_health_check (multi-worker prod safety).

    The cold-boot race: N Daphne workers booting at once all see no
    AIModelHealthRun row for the window. get_or_create on the unique
    window_date lets only one process create; the rest must observe the
    winner's RUNNING/COMPLETED status and bail out without sweeping.
    """

    def _window(self):
        from ai.model_health import get_health_window_date
        return get_health_window_date()

    def test_bails_out_when_a_recent_running_run_exists(self):
        from ai.model_health import run_model_health_check
        from ai.models import AIModelHealthRun

        AIModelHealthRun.objects.create(
            window_date=self._window(),
            status=AIModelHealthRun.STATUS_RUNNING,
            started_at=timezone.now(),
            finished_at=None,
            error_message="",
        )
        # If the guard works, get_runtime_model_handlers is never called
        # (the function returns inside the atomic block before the sweep).
        with patch("ai.model_health.get_runtime_model_handlers") as mock_handlers:
            result = run_model_health_check(force=False)
            mock_handlers.assert_not_called()
        self.assertFalse(result)

    def test_bails_out_when_a_completed_run_exists(self):
        from ai.model_health import run_model_health_check
        from ai.models import AIModelHealthRun

        AIModelHealthRun.objects.create(
            window_date=self._window(),
            status=AIModelHealthRun.STATUS_COMPLETED,
            started_at=timezone.now(),
            finished_at=timezone.now(),
            error_message="",
        )
        with patch("ai.model_health.get_runtime_model_handlers") as mock_handlers:
            result = run_model_health_check(force=False)
            mock_handlers.assert_not_called()
        self.assertFalse(result)

    def test_cold_start_creates_run_and_completes_without_real_api(self):
        from ai.model_health import get_health_window_date, run_model_health_check
        from ai.models import AIModelHealthRun

        # No existing row -> this process becomes the creator and sweeps.
        # Empty handlers => _check_one_model persists "Handler not found" rows
        # without making any real network call.
        with patch("ai.model_health.get_runtime_model_handlers", return_value={}), \
                patch("ai.model_health._maybe_autorecover_web_deepseek"):
            result = run_model_health_check(force=False)
        self.assertTrue(result)
        run = AIModelHealthRun.objects.get(window_date=get_health_window_date())
        self.assertEqual(run.status, AIModelHealthRun.STATUS_COMPLETED)

    def test_force_does_not_double_run_a_recent_running_run(self):
        """force=True may re-run COMPLETED/stale runs, but must NOT bypass an
        actively-running (<45min) run. This is the cross-process TOCTOU guard:
        two concurrent --force / admin-refresh invocations only do a racy
        read-only pre-check, so the row lock inside run_model_health_check is
        the real serialization point — this guard must hold even for force.
        """
        from ai.model_health import run_model_health_check
        from ai.models import AIModelHealthRun

        AIModelHealthRun.objects.create(
            window_date=self._window(),
            status=AIModelHealthRun.STATUS_RUNNING,
            started_at=timezone.now(),
            finished_at=None,
            error_message="",
        )
        with patch("ai.model_health.get_runtime_model_handlers") as mock_handlers:
            result = run_model_health_check(force=True)
            mock_handlers.assert_not_called()
        self.assertFalse(result)

    def test_force_does_run_a_stale_running_run(self):
        """A RUNNING run started >45min ago is treated as stuck and force=True
        IS allowed to re-run it (that is what --force is for)."""
        from datetime import timedelta
        from ai.model_health import run_model_health_check
        from ai.models import AIModelHealthRun

        AIModelHealthRun.objects.create(
            window_date=self._window(),
            status=AIModelHealthRun.STATUS_RUNNING,
            started_at=timezone.now() - timedelta(minutes=60),
            finished_at=None,
            error_message="",
        )
        with patch("ai.model_health.get_runtime_model_handlers", return_value={}), \
                patch("ai.model_health._maybe_autorecover_web_deepseek"):
            result = run_model_health_check(force=True)
        self.assertTrue(result)

    def test_force_runs_a_completed_run(self):
        from ai.model_health import run_model_health_check
        from ai.models import AIModelHealthRun

        AIModelHealthRun.objects.create(
            window_date=self._window(),
            status=AIModelHealthRun.STATUS_COMPLETED,
            started_at=timezone.now(),
            finished_at=timezone.now(),
            error_message="",
        )
        with patch("ai.model_health.get_runtime_model_handlers", return_value={}), \
                patch("ai.model_health._maybe_autorecover_web_deepseek"):
            result = run_model_health_check(force=True)
        self.assertTrue(result)

    def test_autorecovery_never_raises_when_bot_pool_unreachable(self):
        """When restart_bot_pool() returns False, _maybe_autorecover tries to
        annotate the down models. A transient DB error there must not escape
        (docstring: 'Never raises'), so the surrounding health run is not
        flipped to FAILED."""
        from ai.model_health import _maybe_autorecover_web_deepseek
        from ai.models import AIModelAvailability

        window = self._window()
        AIModelAvailability.objects.create(
            window_date=window,
            model_key="Web_DeepSeek",
            model_title="t",
            is_available=False,
            last_message="down",
        )
        with patch("ai.model_health.restart_bot_pool", return_value=False), \
                patch("ai.model_health._save_availability", side_effect=Exception("DB down")):
            # Must not raise.
            _maybe_autorecover_web_deepseek({}, window)


class ModelRefreshProgressTests(TestCase):
    """Прогресс sweep'а обновления моделей (глобальная запись в меню
    «Процессы» + бар на странице статуса)."""

    def setUp(self):
        from ai import model_health
        model_health._refresh_progress.clear()

    def tearDown(self):
        from ai import model_health
        model_health._refresh_progress.clear()

    def _run_sweep(self, *extra_patches):
        from contextlib import ExitStack

        from ai.model_health import run_model_health_check
        # Пустые handlers: _check_one_model пишет "Handler not found" в БД
        # без сетевых вызовов (паттерн ModelHealthGuardTests).
        with ExitStack() as stack:
            stack.enter_context(patch("ai.model_health.get_runtime_model_handlers", return_value={}))
            stack.enter_context(patch("ai.model_health._maybe_autorecover_web_deepseek"))
            stack.enter_context(patch("ai.model_health._maybe_autorecover_kimi"))
            for extra in extra_patches:
                stack.enter_context(extra)
            return run_model_health_check(force=False)

    def test_completed_sweep_updates_progress(self):
        from ai.model_health import (
            MODEL_CATALOG_KEYS, get_refresh_progress, list_model_refresh_runs,
        )
        self.assertTrue(self._run_sweep())
        progress = get_refresh_progress()
        self.assertEqual(progress["status"], "completed")
        self.assertEqual(progress["total"], len(MODEL_CATALOG_KEYS))
        self.assertEqual(progress["completed"], progress["total"])
        self.assertEqual(progress["current"], "")
        self.assertTrue(progress["run_id"])
        # Запись для меню: running-окно не фильтрует, свежая завершённая видна.
        entry = list_model_refresh_runs()[0]
        self.assertEqual(entry["run_type"], "model_refresh")
        self.assertEqual(entry["page_url"], "/ai/admin/arm/models/")
        self.assertEqual(entry["status"], "completed")
        self.assertEqual(entry["total"], progress["total"])

    def test_failed_sweep_marks_progress_failed(self):
        from ai.model_health import get_refresh_progress
        self.assertFalse(self._run_sweep(
            patch("ai.model_health._check_one_model", side_effect=RuntimeError("boom")),
        ))
        progress = get_refresh_progress()
        self.assertEqual(progress["status"], "failed")
        self.assertEqual(progress["completed"], 0)

    def test_skipped_sweep_leaves_progress_intact(self):
        """Ранний return guard'а (уже идёт проверка) не должен перетирать
        живой прогресс посторонним состоянием."""
        from ai.model_health import (
            get_health_window_date, get_refresh_progress, run_model_health_check,
        )
        from ai.models import AIModelHealthRun
        sentinel = {
            "run_id": "sentinel", "status": "running", "total": 7,
            "completed": 2, "current": "X", "created_at_ts": 1.0, "updated_at_ts": 2.0,
        }
        with patch.dict("ai.model_health._refresh_progress", sentinel, clear=True):
            AIModelHealthRun.objects.create(
                window_date=get_health_window_date(),
                status=AIModelHealthRun.STATUS_RUNNING,
                started_at=timezone.now(),
                finished_at=None,
                error_message="",
            )
            with patch("ai.model_health.get_runtime_model_handlers") as mock_handlers:
                result = run_model_health_check(force=False)
                mock_handlers.assert_not_called()
            self.assertFalse(result)
            # Прогресс живого sweep'а не сбит «пустым» состоянием.
            self.assertEqual(get_refresh_progress(), sentinel)

    def test_stale_finished_entry_filtered_by_since_ts(self):
        from ai.model_health import list_model_refresh_runs
        now_ts = time.time()
        stale = {
            "run_id": "stale", "status": "completed", "total": 5, "completed": 5,
            "current": "", "created_at_ts": now_ts - 4000, "updated_at_ts": now_ts - 4000,
        }
        with patch.dict("ai.model_health._refresh_progress", stale, clear=True):
            # Завершённый старее окна уведомления — не виден.
            self.assertEqual(list_model_refresh_runs(since_ts=now_ts - 60), [])
            # Но при нулевом cutoff (внутренние вызовы) — виден.
            self.assertEqual(len(list_model_refresh_runs()), 1)


class HealthClassifierTests(SimpleTestCase):
    """Robust healthcheck classifier: a correct answer wins unless the reply is
    a definite API/client error. Loose stems (недоступ/подключени/ошибка) no
    longer flip a healthy '2' to down."""

    def _healthy(self, text):
        from ai.model_health import _is_healthy_response
        return _is_healthy_response(text)

    def test_plain_digit_is_healthy(self):
        self.assertTrue(self._healthy("2"))
        self.assertTrue(self._healthy(" 2 "))

    def test_correct_answer_with_loose_marker_word_still_healthy(self):
        # Regression: previously "недоступ" stem marked this down even though
        # the model answered correctly.
        self.assertTrue(self._healthy("Никаких ошибок нет, ответ: 2"))
        self.assertTrue(self._healthy("Подключение установлено. 2"))

    def test_word_form_with_punctuation_is_healthy(self):
        self.assertTrue(self._healthy("два."))
        self.assertTrue(self._healthy("two."))
        self.assertTrue(self._healthy("Two"))

    def test_wrong_digit_is_unhealthy(self):
        self.assertFalse(self._healthy("12"))
        self.assertFalse(self._healthy("3"))

    def test_empty_is_unhealthy(self):
        self.assertFalse(self._healthy(""))
        self.assertFalse(self._healthy(None))

    def test_definite_api_error_is_unhealthy_even_with_digit_nearby(self):
        self.assertFalse(self._healthy("Ошибка API (код 402): закончились кредиты"))
        # "2 минуты" must not rescue a rate-limit reply.
        self.assertFalse(self._healthy("rate limit, подождите 2 минуты"))
        self.assertFalse(self._healthy("Бот не авторизован"))
        self.assertFalse(self._healthy("Таймаут при подключении к серверу. Попробуйте позже."))


class HealthCheckTransientTests(SimpleTestCase):
    """Retry-decision helpers (no DB needed): transient detection + invoke."""

    def test_timeout_is_transient(self):
        from ai.model_health import _looks_transient
        self.assertTrue(_looks_transient("Таймаут при подключении к серверу. Попробуйте позже."))
        self.assertTrue(_looks_transient("Бот инициализируется слишком долго."))
        self.assertTrue(_looks_transient(""))

    def test_definite_api_error_is_not_transient(self):
        from ai.model_health import _looks_transient
        self.assertFalse(_looks_transient("Ошибка API (код 402): закончились кредиты"))
        self.assertFalse(_looks_transient("Бот не авторизован"))

    def test_exception_is_transient(self):
        from ai.model_health import _looks_transient
        self.assertTrue(_looks_transient("", exc=Exception("boom")))

    def test_invoke_returns_text_and_no_exc_on_success(self):
        from datetime import date
        from ai.model_health import _invoke_healthcheck

        async def handler(prompt, conv_id):
            return ("2", 1)

        text, elapsed, exc = _invoke_healthcheck(handler, date(2026, 1, 1), "K")
        self.assertEqual(text, "2")
        self.assertIsNone(exc)
        self.assertGreaterEqual(elapsed, 0)

    def test_invoke_returns_exc_on_failure(self):
        from datetime import date
        from ai.model_health import _invoke_healthcheck

        async def handler(prompt, conv_id):
            raise RuntimeError("network down")

        text, elapsed, exc = _invoke_healthcheck(handler, date(2026, 1, 1), "K")
        self.assertEqual(text, "")
        self.assertIsInstance(exc, RuntimeError)


class HealthCheckRetryTests(TestCase):
    """One retry on transient failure so a cold-start timeout on a now-working
    model is not persisted as down. Definite API errors are not retried."""

    def _window(self):
        from ai.model_health import get_health_window_date
        return get_health_window_date()

    def test_retries_once_on_transient_timeout_then_marks_up(self):
        from ai.model_health import _check_one_model

        state = {"n": 0}

        async def handler(prompt, conv_id):
            state["n"] += 1
            if state["n"] == 1:
                return ("Таймаут при подключении к серверу. Попробуйте позже.", "0")
            return ("2", 1)

        handler_info = {"handler": handler, "title": "Test"}
        with patch("ai.model_health.time.sleep"):
            result = _check_one_model("TestKey", "Test", handler_info, self._window())
        self.assertTrue(result["is_available"])
        self.assertEqual(result["last_http_code"], 200)
        self.assertEqual(state["n"], 2)

    def test_does_not_retry_on_definite_api_error(self):
        from ai.model_health import _check_one_model

        state = {"n": 0}

        async def handler(prompt, conv_id):
            state["n"] += 1
            return ("Ошибка API (код 402): закончились кредиты", "0")

        handler_info = {"handler": handler, "title": "Test"}
        with patch("ai.model_health.time.sleep"):
            result = _check_one_model("TestKey", "Test", handler_info, self._window())
        self.assertFalse(result["is_available"])
        self.assertEqual(result["last_http_code"], 402)
        self.assertEqual(state["n"], 1)


class PoolEmptyHealthCheckTests(TestCase):
    """Пустой бот-пул (0 готовых ботов) диагностируется напрямую по /health пула,
    без ask-пинга: против пустого пула любой запрос 429, и 3 ретрая с backoff'ом
    лишь тянут время. Ложится как unavailable с честным сообщением."""

    def _patch_pools(self, ds_state="", kimi_state=""):
        from ai import model_health
        ds, kimi = MagicMock(), MagicMock()
        ds.pool_state.return_value = ds_state
        kimi.pool_state.return_value = kimi_state
        ds.empty_pool_message.return_value = (
            "Бот-пул Web DeepSeek пуст: нет ни одного готового бота (Chromium не установлен или бот не залогинен)."
        )
        kimi.empty_pool_message.return_value = (
            "Бот-пул Kimi пуст: нет ни одного готового бота (Chromium не установлен или бот не залогинен)."
        )
        patcher = patch.object(
            model_health, "_WEB_POOL_CLIENTS",
            {"Web_DeepSeek": ds, "Web_DeepSeek_Thinking": ds, "Web_Kimi": kimi},
        )
        return patcher, ds, kimi

    def test_empty_pool_short_circuits_without_model_call(self):
        from ai.model_health import _check_one_model

        async def handler(prompt, conv_id):
            raise AssertionError("ask-пинг не должен вызываться при пустом пуле")

        handler_info = {"handler": handler, "title": "Web DeepSeek"}
        patcher, ds, _kimi = self._patch_pools(ds_state="empty", kimi_state="live")
        with patcher, patch("ai.model_health.time.sleep"):
            result = _check_one_model("Web_DeepSeek", "Web DeepSeek", handler_info, self._window_date())
        self.assertFalse(result["is_available"])
        self.assertEqual(result["last_http_code"], 429)
        self.assertIn("пуст", result["last_message"])
        self.assertIn("Web DeepSeek", result["last_message"])

    def test_healthy_pool_still_uses_ask_ping(self):
        from ai.model_health import _check_one_model

        async def handler(prompt, conv_id):
            return ("2", 1)

        handler_info = {"handler": handler, "title": "Web DeepSeek"}
        patcher, ds, _kimi = self._patch_pools(ds_state="live", kimi_state="")
        with patcher, patch("ai.model_health.time.sleep"):
            result = _check_one_model("Web_DeepSeek", "Web DeepSeek", handler_info, self._window_date())
        self.assertTrue(result["is_available"])
        ds.pool_state.assert_called_once()

    def test_pool_starting_still_uses_ask_ping(self):
        # Бот запускается («starting » 0) — пул считается живым: старт может
        # доехать, ask-пинг сохраняет прежний ретрай-протокол.
        from ai.model_health import _check_one_model

        async def handler(prompt, conv_id):
            return ("2", 1)

        handler_info = {"handler": handler, "title": "Web Kimi"}
        patcher, _ds, kimi = self._patch_pools(ds_state="live", kimi_state="live")
        with patcher, patch("ai.model_health.time.sleep"):
            result = _check_one_model("Web_Kimi", "Web Kimi", handler_info, self._window_date())
        self.assertTrue(result["is_available"])
        kimi.pool_state.assert_called_once()

    def _window_date(self):
        from ai.model_health import get_health_window_date
        return get_health_window_date()


class BotPoolEmptyReplyTests(SimpleTestCase):
    """ask() против мёртвого пула: после исчерпания ретраев 429/503 превращаются
    в честное «пул пуст» вместо лживых «ограничил частоту запросов» / «бот
    инициализируется». Живой пул (боты есть или запускаются) сохраняет старые
    тексты — они там честны."""

    def _client(self):
        from ai.model_clients._base import BotPoolClient
        return BotPoolClient(
            base_url="http://127.0.0.1:9999",
            model="deepseek",
            label="Web DeepSeek",
            provider="web_deepseek",
            site_label="DeepSeek",
            selector_doc="WebPools/sites/deepseek.js",
        )

    def _post(self, status_code, retry_after="1"):
        import requests
        response = requests.Response()
        response.status_code = status_code
        response.headers["Retry-After"] = retry_after
        response._content = b'{"error":"all bots busy"}'
        return response

    def test_empty_pool_429_becomes_honest_message(self):
        client = self._client()
        with patch("ai.model_clients._base.asyncio.to_thread", return_value=self._post(429)), \
             patch.object(client, "pool_state", return_value="empty"), \
             patch("ai.model_clients._base.asyncio.sleep"):
            text, tokens, is_error, _cut, _prompt = self._run_ask(client)
        self.assertTrue(is_error)
        self.assertEqual(tokens, 0)
        self.assertIn("пуст", text)
        self.assertIn("нет ни одного готового бота", text)

    def test_empty_pool_503_becomes_honest_message(self):
        # Пул спавнил бота «вечно» и тот не поднимается: к концу ретраев /health
        # не показывает ни ready/busy/starting — честное «пул пуст» вместо
        # «Бот инициализируется слишком долго».
        client = self._client()
        with patch("ai.model_clients._base.asyncio.to_thread", return_value=self._post(503)), \
             patch.object(client, "pool_state", return_value="empty"), \
             patch("ai.model_clients._base.asyncio.sleep"):
            text, _, is_error, _cut, _prompt = self._run_ask(client)
        self.assertTrue(is_error)
        self.assertIn("пуст", text)

    def test_busy_pool_429_keeps_rate_limit_text(self):
        from ai.model_clients.exceptions import map_http_error
        client = self._client()
        with patch("ai.model_clients._base.asyncio.to_thread", return_value=self._post(429)), \
             patch.object(client, "pool_state", return_value="live"), \
             patch("ai.model_clients._base.asyncio.sleep"):
            text, _, _, _cut, _prompt = self._run_ask(client)
        self.assertEqual(text, map_http_error(429, "web_deepseek"))

    def test_starting_pool_503_keeps_bot_init_text(self):
        client = self._client()
        with patch("ai.model_clients._base.asyncio.to_thread", return_value=self._post(503)), \
             patch.object(client, "pool_state", return_value="live"), \
             patch("ai.model_clients._base.asyncio.sleep"):
            text, _, _, _cut, _prompt = self._run_ask(client)
        self.assertIn("инициализируется", text)

    def _run_ask(self, client):
        import asyncio
        return asyncio.run(client.ask("ping", user_id=1, thinking=False))


class ChatViewSelfHealTests(SimpleTestCase):
    """When no model is available for the current window, the chat page kicks a
    non-blocking forced sweep so a freshly-fixed key/balance recovers without
    waiting for 04:00 MSK or a manual --force."""

    def setUp(self):
        self.factory = RequestFactory()

    def _chat_request(self):
        request = self.factory.get("/ai/chat/")
        request.user = SimpleNamespace(is_authenticated=True, is_active=True, username="u")
        request.session = {}
        request.user_info = {"userId": "u"}
        request.COOKIES = {"userId": "u"}
        return request

    def test_empty_models_triggers_async_refresh(self):
        request = self._chat_request()
        with patch("ai.views.AIAppSettings.get_solo", return_value=SimpleNamespace(is_enabled=True)), \
             patch("ai.views.get_available_model_options", return_value=[]), \
             patch("ai.views.trigger_model_health_refresh_async") as mock_trigger, \
             patch("ai.views.render", return_value=HttpResponse("ok")):
            response = chat_view(request)
        self.assertEqual(response.status_code, 200)
        mock_trigger.assert_called_once()

    def test_populated_models_does_not_trigger_refresh(self):
        request = self._chat_request()
        models = [{"key": "DeepSeek_V3_1", "title": "DeepSeek-V3.1", "capabilities": {}}]
        with patch("ai.views.AIAppSettings.get_solo", return_value=SimpleNamespace(is_enabled=True)), \
             patch("ai.views.get_available_model_options", return_value=models), \
             patch("ai.views.trigger_model_health_refresh_async") as mock_trigger, \
             patch("ai.views.render", return_value=HttpResponse("ok")):
            response = chat_view(request)
        self.assertEqual(response.status_code, 200)
        mock_trigger.assert_not_called()


class TranslatePromptsCommandTests(TestCase):
    """translate_prompts: fill only empty _en/_fr fields, preserve placeholders,
    and --dry-run must not write."""

    def _echo_handler(self):
        async def handler(prompt, conv_id):
            # Echo the protected payload back so placeholder restore is exercised.
            marker = "\n\nТекст:\n"
            idx = prompt.find(marker)
            payload = prompt[idx + len(marker):] if idx >= 0 else prompt
            return (payload, 0)
        return handler

    def _run(self, *args):
        from io import StringIO
        from django.core.management import call_command
        out = StringIO()
        call_command("translate_prompts", *args, stdout=out)
        return out.getvalue()

    def setUp(self):
        from ai.models import SharedPrompt, Prompt, Topic
        self.shared = SharedPrompt.objects.create(
            prompt_name_ru="Решить задачу",
            prompt_text_ru="Реши задачу на {language} по теме {тема}. Код: {code}",
            prompt_name_en="",
            prompt_text_en="already-en",
            prompt_name_fr="",
            prompt_text_fr="",
        )

    def test_fills_only_empty_fields_and_preserves_placeholders(self):
        with patch("ai.management.commands.translate_prompts.registry.handler",
                   return_value=self._echo_handler()):
            self._run()

        self.shared.refresh_from_db()
        # Empty name fields were translated (echoed back).
        self.assertEqual(self.shared.prompt_name_en, "Решить задачу")
        self.assertEqual(self.shared.prompt_name_fr, "Решить задачу")
        # Non-empty _en text was NOT overwritten.
        self.assertEqual(self.shared.prompt_text_en, "already-en")
        # Empty _fr text was filled, with placeholders restored verbatim.
        self.assertIn("{language}", self.shared.prompt_text_fr)
        self.assertIn("{тема}", self.shared.prompt_text_fr)
        self.assertIn("{code}", self.shared.prompt_text_fr)
        self.assertNotIn("@@PH", self.shared.prompt_text_fr)

    def test_dry_run_does_not_write(self):
        with patch("ai.management.commands.translate_prompts.registry.handler",
                   return_value=self._echo_handler()):
            self._run("--dry-run")
        self.shared.refresh_from_db()
        self.assertEqual(self.shared.prompt_name_en, "")
        self.assertEqual(self.shared.prompt_name_fr, "")

    def test_unsupported_language_raises(self):
        from django.core.management import CommandError
        with patch("ai.management.commands.translate_prompts.registry.handler",
                   return_value=self._echo_handler()):
            with self.assertRaises(CommandError):
                self._run("--languages", "de")


class BatchGradingTests(SimpleTestCase):
    """Batch-solve ARM grading: normalize_solution / grade_solution."""

    def test_identical_after_whitespace_and_case_normalization(self):
        from ai.arm_runner import grade_solution
        self.assertEqual(
            grade_solution("Program A;\nBegin\n  Writeln(1);\nEnd.", "program a; begin writeln(1); end."),
            "solved",
        )

    def test_pascal_brace_and_paren_comments_stripped(self):
        from ai.arm_runner import grade_solution
        sample = "{ this is a comment } program a; begin writeln(1); end."
        model = "(* another *) program a; begin writeln(1); end."
        self.assertEqual(grade_solution(model, sample), "solved")

    def test_c_line_and_block_comments_stripped(self):
        from ai.arm_runner import grade_solution
        sample = "#include <stdio.h>\nint main(){return 0;}"
        model = "// leading comment\nint main(){ /* x */ return 0; }"
        self.assertEqual(grade_solution(model, sample), "solved")

    def test_different_solution_is_failed(self):
        from ai.arm_runner import grade_solution
        # Substantially different solutions fall below the similarity threshold.
        # (A single-token diff like return 42 vs return 0 is intentionally NOT
        #  enough to fail — grading is approximate, see CLAUDE.md.)
        self.assertEqual(
            grade_solution("print('hello world')", "int main(){return 0;}"),
            "failed",
        )

    def test_empty_sample_is_skipped(self):
        from ai.arm_runner import grade_solution
        self.assertEqual(grade_solution("anything", ""), "skipped")

    def test_empty_model_is_failed(self):
        from ai.arm_runner import grade_solution
        self.assertEqual(grade_solution("", "int main(){return 0;}"), "failed")


class DLCommentVerdictTests(SimpleTestCase):
    """Batch-solve ARM: вердикт по завершённому DL-комментарию.

    Раньше success-маркеры были голыми подстроками («ок»/«ok»): комментарий
    «Ошибка компиляции в строке 5» содержит «ок» (в «строке») и ложно
    помечал проваленную DL-проверку как solved.
    """

    def _verdict(self, comment):
        from ai.arm_runner import _verdict_from_dl_comment
        return _verdict_from_dl_comment(comment)

    def test_success_comments_are_solved(self):
        for comment in ("Все тесты пройдены успешно", "ОК", "ok", "Accepted",
                        "Тесты успешно пройдены", "Correct"):
            self.assertEqual(self._verdict(comment), "solved", comment)

    def test_compile_error_with_ok_substring_is_failed(self):
        # «в строке» содержит подстроку «ок» — провальный маркер приоритетен.
        self.assertEqual(
            self._verdict("Ошибка компиляции в строке 5: expected ';'"),
            "failed",
        )

    def test_wrong_answer_comments_are_failed(self):
        for comment in (
            "Неправильный ответ на тесте 2",
            "Неверный вывод программы",
            "Не все тесты пройдены",
            "Вывод не совпадает с ожидаемым",
            "Задача не прошла проверку",
            "Wrong answer on test 3",
            "Runtime error: token limit exceeded",
            "Solution rejected",
        ):
            self.assertEqual(self._verdict(comment), "failed", comment)

    def test_standalone_ok_word_boundaries(self):
        # «ок»/«ok» как отдельное слово — solved; внутри «строке»/«token» — нет.
        self.assertEqual(self._verdict("Всё ок"), "solved")
        self.assertEqual(self._verdict("token"), "failed")

    def test_empty_comment_is_failed(self):
        self.assertEqual(self._verdict(""), "failed")
        self.assertEqual(self._verdict(None), "failed")


class HealthCheckOutputTests(SimpleTestCase):
    """`check_models_health --force` live per-model console line formatting."""

    def _capture(self, detail):
        from io import StringIO
        from ai.management.commands.check_models_health import Command

        cmd = Command()
        out = StringIO()
        cmd.stdout = out
        cmd._print_model_detail(detail)
        return out.getvalue()

    def test_healthy_model_prints_200_and_response(self):
        line = self._capture({
            "title": "DeepSeek V3", "is_available": True,
            "last_http_code": 200, "response_time_ms": 850,
            "last_message": "2",
        })
        self.assertIn("DeepSeek V3", line)
        self.assertIn("HTTP 200", line)
        self.assertIn("OK", line)
        self.assertIn("850ms", line)
        self.assertIn("| 2", line)

    def test_down_model_prints_error_code_and_message(self):
        line = self._capture({
            "title": "GigaChat", "is_available": False,
            "last_http_code": 402, "response_time_ms": 120,
            "last_message": "Ошибка API (код 402): закончились кредиты",
        })
        self.assertIn("HTTP 402", line)
        self.assertIn("FAIL", line)
        self.assertIn("закончились кредиты", line)

    def test_missing_code_shows_dash(self):
        line = self._capture({
            "title": "Bot", "is_available": False,
            "last_http_code": None, "response_time_ms": None,
            "last_message": "Health check exception: timeout",
        })
        self.assertIn("HTTP —", line)
        self.assertIn("| — |", line)


class BatchReportTests(SimpleTestCase):
    """Batch-solve ARM report: per-model / per-topic aggregation + ordering."""

    def _item(self, model, topic, verdict, duration, tokens=0, topic_id=None):
        # topic_id — ключ группировки по темам (названия тем совпадают у
        # разных языков, поэтому группируем по ID, см. _topic_bucket_key).
        return {
            "model_key": model, "model_title": model,
            "topic_id": topic_id, "topic_name": topic, "verdict": verdict,
            "duration": duration, "tokens": tokens,
        }

    def test_per_model_and_per_topic_with_skipped_excluded(self):
        from ai.arm_runner import _build_batch_report

        results = [
            self._item("A", "Линейные", "solved", 2.0, 10, topic_id=1),
            self._item("A", "Циклы", "failed", 4.0, 10, topic_id=2),
            self._item("A", "Линейные", "skipped", 1.0, 2, topic_id=1),
            self._item("B", "Линейные", "solved", 5.0, 20, topic_id=1),
            self._item("B", "Циклы", "solved", 3.0, 20, topic_id=2),
        ]
        report = _build_batch_report(results)
        # Top-level counts: 3 solved, 1 failed, 1 skipped, 5 total.
        self.assertEqual(report["total_pairs"], 5)
        self.assertEqual(report["solved"], 3)
        self.assertEqual(report["failed"], 1)
        self.assertEqual(report["skipped"], 1)

        # Per-model: A has 1 solved / 2 non-skipped = 50%, B 2/2 = 100%.
        per_model = {row["label"]: row for row in report["per_model"]}
        self.assertEqual(per_model["A"]["solved"], 1)
        self.assertEqual(per_model["A"]["total"], 2)
        self.assertEqual(per_model["A"]["percent_solved"], 50.0)
        self.assertEqual(per_model["B"]["percent_solved"], 100.0)
        # Sorted by % desc -> B first.
        self.assertEqual(report["per_model"][0]["label"], "B")

        # Per-topic: skipped excluded. Линейные: A solved + B solved + A skipped
        # -> 2 solved / 2 total = 100%. Циклы: A failed + B solved -> 1/2 = 50%.
        per_topic = {row["label"]: row for row in report["per_topic"]}
        self.assertEqual(per_topic["Линейные"]["percent_solved"], 100.0)
        self.assertEqual(per_topic["Циклы"]["percent_solved"], 50.0)

    def test_report_volume_and_errors_by_topic(self):
        """Объём прогона (модели/задачи) и ошибки, сгруппированные по темам
        (failed/skipped; позиции сокращаются до капа, остаток — hidden)."""
        from ai.arm_runner import _build_batch_report

        results = [
            {"model_key": "A", "model_title": "A", "topic_name": "Циклы",
             "verdict": "failed", "duration": 1.0, "task_node_id": 1, "task_name": "Зад1"},
            {"model_key": "A", "model_title": "A", "topic_name": "Циклы",
             "verdict": "skipped", "duration": 1.0, "task_node_id": 2, "task_name": "Зад2"},
            {"model_key": "B", "model_title": "B", "topic_name": "Циклы",
             "verdict": "solved", "duration": 1.0, "task_node_id": 1, "task_name": "Зад1"},
            {"model_key": "B", "model_title": "B", "topic_name": "Строки",
             "verdict": "failed", "duration": 1.0, "task_node_id": 3, "task_name": "Зад3"},
        ]
        report = _build_batch_report(results)
        # Объём: 2 модели, 3 задачи, 4 пары.
        self.assertEqual(report["models_count"], 2)
        self.assertEqual(report["tasks_count"], 3)
        self.assertEqual(report["total_pairs"], 4)
        # Ошибки по темам: Циклы (failed+skipped) выше Строки (сортировка по count desc).
        errors = report["errors_by_topic"]
        self.assertEqual([e["topic"] for e in errors], ["Циклы", "Строки"])
        self.assertEqual(errors[0]["count"], 2)
        self.assertEqual(errors[0]["items"], ["Зад1 × A", "Зад2 × A"])
        self.assertEqual(errors[0]["hidden"], 0)
        self.assertEqual(errors[1]["items"], ["Зад3 × B"])
        # Решённые пары в ошибки не попадают.
        self.assertFalse(any("Зад1 × B" in item for e in errors for item in e["items"]))

    def test_errors_items_cap_and_hidden(self):
        """Кап позиций на тему (6): остальное — hidden, для компактности."""
        from ai.arm_runner import _REPORT_ERRORS_ITEM_CAP, _build_batch_report

        results = [
            {"model_key": "A", "model_title": "A", "topic_name": "Циклы",
             "verdict": "failed", "duration": 1.0, "task_node_id": i, "task_name": f"Зад{i}"}
            for i in range(10)
        ]
        report = _build_batch_report(results)
        row = report["errors_by_topic"][0]
        self.assertEqual(row["count"], 10)
        self.assertEqual(len(row["items"]), _REPORT_ERRORS_ITEM_CAP)
        self.assertEqual(row["hidden"], 4)

    def test_attach_run_meta(self):
        """Время прогона: завершён — длительность конец-старт; идёт — elapsed."""
        from ai.arm_runner import _attach_run_meta

        report = {}
        _attach_run_meta(report, started_ts=1000.0, finished_ts=1900.0)
        self.assertEqual(report["run_meta"]["started_at_ts"], 1000.0)
        self.assertEqual(report["run_meta"]["finished_at_ts"], 1900.0)
        self.assertEqual(report["run_meta"]["duration_seconds"], 900.0)
        self.assertFalse(report["run_meta"]["running"])

        running = {}
        _attach_run_meta(running, started_ts=1000.0, finished_ts=None, now_ts=1600.0)
        self.assertIsNone(running["run_meta"]["finished_at_ts"])
        self.assertTrue(running["run_meta"]["running"])
        self.assertEqual(running["run_meta"]["duration_seconds"], 600.0)

        empty = {}
        _attach_run_meta(None, 1000.0)  # не падает на пустом отчёте
        _attach_run_meta(empty, None)
        self.assertIsNone(empty["run_meta"]["started_at_ts"])


class BatchSaveSolutionsTests(TestCase):
    """Чекбокс «Сохранять решённые задачи» в batch-solve: пары, прошедшие
    DL-тест (verdict solved), попадают в кэш TaskSolution по правилам
    solution_cache.record_batch_solution; уже решённые (узел+язык) не трогаются.
    Прогоняется синхронно (_run_batch_job_worker), как BatchRunnerIntegrationTests.
    """

    def setUp(self):
        from ai.models import Task
        self.user = get_user_model().objects.create_user(username="saver", password="x")
        self.lang = ProgrammingLanguage.objects.create(language_name="Pascal")
        self.topic = Topic.objects.create(topic_name_ru="Линейные", programming_language=self.lang)
        self.t1 = Task.objects.create(
            node_id=7001, task_id=8001, name="A", statement="Сложите a и b",
            topic=self.topic, programming_language=self.lang, file_extension=".pas",
        )

    def _run(self, save_solutions, dl_mock):
        import time as _t
        from ai import arm_runner

        async def fake_handler(messages, conv_id):
            return ("program a; begin writeln(1); end.", 12)

        run_id = "save-sol-" + uuid.uuid4().hex[:8]
        now_ts = _t.time()
        arm_runner._jobs[run_id] = {
            "run_id": run_id, "run_type": "batch", "status": "running",
            "error_message": "", "total_models": 1, "total_pairs": 1,
            "completed_pairs": 0, "completed_models": 0,
            "current_model_key": "FakeModel", "current_model_title": "FakeModel",
            "current_task_node_id": "", "current_task_name": "",
            "results": [], "report": None,
            "created_at_ts": now_ts, "updated_at_ts": now_ts,
        }
        try:
            with patch("ai.arm_runner._test_solution_on_dl", dl_mock):
                arm_runner._run_batch_job_worker(
                    run_id, [self.t1.node_id],
                    [{"key": "FakeModel", "title": "FakeModel", "handler": fake_handler}],
                    self.user.id, "DLSID-1",
                    ui_language="Русский", dl_test=True,
                    solve_file_extension=".pas", solve_prog_lang_name="Pascal",
                    programming_language_id=self.lang.id,
                    course_id=1450,
                    run_params={"save_solutions": save_solutions},
                    save_solutions=save_solutions,
                )
        finally:
            arm_runner._jobs.pop(run_id, None)
        return run_id

    @staticmethod
    def _dl_ok():
        def dl_mock(sid, node_id, code, ext, **kw):
            return {
                "verdict": "solved", "comment": "Все тесты успешно пройдены",
                "submit_error": "", "queue_id": 11, "code_sent": code,
            }
        return dl_mock

    def test_solved_pair_saved_to_cache(self):
        from ai.models import TaskSolution
        run_id = self._run(True, self._dl_ok())

        solution = TaskSolution.objects.filter(
            task_node_id=self.t1.node_id,
            programming_language_id=self.lang.id,
        ).first()
        self.assertIsNotNone(solution)
        self.assertEqual(solution.verdict, TaskSolution.VERDICT_PASSED)
        self.assertEqual(solution.code, "program a; begin writeln(1); end.")
        self.assertEqual(solution.model_key, "FakeModel")
        self.assertEqual(solution.model_title, "FakeModel")
        self.assertEqual(solution.file_extension, ".pas")
        self.assertEqual(solution.dl_comment, "Все тесты успешно пройдены")
        self.assertEqual(solution.topic_id, self.topic.id)
        self.assertEqual(solution.course_id, 1450)
        self.assertEqual(solution.created_by_id, self.user.id)
        # Внешнего DL-аккаунта нет — фолбэк _resolve_user: username.
        self.assertEqual(solution.external_user_id, "saver")
        # Флаг дошёл до живого result_item (для пометки в UI).
        from ai import arm_runner
        # (job уже вычищен — проверяем через снапшот БД: флаг живёт только в
        # live-item'ах, поэтому проверяем run_params и наличие записи кэша.)
        from ai.models import AIModelTestRun
        run = AIModelTestRun.objects.get(run_id=run_id)
        self.assertTrue(run.run_params.get("save_solutions"))
        # queue_id сброшен — вердикт финальный.
        self.assertFalse(solution.queue_id)

    def test_already_passed_is_ignored(self):
        from ai.models import TaskSolution
        TaskSolution.objects.create(
            task_node_id=self.t1.node_id,
            programming_language_id=self.lang.id,
            code="OLD CODE", verdict=TaskSolution.VERDICT_PASSED,
            model_key="OldModel",
        )
        self._run(True, self._dl_ok())
        solution = TaskSolution.objects.get(
            task_node_id=self.t1.node_id,
            programming_language_id=self.lang.id,
        )
        # Уже решённая — игнорируем: код и модель не перетёрты.
        self.assertEqual(solution.code, "OLD CODE")
        self.assertEqual(solution.model_key, "OldModel")

    def test_failed_pair_not_saved(self):
        from ai.models import TaskSolution

        def dl_fail(sid, node_id, code, ext, **kw):
            return {
                "verdict": "failed", "comment": "Программа завершилась с ошибкой",
                "submit_error": "", "queue_id": 12, "code_sent": code,
            }

        self._run(True, dl_fail)
        self.assertFalse(
            TaskSolution.objects.filter(
                task_node_id=self.t1.node_id,
                programming_language_id=self.lang.id,
            ).exists()
        )

    def test_checkbox_off_saves_nothing(self):
        from ai.models import TaskSolution
        self._run(False, self._dl_ok())
        self.assertFalse(
            TaskSolution.objects.filter(
                task_node_id=self.t1.node_id,
                programming_language_id=self.lang.id,
            ).exists()
        )

    def test_record_batch_solution_unit(self):
        """record_batch_solution: saved / skipped (passed уже есть) / пустой код."""
        from ai.services.solution_cache import record_batch_solution
        from ai.models import TaskSolution

        self.assertEqual(
            record_batch_solution(
                node_id=self.t1.node_id, programming_language_id=self.lang.id,
                code="CODE1", dl_comment="ок", model_key="M", model_title="M",
                course_id=1, created_by=self.user,
            ),
            "saved",
        )
        self.assertEqual(
            record_batch_solution(
                node_id=self.t1.node_id, programming_language_id=self.lang.id,
                code="CODE2",
            ),
            "skipped",
        )
        self.assertEqual(TaskSolution.objects.get(
            task_node_id=self.t1.node_id,
            programming_language_id=self.lang.id,
        ).code, "CODE1")
        # Пустой код — не пишем (нет решения).
        self.assertEqual(
            record_batch_solution(node_id=999999, programming_language_id=None, code=""),
            None,
        )


class TaskModelTests(TestCase):
    """Task model basics (DL task reference for batch-solve ARM)."""

    def test_node_id_unique_and_str(self):
        from django.db import transaction
        from ai.models import Task
        Task.objects.create(node_id=12345, name="Сумма двух чисел")
        # Wrap the expected unique violation in a savepoint so the TestCase's
        # outer transaction is not left broken for subsequent tests.
        with transaction.atomic():
            with self.assertRaises(Exception):
                Task.objects.create(node_id=12345)
        t = Task.objects.get(node_id=12345)
        self.assertEqual(str(t), "Сумма двух чисел")
        t2 = Task.objects.create(node_id=99)
        self.assertEqual(str(t2), "DL #99")

    def test_active_default_true(self):
        from ai.models import Task
        t = Task.objects.create(node_id=555)
        self.assertTrue(t.active)


class ExtractCodeFromResponseTests(SimpleTestCase):
    """_extract_code_from_response: оградки, проза и think-блоки (ARM batch).

    Регрессия на баг: «Извлечённый код программы» получал цепочку рассуждений
    модели («We need answer solve task…») вместо кода.
    """

    def setUp(self):
        from ai.arm_runner import _extract_code_from_response
        self.extract = _extract_code_from_response

    # Литерал think-тега в исходнике хрупок (см. decide_task.js THINK_OPEN) —
    # собираем из кусков.
    THINK = "<" + "think>"
    THINK_END = "<" + "/think>"

    def test_plain_code_without_fences_passthrough(self):
        code = "program a; begin writeln(1); end."
        self.assertEqual(self.extract(code), code)

    def test_code_fence_extracted(self):
        text = "Вот решение:\n```asm\nmov ax, 1\nadd ax, 2\n```\nУдачи!"
        self.assertEqual(self.extract(text), "mov ax, 1\nadd ax, 2")

    def test_cot_without_fences_returns_empty(self):
        cot = (
            "We need answer solve task in i8086 assembly per rules. "
            "Need provide code only. We need check the condition first, "
            "it is simple: if y >= x then we must compute the product and "
            "the quotient, so all operands can be converted to words, "
            "and the result is stored in R because idiv needs signed "
            "division here. We need use intermediate variables for that."
        )
        self.assertEqual(self.extract(cot), "")

    def test_prose_fence_skipped_real_code_chosen(self):
        prose = (
            "Need check the condition, it is simple: all operands "
            "are converted to words first, so we must use idiv and "
            "the result is stored in R. We need be careful with the "
            "signed division because it is not the same as div, and "
            "the quotient can be wrong if we forget cwd. There is no "
            "overflow in our example, so it is fine."
        )
        real = "mov al, a\ncbw\nmov a_16, ax\nidiv b_16\nmov R, ax"
        text = f"```asm\n{prose}\n```\n```asm\n{real}\n```"
        self.assertEqual(self.extract(text), real)

    def test_think_wrapped_code_extracted(self):
        think = (
            self.THINK + "Need think about the condition, we must check "
            "the variables first, it is simple enough for the signed "
            "arithmetic and the result is stored in R." + self.THINK_END
        )
        code = "mov ax, x\nsub ax, y\nimul a_16\nmov R, ax"
        text = f"{think}\n```asm\n{code}\n```"
        self.assertEqual(self.extract(text), code)

    def test_all_fences_are_prose_returns_empty(self):
        prose = (
            "Need check the condition, it is simple: all operands "
            "are converted to words first, so we must use idiv and "
            "the result is stored in R because the quotient can be "
            "negative here, and the overflow is not possible in our "
            "case at all."
        )
        text = f"```asm\n{prose}\n```"
        self.assertEqual(self.extract(text), "")

    # === «Альтернатива/Вариант 2» с кодом второго решения (срез) ===

    def test_alternative_section_cut_keeps_main_code(self):
        # Второй блок длиннее первого — без среза «победил бы» он (longest wins).
        main = "mov ax, x\nsub ax, y\nmov R, ax"
        alt = "mov bx, z\nadd bx, 3\nadd bx, 4\nadd bx, 5\nmov R, bx"
        text = f"```asm\n{main}\n```\nАльтернатива:\n```asm\n{alt}\n```"
        self.assertEqual(self.extract(text), main)

    def test_alternative_heading_in_unclosed_fence_cut(self):
        # Модель не закрыла оградку: всё после неё тянулось бы в код вместе
        # с заголовком «Альтернатива:» и вторым вариантом.
        main = "mov ax, x\nsub ax, y"
        text = f"```asm\n{main}\nАльтернатива:\nmov bx, z\nadd bx, 1"
        self.assertEqual(self.extract(text), main)

    def test_carved_section_skips_alternative_code(self):
        # Без оградок: карвинг «перепрыгивал» заголовок зазором ≤2 не-кодовых
        # строк и клеил оба решения в один фрагмент.
        main = "mov ax, x\nsub ax, y\nmov R, ax"
        alt = "mov bx, z\nadd bx, 1"
        text = f"{main}\nАльтернатива:\n{alt}"
        extracted = self.extract(text)
        self.assertIn("mov ax, x", extracted)
        self.assertNotIn("Альтернатива", extracted)
        self.assertNotIn("mov bx, z", extracted)

    def test_variant_heading_before_code_not_cut(self):
        # «Вариант 1:» открывает ЕДИНСТВЕННЫЙ код-блок — среза быть не должно.
        text = "Вариант 1:\n```asm\nmov ax, x\nsub ax, y\nmov R, ax\n```"
        self.assertEqual(self.extract(text), "mov ax, x\nsub ax, y\nmov R, ax")

    def test_english_alternative_heading_cut(self):
        main = "mov ax, x\nsub ax, y\nmov R, ax"
        text = f"```asm\n{main}\n```\nAlternative solution:\n```asm\nmov bx, z\nadd bx, 1\n```"
        self.assertEqual(self.extract(text), main)


class OllamaNumPredictTests(SimpleTestCase):
    """Потолок генерации Ollama: env OLLAMA_NUM_PREDICT, дефолт 32768
    (4096 → 8192 → 16384 → 32768: think-рассуждения думающих моделей сжигают
    бюджет ДО кода — «выкусывание ответа»), env-переопределение и нижняя
    граница."""

    def test_default_32768_and_env_override(self):
        from ai.model_clients import ollama
        self.assertEqual(ollama._default_num_predict(), 32768)
        with patch.dict(os.environ, {"OLLAMA_NUM_PREDICT": "2048"}):
            self.assertEqual(ollama._default_num_predict(), 2048)
        with patch.dict(os.environ, {"OLLAMA_NUM_PREDICT": "16"}):
            # Слишком малые значения не допускаются (нижняя граница 1024).
            self.assertEqual(ollama._default_num_predict(), 1024)


class AskOllamaCutFlagTests(SimpleTestCase):
    """_ask_ollama: 4-й элемент кортежа — честный сигнал обреза от done_reason.
    Не «stop» (длина) → True; «stop» → False; пустой контент с непустым
    thinking: обрезанное рассуждение → True, иначе False."""

    @staticmethod
    def _run_with_chat_sync(result):
        """result — кортеж _chat_sync: (content, tokens, thinking, done_reason).

        Возвращает кортеж _ask_ollama: (текст, токены_ответа, is_error,
        обрезан, токены_промпта) — 5 элементов (контракт провайдеров)."""
        from asyncio import run as _run
        from unittest.mock import patch
        from ai.model_clients import ollama
        with patch.object(ollama, "_chat_sync", lambda *a, **kw: result):
            return _run(ollama._ask_ollama("msg", 1, "test:model"))

    def test_done_reason_stop_not_truncated(self):
        text, tokens, is_err, cut, _prompt = self._run_with_chat_sync(("ok", 5, "", "stop", 3))
        self.assertEqual((text, tokens, is_err, cut), ("ok", 5, False, False))

    def test_done_reason_length_truncated(self):
        text, tokens, is_err, cut, _prompt = self._run_with_chat_sync(("partial code block", 16384, "", "length", 3))
        self.assertEqual((text, tokens, is_err, cut), ("partial code block", 16384, False, True))

    def test_thinking_fallback_cut_flag(self):
        # content пуст, thinking есть — обрезанное рассуждение → флаг True
        text, tokens, is_err, cut, _prompt = self._run_with_chat_sync(("", 1998, "We need to think...", "length", 3))
        self.assertEqual((text, tokens, is_err, cut), ("We need to think...", 1998, False, True))
        # рассуждение дошло до конца, но контента не родил — не «обрез»
        text, tokens, is_err, cut, _prompt = self._run_with_chat_sync(("", 50, "all thinking done", "stop", 3))
        self.assertEqual((text, tokens, is_err, cut), ("all thinking done", 50, False, False))

    def test_empty_content_is_error(self):
        text, tokens, is_err, cut, _prompt = self._run_with_chat_sync(("", 0, "", "stop", 3))
        self.assertTrue(is_err)
        self.assertFalse(cut)
        self.assertIn("пустой ответ", text)


class OllamaNoThinkTests(SimpleTestCase):
    """Ретрай «без think» (Ollama API think=False — docs.ollama.com/
    capabilities/thinking: «false → request no thinking output»): поле уходит
    в запрос при think=False и НЕ подставляется вовсе при дефолте (совместимость
    со старыми SDK); пары «обычный → no-think» ищутся по имени handler'а;
    провайдер без переключателя → None. Думающие модели подводят протекшим
    reasoning'ом — ARM переспрашивает ту же модель в режиме «только ответ»."""

    @staticmethod
    def _fake_client(seen):
        class FakeMessage:
            content = "x"
            thinking = ""
        class FakeChunk:
            message = FakeMessage()
            done_reason = "stop"
            eval_count = 1
        class FakeClient:
            def chat(self, **kwargs):
                seen.update(kwargs)
                return [FakeChunk()]
        return FakeClient()

    def test_stream_once_forwards_think_false_and_default_omits(self):
        from ai.model_clients import ollama
        seen = {}
        with patch.object(ollama, "_get_client", lambda: self._fake_client(seen)):
            content, tokens, thinking, done, _prompt = ollama._stream_once("m", "msg", 0.7, 100, think=False)
        self.assertEqual(content, "x")
        self.assertEqual(seen.get("think"), False)
        # Дефолт — поле в запрос вообще не идёт (старые SDK без think-поля).
        seen.clear()
        with patch.object(ollama, "_get_client", lambda: self._fake_client(seen)):
            ollama._stream_once("m", "msg", 0.7, 100)
        self.assertNotIn("think", seen)

    def test_nothink_handler_calls_with_think_false(self):
        from asyncio import run as _run
        from ai.model_clients import ollama
        seen = {"think": "not-called"}
        def fake_chat_sync(model, msg, temperature, num_predict, think=None):
            seen["think"] = think
            return ("program p;", 5, "", "stop", 3)
        handler = ollama.nothink_handler(ollama.ask_Ollama_Gemma_4_Cloud_async)
        self.assertIsNotNone(handler)  # все Ollama-модели получили no-think пару
        with patch.object(ollama, "_chat_sync", fake_chat_sync):
            text, tokens, is_err, cut, _prompt = _run(handler("м", 1))  # 5-кортеж, как у основных
        self.assertEqual((text, tokens, is_err), ("program p;", 5, False))
        self.assertFalse(seen["think"])

    def test_get_no_think_handler_provider_dispatch(self):
        from ai.model_clients import get_no_think_handler
        from ai.model_clients import ollama, registry
        handler = registry.get("Ollama_Gemma_4_Cloud")["handler"]
        self.assertIs(get_no_think_handler(handler), ollama.nothink_handler(handler))
        # web-пул/фейк без ollama-модуля → обычный ретрай (переключателя нет).
        async def web_like(messages, conv_id):
            return None
        self.assertIsNone(get_no_think_handler(web_like))

    def test_supports_think_provider_dispatch(self):
        from ai.model_clients import supports_think
        from ai.model_clients import ollama, registry
        self.assertTrue(supports_think(registry.get("Ollama_Gemma_4_Cloud")["handler"]))
        self.assertTrue(supports_think(ollama.ask_Ollama_Gemma_4_Cloud_async))
        async def web_like(messages, conv_id):
            return None
        self.assertFalse(supports_think(web_like))

    def test_main_handler_forwards_client_think(self):
        # Клиентский переключатель: основной ollama-хендлер принимает kwarg
        # think и передаёт его в _chat_sync; None — поведение модели по умолчанию.
        from asyncio import run as _run
        from ai.model_clients import ollama
        seen = {"think": "not-called"}
        def fake_chat_sync(model, msg, temperature, num_predict, think=None):
            seen["think"] = think
            return ("код", 9, "", "stop", 3)
        with patch.object(ollama, "_chat_sync", fake_chat_sync):
            text, tokens, is_err, cut, _prompt = _run(
                ollama.ask_Ollama_Gemma_4_Cloud_async("м", 1, think=True))
        self.assertTrue(seen["think"])


class IsOutputTruncatedTests(SimpleTestCase):
    """is_output_truncated: обрез по факту tokens >= потолка провайдера
    (обрезка только при известном потолке — false positives хуже пропусков)."""

    def test_ollama_cut_at_num_predict(self):
        from ai.model_clients import is_output_truncated
        with patch.dict(os.environ, {"OLLAMA_NUM_PREDICT": "16384"}):
            self.assertFalse(is_output_truncated("Ollama_DeepSeek_V4_1_Flash_Cloud", 0))
            self.assertFalse(is_output_truncated("Ollama_DeepSeek_V4_1_Flash_Cloud", 16383))
            self.assertTrue(is_output_truncated("Ollama_DeepSeek_V4_1_Flash_Cloud", 16384))
        with patch.dict(os.environ, {"OLLAMA_NUM_PREDICT": "2048"}):
            self.assertFalse(is_output_truncated("Ollama_DeepSeek_V4_1_Flash_Cloud", 2047))
            self.assertTrue(is_output_truncated("Ollama_DeepSeek_V4_1_Flash_Cloud", 2048))

    def test_unknown_provider_is_never_truncated(self):
        from ai.model_clients import is_output_truncated
        self.assertFalse(is_output_truncated("Web_DeepSeek", 99999))
        self.assertFalse(is_output_truncated("Web_Kimi", 99999))
        self.assertFalse(is_output_truncated("", 16384))

    def _fake_registry(self, module_name):
        from ai.model_clients import registry

        class _FakeHandler:
            __module__ = module_name

        return {"handler": _FakeHandler()}

    def test_openrouter_ceiling_from_table(self):
        """Выкусывание для ВСЕХ моделей, а не только Ollama/SambaNova:
        потолок OpenRouter = max_tokens его таблицы моделей."""
        from ai.model_clients import is_output_truncated, registry
        with patch.object(
            registry, "get",
            lambda key: self._fake_registry("ai.model_clients.openrouter"),
        ):
            self.assertTrue(is_output_truncated("OR_GPT_OSS_20B", 8000))
            self.assertFalse(is_output_truncated("OR_GPT_OSS_20B", 7999))

    def test_groq_ceiling_from_table(self):
        from ai.model_clients import is_output_truncated, registry
        with patch.object(
            registry, "get",
            lambda key: self._fake_registry("ai.model_clients.groq"),
        ):
            self.assertTrue(is_output_truncated("Groq_Gpt_Oss_120B", 4096))
            self.assertFalse(is_output_truncated("Groq_Gpt_Oss_120B", 4095))

    def test_sambanova_ceiling_from_table(self):
        # Провайдер определяется по __module__ обработчика из реестра (ключи
        # SambaNova без префикса). В тестовых сеттингах SambaNova отключена —
        # подменяем registry.get на заглушку с фейковым handler'ом модуля.
        from ai.model_clients import is_output_truncated, registry

        class _FakeSambanovaHandler:
            __module__ = "ai.model_clients.sambanova"

        with patch.object(registry, "get", lambda key: {"handler": _FakeSambanovaHandler()}):
            self.assertTrue(is_output_truncated("DeepSeek_V3_1", 9000))
            self.assertFalse(is_output_truncated("DeepSeek_V3_1", 8999))
        # Через префиксный fallback SambaNova не ловится (без реестра —
        # консервативно «не обрезан»).
        self.assertFalse(is_output_truncated("DeepSeek_V3_1", 9000))


class ResetModelStatsCommandTests(TestCase):
    """reset_model_stats: удаляет накопленную статистику моделей."""

    def test_command_deletes_stats_rows(self):
        from ai.models import AIModelStats
        AIModelStats.objects.create(
            model_key="Ollama_X", model_title="X",
            solved_count=1, total_count=2, duration_count=1, duration_sum=10.0,
        )
        out = StringIO()
        call_command("reset_model_stats", stdout=out)
        self.assertEqual(AIModelStats.objects.count(), 0)
        self.assertIn("удалено 1", out.getvalue())


from io import StringIO
from django.core.management import call_command


class DeleteArmRunCommandTests(TestCase):
    """delete_arm_run: точечное удаление batch-прогона из БД.

    Удаляет прогон (+CASCADE результаты), запись журнала прогона (CASCADE —
    закрепления), опционально решения кэша; вычитает вклад пар прогона из
    AIModelStats (только при run_params.record_stats) с полом на нуле.
    """

    RUN_ID = "a" * 32

    def _make_fixtures(self):
        self.superuser = get_user_model().objects.create_superuser(username='dr_admin', password='x', email='d@t.com')
        from ai.models import (
            AIModelStats, AIModelTestResult, AIModelTestRun, AIRequestLog,
            AIPinnedBatchRun, TaskSolution,
        )
        run = AIModelTestRun.objects.create(
            run_id=self.RUN_ID, run_type="batch", status="completed",
            run_name="Плохой прогон", run_params={"record_stats": True},
            started_at=timezone.now(), finished_at=timezone.now(),
        )
        AIModelTestResult.objects.create(
            run=run, model_key="M_1", model_title="M1", status="ok",
            verdict="solved", duration_seconds=10.0,
        )
        AIModelTestResult.objects.create(
            run=run, model_key="M_2", model_title="M2", status="error",
            verdict="failed", duration_seconds=8.0,
        )
        AIModelTestResult.objects.create(
            run=run, model_key="M_3", model_title="M3", status="error",
            verdict="skipped", duration_seconds=None,
        )
        AIModelStats.objects.create(
            model_key="M_1", model_title="M1", solved_count=5, total_count=8,
            duration_count=4, duration_sum=40.0,
        )
        AIModelStats.objects.create(
            model_key="M_2", model_title="M2", solved_count=2, total_count=3,
            duration_count=2, duration_sum=12.0,
        )
        log = AIRequestLog.objects.create(
            user=None, source="arm", mode="batch_solve",
            message=f"Batch solve run {self.RUN_ID}",
            status=AIRequestLog.STATUS_SUCCESS, sent_at=timezone.now(),
        )
        pin = AIPinnedBatchRun.objects.create(user=self.superuser, log=log)
        sol = TaskSolution.objects.create(
            task_node_id=1, programming_language_id=1, code="x", verdict="passed",
            test_log=log,
        )
        return run, log, pin, sol

    def test_dry_run_deletes_nothing(self):
        run, log, pin, sol = self._make_fixtures()
        out = StringIO()
        call_command("delete_arm_run", run_id=self.RUN_ID, stdout=out)
        self.assertIn("DRY-RUN", out.getvalue())
        self.assertTrue(run.__class__.objects.filter(pk=run.pk).exists())

    def test_dry_run_keeps_stats_untouched(self):
        # DRY-RUN не только не удаляет прогон — он и статистику не вычитает.
        from ai.models import AIModelStats
        self._make_fixtures()
        call_command("delete_arm_run", run_id=self.RUN_ID, stdout=StringIO())
        m1 = AIModelStats.objects.get(model_key="M_1")
        self.assertEqual((m1.solved_count, m1.total_count), (5, 8))
        self.assertEqual((m1.duration_count, m1.duration_sum), (4, 40.0))

    def test_yes_deletes_run_log_pin_and_subtracts_stats(self):
        run, log, pin, sol = self._make_fixtures()
        from ai.models import (
            AIModelStats, AIModelTestResult, AIModelTestRun, AIPinnedBatchRun,
            AIRequestLog, TaskSolution,
        )
        out = StringIO()
        call_command("delete_arm_run", run_id=self.RUN_ID, yes=True, stdout=out)
        # Прогон, результаты, лог и закрепление ушли; решение кэша осталось.
        self.assertEqual(AIModelTestRun.objects.count(), 0)
        self.assertEqual(AIModelTestResult.objects.count(), 0)
        self.assertEqual(AIRequestLog.objects.count(), 0)
        self.assertEqual(AIPinnedBatchRun.objects.count(), 0)
        self.assertTrue(TaskSolution.objects.filter(pk=sol.pk).exists())
        # M_1 вычтено (1 solved/1 пара, 1 замер/10 сек), M_2 (0/1, 1/8).
        m1 = AIModelStats.objects.get(model_key="M_1")
        self.assertEqual((m1.solved_count, m1.total_count), (4, 7))
        self.assertEqual((m1.duration_count, m1.duration_sum), (3, 30.0))
        m2 = AIModelStats.objects.get(model_key="M_2")
        self.assertEqual((m2.solved_count, m2.total_count), (2, 2))
        self.assertEqual((m2.duration_count, m2.duration_sum), (1, 4.0))
        # M_3 был только skipped — в статистику не вносил, строк нет.
        self.assertFalse(AIModelStats.objects.filter(model_key="M_3").exists())

    def test_record_stats_off_keeps_stats(self):
        run, log, pin, sol = self._make_fixtures()
        from ai.models import AIModelStats
        run.run_params = {}
        run.save(update_fields=["run_params"])
        AIModelStats.objects.update_or_create(
            model_key="M_1",
            defaults={"solved_count": 3, "total_count": 4,
                      "duration_count": 2, "duration_sum": 20.0},
        )
        call_command("delete_arm_run", run_id=self.RUN_ID, yes=True, stdout=StringIO())
        stats = AIModelStats.objects.get(model_key="M_1")
        self.assertEqual((stats.solved_count, stats.total_count), (3, 4))

    def test_stats_floor_at_zero(self):
        run, log, pin, sol = self._make_fixtures()
        from ai.models import AIModelStats
        AIModelStats.objects.update_or_create(
            model_key="M_1",
            defaults={"solved_count": 1, "total_count": 1,
                      "duration_count": 1, "duration_sum": 1.0},
        )
        call_command("delete_arm_run", run_id=self.RUN_ID, yes=True, stdout=StringIO())
        stats = AIModelStats.objects.get(model_key="M_1")
        self.assertEqual(stats.solved_count, 0)
        self.assertEqual(stats.duration_sum, 0.0)

    def test_drop_solutions_removes_cached_solutions(self):
        run, log, pin, sol = self._make_fixtures()
        from ai.models import TaskSolution
        call_command("delete_arm_run", run_id=self.RUN_ID, yes=True,
                     drop_solutions=True, stdout=StringIO())
        self.assertEqual(TaskSolution.objects.count(), 0)

    def test_list_lists_batch_runs(self):
        self._make_fixtures()
        out = StringIO()
        call_command("delete_arm_run", **{"list": True}, stdout=out)
        self.assertIn(self.RUN_ID, out.getvalue())
        self.assertIn("Плохой прогон", out.getvalue())


class RerunUnsolvedPairsTests(TestCase):
    """Попарный перезапуск нерешённых пар со слиянием в тот же прогон.

    Регрессия на «Перезапустить прогон» со полным новым прогоном: было
    40 реш / 13 не реш → перезапуск 13 пар (задача × модель) → 10 решились →
    в отчёте 50 реш / 3 не реш. Строки обновляются update_or_create в том же
    run; финализирующий отчёт строится из всех строк прогона.
    """

    def setUp(self):
        from django.utils import timezone as tz
        from ai.models import AIModelTestResult, AIModelTestRun
        from ai.models import ProgrammingLanguage, Task, Topic

        self.user = get_user_model().objects.create_user(username="reruner", password="x")
        self.lang = ProgrammingLanguage.objects.create(language_name="Pascal")
        self.topic = Topic.objects.create(topic_name_ru="Линейные", programming_language=self.lang)
        self.t1 = Task.objects.create(
            node_id=3001, task_id=30001, name="A", statement="Выведи 1",
            topic=self.topic, programming_language=self.lang, file_extension=".pas",
        )
        self.t2 = Task.objects.create(
            node_id=3002, task_id=30002, name="B", statement="Выведи 2",
            topic=self.topic, programming_language=self.lang, file_extension=".pas",
        )
        self.started = tz.now()
        self.run = AIModelTestRun.objects.create(
            run_id="rerun-target-1",
            run_type=AIModelTestRun.RUN_TYPE_BATCH,
            user=self.user,
            status=AIModelTestRun.STATUS_COMPLETED,
            started_at=self.started,
            finished_at=tz.now(),
            message="Batch solve: 2 задач × 1 моделей",
            total_models=1,
            run_params={
                "node_ids": [self.t1.node_id, self.t2.node_id],
                "model_keys": ["FakeModel"],
                "language_id": self.lang.id,
                "file_extension": ".pas",
                "dl_test": True,
                "ui_language": "Русский",
                "record_stats": False,
                "save_solutions": False,
                "run_name": "Контрольный",
            },
        )
        # Пара 1 решена, пара 2 провалилась (код не извлечён) — кандидаты на перезапуск.
        AIModelTestResult.objects.create(
            run=self.run, model_key="FakeModel", model_title="FakeModel",
            task=self.t1, status="ok", verdict="solved",
            dl_queue_id=1, dl_comment="Все тесты успешно пройдены",
            file_extension_snapshot=".pas", topic_name_snapshot="Линейные",
            prog_lang_snapshot="Pascal",
        )
        AIModelTestResult.objects.create(
            run=self.run, model_key="FakeModel", model_title="FakeModel",
            task=self.t2, status="error", verdict="failed",
            dl_queue_id=0, dl_comment="Не удалось извлечь код из ответа модели",
            file_extension_snapshot=".pas", topic_name_snapshot="Линейные",
            prog_lang_snapshot="Pascal",
        )

    def test_collect_unsolved_pairs_only_non_solved(self):
        from ai.arm_runner import collect_unsolved_pairs

        node_ids, pairs_map = collect_unsolved_pairs(self.run)
        self.assertEqual(node_ids, [self.t2.node_id])
        self.assertEqual(pairs_map, {self.t2.node_id: {"FakeModel"}})

    def test_start_rerun_rejects_when_running(self):
        from django.core.cache import cache

        from ai import arm_runner
        from ai.models import AIModelTestRun

        arm_runner.AIModelTestRun.objects.filter(pk=self.run.pk).update(
            status=AIModelTestRun.STATUS_RUNNING,
        )
        new_run_id, error, dropped = arm_runner.start_batch_rerun_pairs(
            self.run.run_id, self.user.id, "DLSID-1",
        )
        self.assertIsNone(new_run_id)
        self.assertIn("выполняется", error)
        self.assertEqual(dropped, [])

    def test_start_rerun_rejects_when_all_solved(self):
        from ai import arm_runner
        from ai.models import AIModelTestResult

        AIModelTestResult.objects.filter(run=self.run, task=self.t2).update(verdict="solved")
        new_run_id, error, _dropped = arm_runner.start_batch_rerun_pairs(
            self.run.run_id, self.user.id, "DLSID-1",
        )
        self.assertIsNone(new_run_id)
        self.assertIn("Все пары решены", error)

    def test_start_rerun_rejects_unknown_run(self):
        from ai import arm_runner

        new_run_id, error, _dropped = arm_runner.start_batch_rerun_pairs(
            "no-such-run", self.user.id, "DLSID-1",
        )
        self.assertIsNone(new_run_id)
        self.assertIn("не найден", error)

    def test_rerun_merges_into_same_run(self):
        """Перезапуск 1 пары: строка обновлена в том же run, отчёт из всех строк."""
        import time as _t
        from unittest.mock import patch

        from ai import arm_runner
        from ai.models import AIModelTestResult, AIModelTestRun

        async def fake_handler(messages, conv_id):
            return ("program r; begin writeln(2); end.", 5)

        ordered_models = [{
            "key": "FakeModel", "title": "FakeModel",
            "handler": fake_handler,
        }]
        run_id = self.run.run_id
        now_ts = _t.time()
        # Pre-seed живой job перезапуска (как в start_batch_rerun_pairs).
        arm_runner._jobs[run_id] = {
            "run_id": run_id, "run_type": "batch", "rerun": True,
            "status": "running", "error_message": "",
            "total_models": 1, "total_pairs": 1, "completed_pairs": 0,
            "completed_models": 0, "current_model_key": "FakeModel",
            "current_model_title": "FakeModel", "current_task_node_id": "",
            "current_task_name": "", "results": [], "report": None,
            "run_name": "Контрольный · перезапуск",
            "created_at_ts": now_ts, "updated_at_ts": now_ts,
        }
        dl_ok = lambda sid, node_id, code, ext, **kw: {
            "verdict": "solved", "comment": "Все тесты успешно пройдены",
            "submit_error": "", "queue_id": 1, "code_sent": code,
        }
        try:
            with patch("ai.arm_runner._test_solution_on_dl", dl_ok):
                arm_runner._run_batch_job_worker(
                    run_id, [self.t2.node_id], ordered_models, self.user.id, "DLSID-1",
                    ui_language="Русский", dl_test=True,
                    rerun=True, rerun_pairs={self.t2.node_id: {"FakeModel"}},
                )
        finally:
            arm_runner._jobs.pop(run_id, None)

        run = AIModelTestRun.objects.get(run_id=run_id)
        # Строки НЕ дублируются: update_or_create в тот же run.
        rows = list(AIModelTestResult.objects.filter(run=run).order_by("task_id"))
        self.assertEqual(len(rows), 2)
        by_task = {r.task_id: r for r in rows}
        # Перезапущенная пара стала solved; решённая строка не тронута.
        self.assertEqual(by_task[self.t2.id].verdict, "solved")
        self.assertEqual(by_task[self.t2.id].dl_queue_id, 1)
        self.assertEqual(by_task[self.t1.id].verdict, "solved")
        # Отчёт финализирован из всех строк: 2 решено / 0 не решено.
        self.assertEqual(run.report["solved"], 2)
        self.assertEqual(run.report["failed"], 0)
        self.assertEqual(run.report["total_pairs"], 2)
        # Статус прогона вернулся в completed.
        self.assertEqual(run.status, AIModelTestRun.STATUS_COMPLETED)
        self.assertIsNotNone(run.finished_at)
        # Отсчёт времени «Прогон» — от исходного started_at.
        self.assertEqual(run.started_at, self.started)
        # Журнал: отдельная лог-строка перезапуска.
        self.assertTrue(
            AIRequestLog.objects.filter(message=f"Batch solve rerun {run_id}").exists(),
        )

    def test_rerun_snapshot_merges_results(self):
        """Живой перезапуск в снапшоте — все строки прогона, сводка обновляется."""
        import time as _t

        from ai import arm_runner

        run_id = self.run.run_id
        arm_runner._jobs[run_id] = {
            "run_id": run_id, "run_type": "batch", "rerun": True,
            "status": "running", "error_message": "",
            "total_models": 1, "total_pairs": 1, "completed_pairs": 0,
            "completed_models": 0, "current_model_key": "FakeModel",
            "current_model_title": "FakeModel", "current_task_node_id": "",
            "current_task_name": "", "results": [],
            "report": None, "run_name": "Контрольный · перезапуск",
            "created_at_ts": _t.time(), "updated_at_ts": _t.time(),
        }
        try:
            snapshot = arm_runner.get_arm_run_snapshot(run_id, light_results=True)
        finally:
            arm_runner._jobs.pop(run_id, None)
        # Обе строки видны, сводка — по всем парам прогона.
        self.assertEqual(len(snapshot["results"]), 2)
        self.assertEqual(snapshot["report"]["solved"], 1)
        self.assertEqual(snapshot["report"]["failed"], 1)


class RerunLogJournalTests(TestCase):
    """Лог попарного перезапуска ("Batch solve rerun <hex>") виден журналу.

    Регрессия: воркер перезапуска пишет AIRequestLog с message
    "Batch solve rerun <run_id>" (не "run"), а весь журнал разбирал run_id
    только по "Batch solve run <hex>" — детали перезапуска отрисовывались
    как обычный текст без таблицы, кнопка «Повторить прогон» давала 400,
    resend отправлял служебное сообщение модели как обычный чат.
    """

    def setUp(self):
        from ai.models import AIModelTestRun, AIModelTestResult, Task, ProgrammingLanguage, Topic
        self.factory = RequestFactory()
        self.superuser = get_user_model().objects.create_superuser(
            username="rerun_log_admin", password="x", email="rl@t.com",
        )
        self.lang = ProgrammingLanguage.objects.create(language_name="Pascal")
        self.topic = Topic.objects.create(
            topic_name_ru="Линейные", programming_language=self.lang,
        )
        self.task = Task.objects.create(
            node_id=7301, task_id=73001, name="RL", statement="s",
            topic=self.topic, programming_language=self.lang,
            file_extension=".pas",
        )
        self.run_id = "d" * 32
        self.test_run = AIModelTestRun.objects.create(
            run_id=self.run_id,
            run_type=AIModelTestRun.RUN_TYPE_BATCH,
            status=AIModelTestRun.STATUS_COMPLETED,
            course_id=1450,
            run_name="Контрольный · перезапуск",
        )
        AIModelTestResult.objects.create(
            run=self.test_run, task=self.task,
            model_key="FakeModel", model_title="FakeModel",
            status="ok", verdict="failed",
            dl_comment="[test 1]: Ошибка компиляции",
            file_extension_snapshot=".pas",
            topic_name_snapshot="Линейные", prog_lang_snapshot="Pascal",
        )
        self.rerun_log = AIRequestLog.objects.create(
            user=self.superuser, source="arm", mode="batch_solve",
            message=f"Batch solve rerun {self.run_id}",
            status=AIRequestLog.STATUS_ERROR, sent_at=timezone.now(),
        )

    def test_batch_run_id_matches_run_and_rerun(self):
        from ai.admin.request_logs import _batch_run_id_from_log
        self.assertEqual(
            _batch_run_id_from_log(self.rerun_log), self.run_id,
        )
        run_log = AIRequestLog(message=f"Batch solve run {self.run_id}")
        self.assertEqual(_batch_run_id_from_log(run_log), self.run_id)
        # Служебные сообщения прогресса — не run-id.
        self.assertIsNone(
            _batch_run_id_from_log(AIRequestLog(message="Batch solve: 2 задач × 1 моделей")),
        )
        self.assertIsNone(_batch_run_id_from_log(AIRequestLog(message="")))

    def test_is_batch_solve_log_accepts_rerun(self):
        from ai.admin.request_logs import _is_batch_solve_log
        self.assertTrue(_is_batch_solve_log(self.rerun_log))

    def test_detail_of_rerun_log_shows_results_table(self):
        from ai.admin.request_logs import admin_request_log_detail_view
        request = self.factory.get(f"/ai/admin/ai/request_logs/{self.rerun_log.id}/")
        request.user = self.superuser
        request.session = {}
        response = admin_request_log_detail_view(request, self.rerun_log.id)
        self.assertEqual(response.status_code, 200)
        html = response.render().content.decode()
        # Таблица результатов (общий partial), как у обычного batch-лога.
        self.assertTrue(response.context_data["is_batch_log"])
        # Скрипт развёртки вынесен в static — маркер ищем в файле.
        from pathlib import Path
        js = (
            Path(__file__).resolve().parent.parent / "static" / "admin" / "ai"
            / "request_logs_detail" / "index.js"
        ).read_text(encoding="utf-8")
        self.assertIn("ArmBatchResults.init", js)
        self.assertIn('id="batch-results-card"', html)

    def test_recent_batch_rows_include_rerun_log(self):
        from ai.admin.request_logs import build_recent_batch_rows
        request = self.factory.get("/ai/admin/")
        request.user = self.superuser
        rows = build_recent_batch_rows(request, limit=5)
        log_ids = {r["id"] for r in rows["recent_batch_runs"]}
        self.assertIn(self.rerun_log.id, log_ids)

    def test_resend_rerun_log_delegates_to_full_rerun(self):
        """«Повторить запрос» на rerun-логе ведёт в rerun-arm (новый прогон
        по всем парам), а не в resend одной модели."""
        from ai.admin import request_logs as logs_mod
        request = self.factory.post(f"/ai/admin/ai/request_logs/{self.rerun_log.id}/resend/")
        request.user = self.superuser
        request.session = {"external_session_id": "DLSID-1"}
        with patch(
            "ai.arm_runner.start_batch_solve_run",
            return_value=("f" * 32, ""),
        ) as start_mock:
            response = logs_mod.resend_request_view(request, self.rerun_log.id)
        data = json.loads(response.content)
        # Служебный текст лога ушёл не модели, а в полный перезапуск прогона.
        self.assertTrue(data["success"])
        self.assertEqual(data["run_id"], "f" * 32)
        start_mock.assert_called_once()
        self.assertEqual(start_mock.call_args.args[0], [self.task.node_id])
        self.assertEqual(start_mock.call_args.args[1], ["FakeModel"])

    def test_resend_rerun_log_without_run_gives_error(self):
        """Если прогона уже нет в БД — внятная ошибка, а не resend модели."""
        from ai.admin import request_logs as logs_mod
        self.test_run.delete()
        request = self.factory.post(f"/ai/admin/ai/request_logs/{self.rerun_log.id}/resend/")
        request.user = self.superuser
        request.session = {"external_session_id": "DLSID-1"}
        response = logs_mod.resend_request_view(request, self.rerun_log.id)
        data = json.loads(response.content)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(data["error"], "Прогон ARM не найден в БД")


class ArmSolveRerunPairViewTests(TestCase):
    """POST /ai/admin/arm/solve/rerun-pairs/ — эндпоинт кнопки
    «Перезапустить нерешённые пары» (день-2026-10-08: кнопка упиралась
    в 403 CSRF; контракт вью — JSON и понятный message при ошибках)."""

    def setUp(self):
        from ai.models import AIModelTestRun
        self.factory = RequestFactory()
        self.superuser = get_user_model().objects.create_superuser(
            username="rerun_view_admin", password="x", email="rv@t.com",
        )
        self.run = AIModelTestRun.objects.create(
            run_id="e" * 32,
            run_type=AIModelTestRun.RUN_TYPE_BATCH,
            user=self.superuser,
            status=AIModelTestRun.STATUS_COMPLETED,
        )

    def _view(self, request):
        from ai.admin.arm import admin_arm_solve_rerun_pairs_view
        return admin_arm_solve_rerun_pairs_view(request)

    def _post(self, body):
        request = self.factory.post(
            "/ai/admin/arm/solve/rerun-pairs/",
            data=body, content_type="application/json",
        )
        request.user = self.superuser
        request.session = {}
        return self._view(request)

    def test_get_not_allowed(self):
        request = self.factory.get("/ai/admin/arm/solve/rerun-pairs/")
        request.user = self.superuser
        request.session = {}
        response = self._view(request)
        self.assertEqual(response.status_code, 405)

    def test_run_id_required(self):
        response = self._post(json.dumps({}))
        self.assertEqual(response.status_code, 400)
        self.assertIn("run_id", json.loads(response.content)["message"])

    def test_unknown_run_gives_friendly_message(self):
        request = self.factory.post(
            "/ai/admin/arm/solve/rerun-pairs/",
            data=json.dumps({"run_id": "0" * 32}), content_type="application/json",
        )
        request.user = self.superuser
        request.session = {"external_session_id": "DLSID-1"}
        response = self._view(request)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.content)["message"], "Прогон не найден")

    def test_running_run_rejected(self):
        from ai.models import AIModelTestRun
        AIModelTestRun.objects.filter(pk=self.run.pk).update(
            status=AIModelTestRun.STATUS_RUNNING,
        )
        request = self.factory.post(
            "/ai/admin/arm/solve/rerun-pairs/",
            data=json.dumps({"run_id": self.run.run_id}), content_type="application/json",
        )
        request.user = self.superuser
        request.session = {"external_session_id": "DLSID-1"}
        response = self._view(request)
        self.assertEqual(response.status_code, 400)
        self.assertIn("выполняется", json.loads(response.content)["message"])

    def test_success_returns_snapshot_and_message(self):
        with patch("ai.admin.arm.start_batch_rerun_pairs", return_value=(self.run.run_id, "", ["Web_Gone"])), \
             patch("ai.admin.arm.get_arm_run_snapshot", return_value={"run_id": self.run.run_id, "status": "running"}):
            response = self._post(json.dumps({"run_id": self.run.run_id}))
        data = json.loads(response.content)
        self.assertTrue(data["ok"])
        self.assertEqual(data["run_id"], self.run.run_id)
        self.assertIn("Web_Gone", data["message"])
        self.assertEqual(data["run"]["run_id"], self.run.run_id)


class BatchRunnerIntegrationTests(TestCase):
    """End-to-end batch solve with mocked handlers + DL sample fetch.

    Calls ``_run_batch_job_worker`` directly (synchronously, same thread) rather
    than going through the daemon thread in ``start_batch_solve_run``: the real
    thread runs on a separate DB connection which a TestCase's per-test
    transaction would hide. The worker function is the unit that owns the
    handler calls, grading, persistence and report — exercising it directly is a
    faithful, deterministic test of that logic. Одно-модельный прогон после
    распараллеливания выполняется inline в вызывающем потоке (model-loop при
    N==1 не создаёт потоков), поэтому ORM в теле пары остаётся на коннекции
    вызывающего потока и TestCase-транзакция по-прежнему видна.
    """

    def setUp(self):
        from ai.models import Task
        self.user = get_user_model().objects.create_user(username="batcher", password="x")
        self.lang = ProgrammingLanguage.objects.create(language_name="Pascal")
        self.topic = Topic.objects.create(topic_name_ru="Линейные", programming_language=self.lang)
        self.t1 = Task.objects.create(
            node_id=1001, task_id=2001, name="A", statement="Сложите a и b",
            topic=self.topic, programming_language=self.lang, file_extension=".pas",
        )
        self.t2 = Task.objects.create(
            node_id=1002, task_id=2002, name="B", statement="Выведите n",
            topic=self.topic, programming_language=self.lang, file_extension=".pas",
        )

    def test_batch_run_records_solved_verdicts(self):
        import time as _t
        from ai import arm_runner
        from ai.models import AIModelTestResult, AIModelTestRun, Task

        async def fake_handler(messages, conv_id):
            return ("program a; begin writeln(1); end.", 12)

        ordered_models = [{"key": "FakeModel", "title": "FakeModel", "handler": fake_handler}]
        node_ids = [self.t1.node_id, self.t2.node_id]
        run_id = "test-batch-run-1"

        # Pre-seed the in-memory job so the worker can record live progress and
        # build the final report from it (mirrors start_batch_solve_run).
        now_ts = _t.time()
        arm_runner._jobs[run_id] = {
            "run_id": run_id, "run_type": "batch", "status": "running",
            "error_message": "", "total_models": 1,
            "total_pairs": 2, "completed_pairs": 0, "completed_models": 0,
            "current_model_key": "FakeModel", "current_model_title": "FakeModel",
            "current_task_node_id": "", "current_task_name": "",
            "results": [], "report": None,
            "created_at_ts": now_ts, "updated_at_ts": now_ts,
        }
        # Вердикт теперь строго по DL: мокаем _test_solution_on_dl успехом.
        dl_ok = lambda sid, node_id, code, ext, **kw: {
            "verdict": "solved", "comment": "Все тесты успешно пройдены",
            "submit_error": "", "queue_id": 1, "code_sent": code,
        }
        try:
            with patch("ai.arm_runner._test_solution_on_dl", dl_ok):
                arm_runner._run_batch_job_worker(
                    run_id, node_ids, ordered_models, self.user.id, "DLSID-1",
                    ui_language="Русский", dl_test=True,
                )
        finally:
            arm_runner._jobs.pop(run_id, None)

        run = AIModelTestRun.objects.get(run_id=run_id)
        self.assertEqual(run.status, AIModelTestRun.STATUS_COMPLETED)
        self.assertEqual(run.run_type, AIModelTestRun.RUN_TYPE_BATCH)

        results = list(AIModelTestResult.objects.filter(run=run))
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r.verdict == "solved" for r in results))
        self.assertTrue(all(r.task_id in (self.t1.id, self.t2.id) for r in results))
        # Код и DL-коммент сохраняются в новых полях.
        self.assertTrue(all(r.code == "program a; begin writeln(1); end." for r in results))
        self.assertTrue(all(r.dl_comment == "Все тесты успешно пройдены" for r in results))
        self.assertTrue(all(r.dl_queue_id == 1 for r in results))
        self.assertTrue(all(r.file_extension_snapshot == ".pas" for r in results))

        # DB-fallback snapshot rebuilds the batch report from persisted rows.
        snapshot = arm_runner.get_arm_run_snapshot(run_id)
        self.assertEqual(snapshot["run_type"], "batch")
        self.assertEqual(snapshot["report"]["per_model"][0]["solved"], 2)
        self.assertEqual(snapshot["report"]["per_model"][0]["total"], 2)

        # Все пары решены → batch-лог в журнале получает Success (статус строго
        # по полному решению всех пар задача×модель).
        log = AIRequestLog.objects.get(message=f"Batch solve run {run_id}")
        self.assertEqual(log.status, AIRequestLog.STATUS_SUCCESS)
        self.assertEqual(log.error_message, "")


class BatchParallelWorkerTests(TransactionTestCase):
    """Параллельное решение batch-solve: каждая модель — независимый цикл.

    Model-потоки работают с БД на собственных соединениях, TestData TestCase
    транзакции им не видна — поэтому TransactionTestCase (данные реально
    закоммичены). Воркер вызывается синхронно в тестовом потоке (как в
    BatchRunnerIntegrationTests), но сам он поднимает model-потоки (при
    единственной модели её цикл выполняется inline). DL-фаза сериализована
    run-level локом: перекрывается генерация кода, а send-solution + полл
    идут по очереди (одна DLSID-сессия на прогон).
    """

    def setUp(self):
        from ai.models import Task
        self.user = get_user_model().objects.create_user(username="parallel", password="x")
        self.lang = ProgrammingLanguage.objects.create(language_name="Pascal")
        self.topic = Topic.objects.create(topic_name_ru="Параллельные", programming_language=self.lang)
        self.t1 = Task.objects.create(
            node_id=9101, task_id=9201, name="A", statement="Сложите a и b",
            topic=self.topic, programming_language=self.lang, file_extension=".pas",
        )
        self.t2 = Task.objects.create(
            node_id=9102, task_id=9202, name="B", statement="Выведите n",
            topic=self.topic, programming_language=self.lang, file_extension=".pas",
        )

    def _seed_job(self, run_id, total_pairs, rerun=False):
        import time as _t
        from ai import arm_runner

        now_ts = _t.time()
        job = {
            "run_id": run_id, "run_type": "batch", "status": "running",
            "error_message": "", "total_models": 2, "total_pairs": total_pairs,
            "completed_pairs": 0, "completed_models": 0,
            "current_model_key": "", "current_model_title": "",
            "current_task_node_id": "", "current_task_name": "",
            "model_progress": [], "results": [], "report": None,
            "created_at_ts": now_ts, "updated_at_ts": now_ts,
        }
        if rerun:
            job["rerun"] = True
        arm_runner._jobs[run_id] = job
        return job

    @staticmethod
    def _dl_ok():
        def dl_mock(sid, node_id, code, ext, **kw):
            return {
                "verdict": "solved", "comment": "Все тесты успешно пройдены",
                "submit_error": "", "queue_id": 21, "code_sent": code,
            }
        return dl_mock

    @staticmethod
    def _make_handler(key, calls):
        """Хендлер, записывающий (модель, нода, поток, интервал) вызова."""
        import asyncio
        import threading

        async def handler(messages, conv_id):
            started = time.monotonic()
            node_id = int(conv_id.rsplit("-", 1)[1])
            await asyncio.sleep(0.3)
            calls.append({
                "model_key": key, "node_id": node_id,
                "thread": threading.get_ident(),
                "start": started, "end": time.monotonic(),
            })
            return ("program p; begin writeln(1); end.", 7)

        return handler

    def _calls_for(self, calls, key):
        return [c for c in calls if c["model_key"] == key]

    def test_truncated_pair_auto_retry_solves(self):
        """Обрез по потолку генерации (is_output_truncated): без кода — один
        авто-ретрай с припиской «сразу код»; ретрай принёс код — пара решена,
        флаг снят (сохранённый ответ цел)."""
        import os
        from ai import arm_runner
        from ai.models import AIModelTestResult, AIModelTestRun

        calls = []

        async def cut_then_code_handler(messages, conv_id):
            calls.append(messages)
            if len(calls) == 1:
                # Обрезанный ответ: content пуст (рассуждения сожрали бюджет;
                # до кода модель не дошла), tokens упёрся в потолок —
                # обрез по факту.
                return ("", 8192, False)
            return ("program p; begin writeln(1); end.", 128, False)

        models = [{
            "key": "Ollama_Test_Cut", "title": "Ollama Test Cut",
            "handler": cut_then_code_handler,
        }]
        run_id = "cut-" + uuid.uuid4().hex[:8]
        job = self._seed_job(run_id, total_pairs=1)
        try:
            with patch.dict(os.environ, {"OLLAMA_NUM_PREDICT": "8192"}):
                with patch("ai.arm_runner._test_solution_on_dl", self._dl_ok()):
                    arm_runner._run_batch_job_worker(
                        run_id, [self.t1.node_id], models, self.user.id, "DLSID-1",
                        ui_language="Русский", dl_test=True,
                    )
        finally:
            arm_runner._jobs.pop(run_id, None)

        # Ровно один авто-ретрай: второй вызов с припиской про лимит длины.
        self.assertEqual(len(calls), 2)
        self.assertIn("лимитом длины", calls[1])
        run = AIModelTestRun.objects.get(run_id=run_id)
        row = AIModelTestResult.objects.get(run=run, model_key="Ollama_Test_Cut")
        self.assertEqual(row.verdict, "solved")
        self.assertFalse(row.truncated)      # ретрай дал полный ответ
        self.assertEqual(row.tokens, 128)
        self.assertEqual(run.report["truncated_pairs"], 0)

    def test_cut_flag_from_handler_fourth_element(self):
        """Обрез без потолка по токенам: провайдер сам сказал «длина» 4-м
        элементом (done_reason != "stop" в Ollama — скрытый потолок шлюза на
        ~2000 токенах, лог пары 16246/task 495). Флаг ставится, ретрай
        разогорается, честная причина в dl_comment."""
        import os
        from ai import arm_runner
        from ai.models import AIModelTestResult, AIModelTestRun

        calls = []

        async def cut_flag_handler(messages, conv_id):
            calls.append(messages)
            if len(calls) == 1:
                return ("We need to think slowly and carefully about all the letters before writing anything.", 1998, False, True)
            return ("program p; begin writeln(1); end.", 128, False, False)

        models = [{
            "key": "Web_Test_Cut", "title": "Web Test Cut",
            "handler": cut_flag_handler,   # ключ без префикса — потолка нет, работает только 4-й элемент
        }]
        run_id = "cut4-" + uuid.uuid4().hex[:8]
        job = self._seed_job(run_id, total_pairs=1)
        try:
            with patch.dict(os.environ, {"OLLAMA_NUM_PREDICT": "16384"}):
                with patch("ai.arm_runner._test_solution_on_dl", self._dl_ok()):
                    arm_runner._run_batch_job_worker(
                        run_id, [self.t1.node_id], models, self.user.id, "DLSID-1",
                        ui_language="Русский", dl_test=True,
                    )
        finally:
            arm_runner._jobs.pop(run_id, None)

        self.assertEqual(len(calls), 2)
        self.assertIn("лимитом длины", calls[1])
        run = AIModelTestRun.objects.get(run_id=run_id)
        row = AIModelTestResult.objects.get(run=run, model_key="Web_Test_Cut")
        self.assertEqual(row.verdict, "solved")
        self.assertFalse(row.truncated)      # ретрай дал полный ответ
        run.refresh_from_db()
        self.assertEqual(run.report["truncated_pairs"], 0)

    def test_cut_flag_still_cut_reports_reason(self):
        """И ретрай по 4-му флагу обрезан: пара failed с truncated=True и
        честной подписью «ответ оборван» вместо «не удалось извлечь»."""
        import os
        from ai import arm_runner
        from ai.models import AIModelTestResult, AIModelTestRun

        calls = []

        async def always_flag_cut(messages, conv_id):
            calls.append(messages)
            return ("We need to keep reasoning silently while the model keeps adding more words without any conclusion yet.", 1800, False, True)

        models = [{
            "key": "Web_Test_Cut2", "title": "Web Test Cut2",
            "handler": always_flag_cut,
        }]
        run_id = "cut5-" + uuid.uuid4().hex[:8]
        job = self._seed_job(run_id, total_pairs=1)
        try:
            with patch.dict(os.environ, {"OLLAMA_NUM_PREDICT": "16384"}):
                with patch("ai.arm_runner._test_solution_on_dl", self._dl_ok()):
                    arm_runner._run_batch_job_worker(
                        run_id, [self.t1.node_id], models, self.user.id, "DLSID-1",
                        ui_language="Русский", dl_test=True,
                    )
        finally:
            arm_runner._jobs.pop(run_id, None)

        self.assertEqual(len(calls), 2)
        run = AIModelTestRun.objects.get(run_id=run_id)
        row = AIModelTestResult.objects.get(run=run, model_key="Web_Test_Cut2")
        self.assertEqual(row.verdict, "failed")
        self.assertTrue(row.truncated)
        self.assertIn("ответ оборван", row.dl_comment)

    def test_truncated_still_cut_keeps_flag_and_failed(self):
        """Ретрай обрезан тоже: пара failed с truncated=True, сохранены токены
        ретрая, в отчёте счётчик truncated_pairs."""
        import os
        from ai import arm_runner
        from ai.models import AIModelTestResult, AIModelTestRun

        calls = []

        async def always_cut_handler(messages, conv_id):
            calls.append(messages)
            return ("", 8192, False)

        models = [{
            "key": "Ollama_Test_Cut2", "title": "Ollama Test Cut2",
            "handler": always_cut_handler,
        }]
        run_id = "cut2-" + uuid.uuid4().hex[:8]
        job = self._seed_job(run_id, total_pairs=1)
        try:
            with patch.dict(os.environ, {"OLLAMA_NUM_PREDICT": "8192"}):
                with patch("ai.arm_runner._test_solution_on_dl", self._dl_ok()):
                    arm_runner._run_batch_job_worker(
                        run_id, [self.t1.node_id], models, self.user.id, "DLSID-1",
                        ui_language="Русский", dl_test=True,
                    )
        finally:
            arm_runner._jobs.pop(run_id, None)

        self.assertEqual(len(calls), 2)
        run = AIModelTestRun.objects.get(run_id=run_id)
        row = AIModelTestResult.objects.get(run=run, model_key="Ollama_Test_Cut2")
        self.assertEqual(row.verdict, "failed")
        self.assertTrue(row.truncated)
        self.assertEqual(row.tokens, 8192)
        # Обрезанная пара получает честную причину вместо общего «не удалось извлечь».
        self.assertIn("Модель не успела вывести код — ответ оборван", row.dl_comment)
        self.assertEqual(run.report["truncated_pairs"], 1)

    def test_no_code_retry_resends_same_task_to_same_model(self):
        """Код не извлёкся ПО ПРИЧИНЕ, НЕ связанной с обрезом (протекшее
        рассуждение залило ответ): пара не срывается сразу — один повтор
        ТОЙ ЖЕ задаче к ТОЙ ЖЕ модели тем же текстом (без приписки про
        лимит — его причина другая). Ретрай дал код — пара решена."""
        import os
        from ai import arm_runner
        from ai.models import AIModelTestResult, AIModelTestRun

        calls = []

        async def prose_then_code_handler(messages, conv_id):
            calls.append(messages)
            if len(calls) == 1:
                # Проза без маркеров кода, токены далеки от потолка:
                # «код не извлечён» БЕЗ обреза.
                return (
                    "Сначала я долго размышляю над условием задачи от самого начала "
                    "и перечитываю его снова и снова, не переходя к программе.",
                    50, False,
                )
            return ("program p; begin writeln(1); end.", 60, False)

        models = [{
            "key": "Prose_Test", "title": "Prose Test",
            "handler": prose_then_code_handler,  # фейковый модуль → nothink-пути нет
        }]
        run_id = "nc-" + uuid.uuid4().hex[:8]
        job = self._seed_job(run_id, total_pairs=1)
        try:
            with patch.dict(os.environ, {"OLLAMA_NUM_PREDICT": "16384"}):
                with patch("ai.arm_runner._test_solution_on_dl", self._dl_ok()):
                    arm_runner._run_batch_job_worker(
                        run_id, [self.t1.node_id], models, self.user.id, "DLSID-1",
                        ui_language="Русский", dl_test=True,
                    )
        finally:
            arm_runner._jobs.pop(run_id, None)

        # Один повтор: та же задача, тот же текст — без «лимитом длины».
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1], calls[0])
        run = AIModelTestRun.objects.get(run_id=run_id)
        row = AIModelTestResult.objects.get(run=run, model_key="Prose_Test")
        self.assertEqual(row.verdict, "solved")
        self.assertFalse(row.truncated)
        run.refresh_from_db()
        self.assertEqual(run.report["truncated_pairs"], 0)

    def test_no_code_retry_failed_keeps_honest_comment(self):
        """И повтор не помог (проза снова): пара failed с честной «Не удалось
        извлечь код» (НЕ «обрезан» — обрезы тут ни при чём, флаг не врёт)."""
        import os
        from ai import arm_runner
        from ai.models import AIModelTestResult, AIModelTestRun

        calls = []

        async def always_prose(messages, conv_id):
            calls.append(messages)
            return (
                "Я снова подробно рассказываю про свои сомнения вместо программы "
                "и обсуждения деталей условия, никаких блоков кода тут не будет.",
                55, False,
            )

        models = [{"key": "Prose_Test2", "title": "Prose Test2", "handler": always_prose}]
        run_id = "nc2-" + uuid.uuid4().hex[:8]
        job = self._seed_job(run_id, total_pairs=1)
        try:
            with patch.dict(os.environ, {"OLLAMA_NUM_PREDICT": "16384"}):
                with patch("ai.arm_runner._test_solution_on_dl", self._dl_ok()):
                    arm_runner._run_batch_job_worker(
                        run_id, [self.t1.node_id], models, self.user.id, "DLSID-1",
                        ui_language="Русский", dl_test=True,
                    )
        finally:
            arm_runner._jobs.pop(run_id, None)

        self.assertEqual(len(calls), 2)
        run = AIModelTestRun.objects.get(run_id=run_id)
        row = AIModelTestResult.objects.get(run=run, model_key="Prose_Test2")
        self.assertEqual(row.verdict, "failed")
        self.assertFalse(row.truncated)
        self.assertIn("Не удалось извлечь код", row.dl_comment)

    def test_no_code_retry_prefers_nothink_handler(self):
        """Ollama-модель: повтор идёт через no-think хендлер (get_no_think_handler;
        Ollama API think=False по docs.ollama.com/capabilities/thinking), а не
        через обычный. Провайдер определяется по __module__ — фейк держит
        оllama-модуль и имя из реестра."""
        import os
        from ai import arm_runner
        from ai.model_clients import get_no_think_handler
        from ai.models import AIModelTestResult, AIModelTestRun

        calls = []

        async def real_handler(messages, conv_id):
            calls.append(("plain", messages))
            return ("мусор без кода", 50, False)

        real_handler.__module__ = "ai.model_clients.ollama"
        real_handler.__name__ = "ask_Ollama_NotInRegistry_async"  # нет в _NOTHINK_HANDLERS

        async def nothink_proxy(messages, conv_id):
            calls.append(("nothink", messages))
            return ("program p; begin writeln(1); end.", 60, False)

        models = [{"key": "Ollama_NotInRegistry", "title": "Ollama NotInRegistry",
                   "handler": real_handler}]
        run_id = "nc3-" + uuid.uuid4().hex[:8]
        job = self._seed_job(run_id, total_pairs=1)
        try:
            with patch.dict(os.environ, {"OLLAMA_NUM_PREDICT": "16384"}):
                with patch.object(arm_runner, "get_no_think_handler",
                                  side_effect=lambda h: get_no_think_handler(h) or nothink_proxy):
                    with patch("ai.arm_runner._test_solution_on_dl", self._dl_ok()):
                        arm_runner._run_batch_job_worker(
                            run_id, [self.t1.node_id], models, self.user.id, "DLSID-1",
                            ui_language="Русский", dl_test=True,
                        )
        finally:
            arm_runner._jobs.pop(run_id, None)

        # Первый вызов — реальный хендлер (мусор), второй — no-think прокси.
        self.assertEqual([kind for kind, _ in calls], ["plain", "nothink"])
        run = AIModelTestRun.objects.get(run_id=run_id)
        row = AIModelTestResult.objects.get(run=run, model_key="Ollama_NotInRegistry")
        self.assertEqual(row.verdict, "solved")

    def test_parallel_models_solve_independently(self):
        """2 модели × 2 задачи: каждая пара решена ровно один раз, циклы моделей
        перекрываются по времени (модель не ждёт другую), прогресс per-model полный."""
        import threading
        import time as _t
        from ai import arm_runner
        from ai.models import AIModelTestResult, AIModelTestRun

        calls = []
        models = [
            {"key": "ModelA", "title": "Model A", "handler": self._make_handler("ModelA", calls)},
            {"key": "ModelB", "title": "Model B", "handler": self._make_handler("ModelB", calls)},
        ]
        node_ids = [self.t1.node_id, self.t2.node_id]
        run_id = "par-" + uuid.uuid4().hex[:8]
        job = self._seed_job(run_id, total_pairs=4)
        # Циклы моделей выполняются в РАЗНЫХ потоках. Идентичность потока
        # снимаем spy-обёрткой вокруг _process_batch_pair: внутри самого
        # хендлера get_ident() не равен потоку цикла — корутину исполняет
        # event-loop asgiref'а, а не поток model-loop'а.
        pair_threads = {}
        orig_pair = arm_runner._process_batch_pair

        def spy_pair(ctx, task_ctx, model, dl_lock):
            pair_threads.setdefault(
                model["key"], set()
            ).add((threading.get_ident(), task_ctx["node_id"]))
            return orig_pair(ctx, task_ctx, model, dl_lock)

        try:
            with patch.object(arm_runner, "_process_batch_pair", spy_pair):
                with patch("ai.arm_runner._test_solution_on_dl", self._dl_ok()):
                    arm_runner._run_batch_job_worker(
                        run_id, node_ids, models, self.user.id, "DLSID-1",
                        ui_language="Русский", dl_test=True,
                    )
        finally:
            arm_runner._jobs.pop(run_id, None)

        run = AIModelTestRun.objects.get(run_id=run_id)
        self.assertEqual(run.status, AIModelTestRun.STATUS_COMPLETED)
        # Каждая пара (модель × задача) записана ровно один раз.
        rows = list(AIModelTestResult.objects.filter(run=run))
        self.assertEqual(len(rows), 4)
        self.assertEqual(
            {(r.model_key, r.task.node_id) for r in rows},
            {(m["key"], t.node_id) for m in models for t in (self.t1, self.t2)},
        )
        a, b = self._calls_for(calls, "ModelA"), self._calls_for(calls, "ModelB")
        self.assertEqual(len(a), 2)
        self.assertEqual(len(b), 2)
        # Обе пары модели — в одном потоке; потоки моделей различаются.
        self.assertEqual(len({tid for tid, _ in pair_threads["ModelA"]}), 1)
        self.assertEqual(len({tid for tid, _ in pair_threads["ModelB"]}), 1)
        self.assertNotEqual(
            next(iter(pair_threads["ModelA"])), next(iter(pair_threads["ModelB"]))
        )
        overlap = any(
            max(c1["start"], c2["start"]) < min(c1["end"], c2["end"])
            for c1 in a for c2 in b
        )
        self.assertTrue(overlap, "model loops should run concurrently")
        # Вердикты по DL-моку: все решены; per-model прогресс полный.
        self.assertTrue(all(r.verdict == "solved" for r in rows))
        self.assertEqual(job["completed_pairs"], 4)
        self.assertEqual(
            sorted((e["model_key"], e["done"], e["total"], e["status"])
                   for e in job["model_progress"]),
            [("ModelA", 2, 2, "done"), ("ModelB", 2, 2, "done")],
        )

    def test_one_model_failure_does_not_stop_others(self):
        """Хендлер одной модели всегда падает: её пары — failed с дружелюбной
        ошибкой, вторая модель решает свои пары, прогон всё равно completed."""
        from ai import arm_runner
        from ai.models import AIModelTestResult, AIModelTestRun

        async def broken_handler(messages, conv_id):
            raise RuntimeError("handler exploded")

        async def ok_handler(messages, conv_id):
            return ("program p; begin writeln(1); end.", 7)

        models = [
            {"key": "ModelA", "title": "Model A", "handler": broken_handler},
            {"key": "ModelB", "title": "Model B", "handler": ok_handler},
        ]
        run_id = "parfail-" + uuid.uuid4().hex[:8]
        self._seed_job(run_id, total_pairs=4)
        try:
            with patch("ai.arm_runner._test_solution_on_dl", self._dl_ok()):
                arm_runner._run_batch_job_worker(
                    run_id, [self.t1.node_id, self.t2.node_id], models,
                    self.user.id, "DLSID-1", ui_language="Русский", dl_test=True,
                )
        finally:
            arm_runner._jobs.pop(run_id, None)

        run = AIModelTestRun.objects.get(run_id=run_id)
        self.assertEqual(run.status, AIModelTestRun.STATUS_COMPLETED)
        rows = list(AIModelTestResult.objects.filter(run=run))
        self.assertEqual(len(rows), 4)
        a_rows = [r for r in rows if r.model_key == "ModelA"]
        b_rows = [r for r in rows if r.model_key == "ModelB"]
        # Модель A: каждая пара честно записана как ошибка модели.
        self.assertEqual(len(a_rows), 2)
        self.assertTrue(all(r.verdict == "failed" for r in a_rows))
        self.assertTrue(all("handler exploded" in (r.dl_error or "") for r in a_rows))
        # Модель B: обе пары решены — падение соседа её не задело.
        self.assertTrue(all(r.verdict == "solved" for r in b_rows))

    def test_model_loop_crash_marks_progress_error(self):
        """Падение цикла модели вне тела пары (сбой БД-записи): статус модели
        «error» в model_progress, вторая модель дорешивает, прогон completed,
        в ошибке журнала — примечание о прерванной модели."""
        from ai import arm_runner
        from ai.models import AIModelTestResult, AIModelTestRun

        async def ok_handler(messages, conv_id):
            return ("program p; begin writeln(1); end.", 7)

        real_update = AIModelTestResult.objects.update_or_create

        def flaky_update_or_create(**kwargs):
            if kwargs.get("model_key") == "ModelA":
                raise RuntimeError("db write exploded")
            return real_update(**kwargs)

        models = [
            {"key": "ModelA", "title": "Model A", "handler": ok_handler},
            {"key": "ModelB", "title": "Model B", "handler": ok_handler},
        ]
        run_id = "parcrash-" + uuid.uuid4().hex[:8]
        job = self._seed_job(run_id, total_pairs=4)
        try:
            with patch.object(AIModelTestResult.objects, "update_or_create", flaky_update_or_create):
                with patch("ai.arm_runner._test_solution_on_dl", self._dl_ok()):
                    arm_runner._run_batch_job_worker(
                        run_id, [self.t1.node_id, self.t2.node_id], models,
                        self.user.id, "DLSID-1", ui_language="Русский", dl_test=True,
                    )
        finally:
            arm_runner._jobs.pop(run_id, None)

        run = AIModelTestRun.objects.get(run_id=run_id)
        # Падение одного model-цикла НЕ валит прогон в FAILED.
        self.assertEqual(run.status, AIModelTestRun.STATUS_COMPLETED)
        # ModelB решила все свои пары; ModelA — ни одной (каждая запись падала).
        rows = [r.model_key for r in AIModelTestResult.objects.filter(run=run)]
        self.assertEqual(rows, ["ModelB", "ModelB"])
        progress = {e["model_key"]: e for e in job["model_progress"]}
        self.assertEqual(progress["ModelA"]["status"], "error")
        self.assertEqual(progress["ModelB"]["status"], "done")
        # Примечание о прерванной модели — в журнале; статус лога Error.
        log = AIRequestLog.objects.get(message=f"Batch solve run {run_id}")
        self.assertEqual(log.status, AIRequestLog.STATUS_ERROR)
        self.assertIn("Модели, прерванные ошибкой цикла: «Model A»", log.error_message)

    def test_cancel_mid_run_stops_models_and_keeps_partial(self):
        """Отмена на середине прогона: обе модели обрывают текущую пару, БД —
        STATUS_CANCELLED, записаны только завершённые пары."""
        import time as _t
        import threading
        from ai import arm_runner
        from ai.models import AIModelTestRun

        gate = threading.Event()

        def make_blocked_handler(key):
            async def handler(messages, conv_id):
                node_id = int(conv_id.rsplit("-", 1)[1])
                if node_id == self.t2.node_id:
                    # Вторая пара блокируется до отмены.
                    gate.wait(timeout=15)
                return ("program p; begin writeln(1); end.", 7)
            return handler

        models = [
            {"key": "ModelA", "title": "Model A", "handler": make_blocked_handler("ModelA")},
            {"key": "ModelB", "title": "Model B", "handler": make_blocked_handler("ModelB")},
        ]
        run_id = "parcancel-" + uuid.uuid4().hex[:8]
        job = self._seed_job(run_id, total_pairs=4)
        try:
            with patch("ai.arm_runner._test_solution_on_dl", self._dl_ok()):
                worker = threading.Thread(
                    target=lambda: arm_runner._run_batch_job_worker(
                        run_id, [self.t1.node_id, self.t2.node_id], models,
                        self.user.id, "DLSID-1", ui_language="Русский", dl_test=True,
                    ),
                    daemon=True,
                )
                worker.start()
                # Ждём, пока обе модели решат первую пару (по одной на модель).
                deadline = _t.time() + 15
                while _t.time() < deadline and job["completed_pairs"] < 2:
                    _t.sleep(0.05)
                self.assertEqual(job["completed_pairs"], 2)
                self.assertEqual(
                    {(e["done"], e["total"]) for e in job["model_progress"]},
                    {(1, 2)},
                )
                arm_runner.cancel_arm_run(run_id)
                gate.set()
                worker.join(timeout=30)
        finally:
            arm_runner._jobs.pop(run_id, None)

        run = AIModelTestRun.objects.get(run_id=run_id)
        self.assertEqual(run.status, AIModelTestRun.STATUS_CANCELLED)
        # Мгновенный flip cancel_arm_run уже выставил job-статус; финализация
        # его не перетёрла и частичные результаты сохранились.
        self.assertEqual(job["status"], "cancelled")
        self.assertEqual(len(job["results"]), 2)
        # Блокированные вторые пары не записаны: каждая модель по одной паре.
        self.assertTrue(
            all(e["done"] == 1 and e["total"] == 2 for e in job["model_progress"]),
        )

    def test_rerun_parallel_merges_into_same_run_and_skips_other_models(self):
        """Попарный перезапуск на двух моделях: каждая модель перезапускает
        ТОЛЬКО свои нерешённые пары, строки вливаются в тот же прогон без
        дублирования, отчёт — из всех строк, started_at сохранён."""
        from ai import arm_runner
        from ai.models import AIModelTestResult, AIModelTestRun

        started_at = timezone.now()
        run_id = "parrerun-" + uuid.uuid4().hex[:8]
        test_run = AIModelTestRun.objects.create(
            run_id=run_id, run_type=AIModelTestRun.RUN_TYPE_BATCH,
            user=self.user, status=AIModelTestRun.STATUS_COMPLETED,
            started_at=started_at, finished_at=timezone.now(),
            total_models=2,
            message=f"Batch solve: 2 задач × 2 моделей",
        )
        async def solved_handler(messages, conv_id):
            return ("program p; begin writeln(1); end.", 7)

        models = [
            {"key": "ModelA", "title": "Model A", "handler": solved_handler},
            {"key": "ModelB", "title": "Model B", "handler": solved_handler},
        ]
        # 4 строки: перезапускать нечего... перезапускаем ровно 2 нерешённые
        # пары (t1×ModelA, t2×ModelB); строки соседних моделей не трогаем.
        for model in models:
            for task in (self.t1, self.t2):
                solved = (task is self.t2 and model["key"] == "ModelA") or \
                         (task is self.t1 and model["key"] == "ModelB")
                AIModelTestResult.objects.create(
                    run=test_run, model_key=model["key"], task=task,
                    model_title=model["title"],
                    status="ok" if solved else "error",
                    verdict="solved" if solved else "failed",
                    duration_seconds=1.0,
                )
        rerun_pairs = {self.t1.node_id: {"ModelA"}, self.t2.node_id: {"ModelB"}}
        job = self._seed_job(run_id, total_pairs=2, rerun=True)
        try:
            with patch("ai.arm_runner._test_solution_on_dl", self._dl_ok()):
                arm_runner._run_batch_job_worker(
                    run_id, [self.t1.node_id, self.t2.node_id], models,
                    self.user.id, "DLSID-1", ui_language="Русский", dl_test=True,
                    rerun=True, rerun_pairs=rerun_pairs,
                )
        finally:
            arm_runner._jobs.pop(run_id, None)

        run = AIModelTestRun.objects.get(run_id=run_id)
        rows = list(AIModelTestResult.objects.filter(run=run).order_by("model_key", "task_id"))
        # Строки НЕ дублируются (update_or_create в тот же run).
        self.assertEqual(len(rows), 4)
        by_pair = {(r.model_key, r.task_id): r for r in rows}
        # Перезапущенные пары решены; соседние строки (чужие модели) не тронуты
        # ни вердиктом, ни DL-комментарием (переперезапуск не задел «не мою» пару).
        self.assertEqual(by_pair[("ModelA", self.t1.id)].verdict, "solved")
        self.assertEqual(by_pair[("ModelB", self.t2.id)].verdict, "solved")
        for untouched in (("ModelA", self.t2.id), ("ModelB", self.t1.id)):
            row = by_pair[untouched]
            self.assertEqual(row.verdict, "solved")          # решена была — не перезапускали
            self.assertEqual(row.dl_comment, "")             # DL-мок перезапуска её не касался
            self.assertEqual(row.duration_seconds, 1.0)      # исходные значения сохранены
        # Отчёт финализирован из ВСЕХ строк прогона: 4 решено / 0 провалено.
        self.assertEqual(run.report["total_pairs"], 4)
        self.assertEqual(run.report["solved"], 4)
        self.assertEqual(run.status, AIModelTestRun.STATUS_COMPLETED)
        # Отсчёт «Прогон» — от исходного started_at.
        self.assertEqual(run.started_at, started_at)
        self.assertTrue(
            AIRequestLog.objects.filter(message=f"Batch solve rerun {run_id}").exists(),
        )


class ArmModelProgressSnapshotTests(TestCase):
    """job["model_progress"] доезжает до снапшотов: light-пасsthrough живого
    job и _snapshot_from_test_run (БД-путь с живым job)."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(username="snapshots", password="x")
        self.lang = ProgrammingLanguage.objects.create(language_name="Pascal")
        self.topic = Topic.objects.create(topic_name_ru="Линейные", programming_language=self.lang)
        self.task = Task.objects.create(
            node_id=9501, task_id=9601, name="A", statement="do",
            topic=self.topic, programming_language=self.lang, file_extension=".pas",
        )

    @staticmethod
    def _progress():
        return [
            {"model_key": "ModelA", "model_title": "Model A", "done": 1, "total": 2,
             "current_task_node_id": 9501, "current_task_name": "A", "status": "running"},
            {"model_key": "ModelB", "model_title": "Model B", "done": 2, "total": 2,
             "current_task_node_id": "", "current_task_name": "", "status": "done"},
        ]

    def _seed(self, run_id, rerun=False):
        import time as _t
        from ai import arm_runner

        now_ts = _t.time()
        job = {
            "run_id": run_id, "run_type": "batch", "status": "running",
            "error_message": "", "total_models": 2, "total_pairs": 4,
            "completed_pairs": 3, "completed_models": 1,
            "current_model_key": "ModelA", "current_model_title": "Model A",
            "current_task_node_id": 9501, "current_task_name": "A",
            "model_progress": self._progress(), "results": [], "report": None,
            "created_at_ts": now_ts, "updated_at_ts": now_ts,
        }
        if rerun:
            job["rerun"] = True
        arm_runner._jobs[run_id] = job
        return job

    def test_light_snapshot_carries_model_progress(self):
        from ai import arm_runner

        run_id = "mp-snap-" + uuid.uuid4().hex[:8]
        job = self._seed(run_id)
        try:
            snapshot = arm_runner.get_arm_run_snapshot(run_id, light_results=True)
        finally:
            arm_runner._jobs.pop(run_id, None)
        self.assertEqual(snapshot["model_progress"], self._progress())
        self.assertEqual(snapshot["current_task_name"], "A")
        self.assertEqual(snapshot["current_model_title"], "Model A")

    def test_db_snapshot_reads_model_progress_from_live_job(self):
        from ai import arm_runner
        from ai.models import AIModelTestRun

        run_id = "mp-db-" + uuid.uuid4().hex[:8]
        test_run = AIModelTestRun.objects.create(
            run_id=run_id, run_type=AIModelTestRun.RUN_TYPE_BATCH,
            user=self.user, status=AIModelTestRun.STATUS_RUNNING,
            started_at=timezone.now(), total_models=2,
        )
        job = self._seed(run_id)
        try:
            snapshot = arm_runner._snapshot_from_test_run(test_run, light_results=True)
        finally:
            arm_runner._jobs.pop(run_id, None)
        self.assertEqual(len(snapshot["results"]), 0)
        self.assertEqual(snapshot["model_progress"], self._progress())
        self.assertEqual(snapshot["current_model_key"], "ModelA")


class TestSolutionOnDlTests(SimpleTestCase):
    """``_test_solution_on_dl``: на любой отказ send-solution (400/500/…)
    пробует taskId вместо nodeId (гипотеза пользователя: «с твоим nodeId —
    доступ запрещён») и при успехе падает в поллинг. Старая подсказка
    «язык выбран неверно» и хардкод «(400)» в финальном тексте удалены."""

    def test_400_falls_back_to_task_id_and_polls(self):
        from ai import arm_runner
        from ai.dl_api_client import DLServerError

        calls = []

        def fake_send(session_id, node_id, code, file_extension, course_id=0, **kw):
            calls.append(node_id)
            if node_id == 2606747:
                raise DLServerError(
                    "send-solution(nodeId=2606747, fileExtension='.mpc', "
                    "codeLen=111, codeHead='...'): DL API вернул ошибку "
                    "(код 400): Bad Request"
                )
            # taskId принят
            return {"queueId": 42, "message": "ok"}

        def fake_poll(session_id, queue_id):
            return {"isFinished": True, "comment": "Все тесты успешно пройдены"}

        with patch("ai.dl_api_client.send_solution_to_dl", fake_send), \
                patch("ai.dl_api_client.get_solution_result_from_dl", fake_poll):
            res = arm_runner._test_solution_on_dl(
                "SID", 2606747, "code...", ".mpc",
                max_polls=1, poll_interval=0, task_id=2001,
            )
        # Сначала nodeId, затем taskId.
        self.assertEqual(calls, [2606747, 2001])
        self.assertEqual(res["queue_id"], 42)
        self.assertEqual(res["submit_error"], "")
        self.assertEqual(res["verdict"], "solved")
        self.assertEqual(res["comment"], "Все тесты успешно пройдены")

    def test_500_falls_back_to_task_id_and_polls(self):
        from ai import arm_runner
        from ai.dl_api_client import DLServerError

        calls = []

        def fake_send(session_id, node_id, code, file_extension, course_id=0, **kw):
            calls.append(node_id)
            if node_id == 2606747:
                raise DLServerError(
                    "send-solution(nodeId=2606747, fileExtension='.i86', "
                    "codeLen=111, codeHead='...'): Ошибка сервера DL (код 500)"
                )
            # taskId принят
            return {"queueId": 7, "message": "ok"}

        def fake_poll(session_id, queue_id):
            return {"isFinished": True, "comment": "Задача решена неправильно"}

        with patch("ai.dl_api_client.send_solution_to_dl", fake_send), \
                patch("ai.dl_api_client.get_solution_result_from_dl", fake_poll):
            res = arm_runner._test_solution_on_dl(
                "SID", 2606747, "code...", ".i86",
                max_polls=1, poll_interval=0, task_id=2001,
            )
        # 500 — тоже повод попробовать taskId (раньше fallback был только на 400).
        self.assertEqual(calls, [2606747, 2001])
        self.assertEqual(res["queue_id"], 7)
        self.assertEqual(res["submit_error"], "")
        self.assertEqual(res["verdict"], "failed")

    def test_400_both_ids_rejected_reports_honest_error(self):
        from ai import arm_runner
        from ai.dl_api_client import DLServerError

        def fake_send(session_id, node_id, code, file_extension, course_id=0, **kw):
            raise DLServerError(
                f"send-solution(nodeId={node_id}, fileExtension='.mpc', "
                f"codeLen=111, codeHead='...'): DL API вернул ошибку "
                f"(код 400): Bad Request"
            )

        with patch("ai.dl_api_client.send_solution_to_dl", fake_send):
            res = arm_runner._test_solution_on_dl(
                "SID", 2606747, "code...", ".mpc", task_id=2001,
            )
        self.assertEqual(res["queue_id"], 0)
        self.assertIn("код 400", res["submit_error"])
        # Оба идентификатора видны оператору для проверки вручную.
        self.assertIn("nodeId=2606747", res["submit_error"])
        self.assertIn("taskId=2001", res["submit_error"])
        # Старая неверная подсказка о языке удалена.
        self.assertNotIn("язык выбран неверно", res["submit_error"])
        # Хардкод «(400)» в финальной фразе удалён — реальный код отказа
        # виден выше, в тексте исключения.
        self.assertNotIn("отправку решения (400)", res["submit_error"])
        self.assertIn("DL отклонил отправку решения", res["submit_error"])

    def test_400_without_task_id_skips_fallback(self):
        from ai import arm_runner
        from ai.dl_api_client import DLServerError

        calls = []

        def fake_send(session_id, node_id, code, file_extension, course_id=0, **kw):
            calls.append(node_id)
            raise DLServerError(
                f"send-solution(nodeId={node_id}, ...): DL API вернул ошибку "
                f"(код 400): Bad Request"
            )

        with patch("ai.dl_api_client.send_solution_to_dl", fake_send):
            res = arm_runner._test_solution_on_dl(
                "SID", 2606747, "code...", ".cpp", task_id=0,
            )
        # Без taskId fallback не делается — ровно один вызов (по nodeId).
        self.assertEqual(calls, [2606747])
        self.assertEqual(res["queue_id"], 0)
        self.assertIn("код 400", res["submit_error"])


class SambanovaLoggerTests(SimpleTestCase):
    """sambanova.py must define a module-level logger — its absence made every
    SambaNova model FAIL health-check with `name 'logger' is not defined`."""

    def test_module_defines_logger(self):
        import logging as _logging
        from ai.model_clients import sambanova
        self.assertTrue(hasattr(sambanova, "logger"))
        self.assertIsInstance(sambanova.logger, _logging.Logger)
        # The functions referenced by the registry must import cleanly.
        # getattr, т.к. ask_* генерируются динамически (make_table_handlers)
        # и статический анализатор их не видит.
        self.assertTrue(callable(getattr(sambanova, "ask_DeepSeek_V3_2_async")))
        self.assertTrue(callable(getattr(sambanova, "ask_Gpt_oss_120b_async")))


class TaskRegistryTests(TestCase):
    """Auto-registration of DL tasks solved via the chat page."""

    def setUp(self):
        self.lang = ProgrammingLanguage.objects.create(language_name="Pascal")
        self.other_lang = ProgrammingLanguage.objects.create(language_name="Python")
        self.topic = Topic.objects.create(topic_name_ru="Линейные", programming_language=self.lang)

    def test_apply_dl_task_info_sets_truthy_fields_only(self):
        from ai.models import Task
        from ai.services import apply_dl_task_info
        t = Task.objects.create(node_id=1, name="old", statement="old stmt", task_id=11)
        apply_dl_task_info(t, {"taskId": 22, "name": "Новое название", "statement": "Новое условие"})
        self.assertEqual(t.task_id, 22)
        self.assertEqual(t.name, "Новое название")
        self.assertEqual(t.statement, "Новое условие")
        # Empty values must not clobber existing fields.
        apply_dl_task_info(t, {"taskId": None, "name": "", "statement": ""})
        self.assertEqual(t.task_id, 22)
        self.assertEqual(t.name, "Новое название")
        self.assertEqual(t.statement, "Новое условие")

    def test_ensure_task_creates_inactive_and_fills_from_dl(self):
        from ai.models import Task
        from ai.services import ensure_task
        dl_data = {"taskId": 777, "name": "Сумма", "statement": "Даны a и b, верните a+b"}
        with patch("ai.services.task_registry.fetch_task_info", return_value=dl_data):
            task = ensure_task(
                42, programming_language_id=self.lang.id, topic_id=self.topic.id, session_id="DLSID-1"
            )
        self.assertIsNotNone(task)
        self.assertEqual(task.node_id, 42)
        self.assertFalse(task.active)  # auto-registered tasks are inactive until operator readies them
        self.assertEqual(task.programming_language_id, self.lang.id)
        self.assertEqual(task.topic_id, self.topic.id)
        self.assertEqual(task.task_id, 777)
        self.assertEqual(task.name, "Сумма")
        self.assertEqual(task.statement, "Даны a и b, верните a+b")
        self.assertEqual(Task.objects.filter(node_id=42).count(), 1)

    def test_ensure_task_without_session_creates_without_dl(self):
        from ai.services import ensure_task
        with patch("ai.services.task_registry.fetch_task_info") as mocked:
            task = ensure_task(7, programming_language_id=self.lang.id, topic_id=self.topic.id, session_id=None)
        self.assertIsNotNone(task)
        self.assertFalse(task.active)
        self.assertEqual(task.name, "")
        self.assertEqual(task.statement, "")
        self.assertIsNone(task.task_id)
        mocked.assert_not_called()  # no DL fetch without a session

    def test_ensure_task_existing_updates_assignments_no_dl(self):
        from ai.models import Task, Topic
        from ai.services import ensure_task
        # Тема чужого языка не stale только если принадлежит целевому языку —
        # тогда перегадывание не нужно и DL-фэтч не требуется.
        other_topic = Topic.objects.create(topic_name_ru="Циклы", programming_language=self.other_lang)
        Task.objects.create(
            node_id=9, name="exists", statement="stmt", task_id=5,
            programming_language=self.other_lang, topic=other_topic, file_extension=".pas", active=True,
        )
        with patch("ai.services.task_registry.fetch_task_info") as mocked:
            task = ensure_task(
                9, programming_language_id=self.other_lang.id, topic_id=None, session_id="DLSID"
            )
        self.assertEqual(task.node_id, 9)
        # Local assignments refreshed to the latest solve request; active unchanged.
        self.assertEqual(task.programming_language_id, self.other_lang.id)
        self.assertTrue(task.active)
        # No DL fetch for an already-existing task with a language-consistent topic.
        mocked.assert_not_called()

    def test_ensure_task_reguesses_stale_topic_from_new_language(self):
        """Тема per-языковая: задача, зарегистрированная прогоном на Pascal,
        хранит Pascal-тему. Прогон на Python должен перегадать тему из path
        (тема = имя папки DL) с фильтром по языку — иначе резолв привязки
        уходит как (Python, Pascal-тема) и точная привязка не находится."""
        from ai.models import Task, Topic
        from ai.services import ensure_task
        py_topic = Topic.objects.create(topic_name_ru="Линейные", programming_language=self.other_lang)
        Task.objects.create(
            node_id=9, name="exists", statement="stmt", task_id=5,
            programming_language=self.lang, topic=self.topic, file_extension=".pas", active=True,
        )
        dl_data = {
            "taskId": 5, "name": "exists", "statement": "stmt",
            "path": "Программирование\\Линейные\\Задача1",
        }
        with patch("ai.services.task_registry.fetch_task_info", return_value=dl_data) as mocked:
            task = ensure_task(9, programming_language_id=self.other_lang.id, session_id="DLSID")
        mocked.assert_called_once()  # stale-тема требует фэтча: path нужен для перегадывания
        self.assertEqual(task.programming_language_id, self.other_lang.id)
        self.assertEqual(task.topic_id, py_topic.id)
        self.assertTrue(task.active)

    def test_ensure_task_clears_stale_topic_when_missing_in_language(self):
        """В целевом языке темы с таким названием нет: чужая тема хуже
        отсутствующей — сбрасываем, резолв привязки честно уйдёт «на весь язык»."""
        from ai.models import Task
        from ai.services import ensure_task
        Task.objects.create(
            node_id=9, name="exists", statement="stmt", task_id=5,
            programming_language=self.lang, topic=self.topic, file_extension=".pas", active=True,
        )
        # «Линейные» под Python не заведена; в path нет keyword-тем.
        dl_data = {
            "taskId": 5, "name": "exists", "statement": "stmt",
            "path": "Программирование\\Линейные\\Задача1",
        }
        with patch("ai.services.task_registry.fetch_task_info", return_value=dl_data):
            task = ensure_task(9, programming_language_id=self.other_lang.id, session_id="DLSID")
        self.assertEqual(task.programming_language_id, self.other_lang.id)
        self.assertIsNone(task.topic_id)
        self.assertTrue(task.active)

    def test_guess_topic_from_path_is_language_scoped(self):
        """Два Topic с одинаковым topic_name_ru под разными языками (курс
        «[Ассемблер i8086, C-MPA]»): с языком ищем только в нём; без языка —
        прежнее поведение. Чужой язык не подставляется никогда."""
        from ai.models import Topic
        from ai.services.task_registry import _guess_topic_from_path
        Topic.objects.create(topic_name_ru="Линейные", programming_language=self.other_lang)
        third_lang = ProgrammingLanguage.objects.create(language_name="C++")
        path = "Программирование\\Линейные\\Задача1"
        self.assertEqual(
            _guess_topic_from_path(path, programming_language_id=self.lang.id).id,
            self.topic.id,
        )
        self.assertEqual(
            _guess_topic_from_path(path, programming_language_id=self.other_lang.id).programming_language_id,
            self.other_lang.id,
        )
        # В языке C++ темы «Линейные» нет — None, а не чужая тема.
        self.assertIsNone(_guess_topic_from_path(path, programming_language_id=third_lang.id))
        # Без языка — прежнее поведение: какой-то из одноимённых Topic.
        self.assertIsNotNone(_guess_topic_from_path(path))

    def test_ensure_task_swallows_dl_errors(self):
        from ai.services import ensure_task
        from ai.dl_api_client import DLApiError
        with patch("ai.services.task_registry.fetch_task_info", side_effect=DLApiError("boom")):
            task = ensure_task(100, programming_language_id=self.lang.id, topic_id=None, session_id="DLSID")
        # Task still created; DL fields just left blank. No exception propagated.
        self.assertIsNotNone(task)
        self.assertEqual(task.node_id, 100)
        self.assertEqual(task.name, "")

    def test_ensure_task_never_raises(self):
        from ai.services import ensure_task
        with patch("ai.models.Task.objects.get_or_create", side_effect=RuntimeError("db down")):
            result = ensure_task(200, session_id="DLSID")
        self.assertIsNone(result)  # registration must never break the chat


# ===================================================================

class ModelSortingTests(SimpleTestCase):
    """Tests for _get_user_top_model_keys and model sorting in _render_ai_page."""

    def setUp(self):
        self.factory = RequestFactory()

    def _make_user(self, pk=1):
        return SimpleNamespace(
            is_authenticated=True,
            is_active=True,
            username="sort-user",
            pk=pk,
        )

    def test_top_model_keys_returns_empty_for_anonymous(self):
        """No user pk → empty list, no exception."""
        from ai.views import _get_user_top_model_keys
        request = self.factory.get("/ai/chat/")
        request.user = SimpleNamespace(is_authenticated=False, pk=None)
        result = _get_user_top_model_keys(request, limit=2)
        self.assertEqual(result, [])

    def test_top_model_keys_returns_empty_for_string_user(self):
        """String user (no pk) → empty list."""
        from ai.views import _get_user_top_model_keys
        request = self.factory.get("/ai/chat/")
        request.user = "some-session-string"
        result = _get_user_top_model_keys(request, limit=2)
        self.assertEqual(result, [])

    def test_top_model_keys_returns_empty_when_no_logs(self):
        """User with no AIRequestLog entries → empty list."""
        from ai.views import _get_user_top_model_keys
        request = self.factory.get("/ai/chat/")
        request.user = self._make_user(pk=999)
        result = _get_user_top_model_keys(request, limit=2)
        self.assertEqual(result, [])

    def test_user_top_models_come_first(self):
        """User's top-2 models first, then the rest strictly alphabetical (no web priority)."""
        from ai.views import _render_ai_page
        models_data = [
            {"key": "DeepSeek_V3_1", "title": "DeepSeek V3.1", "capabilities": {}},
            {"key": "Groq_Llama_3_3_70B", "title": "Groq Llama 3.3 70B", "capabilities": {}},
            {"key": "Web_DeepSeek", "title": "Web DeepSeek", "capabilities": {}},
            {"key": "Gemma_3_12b_it", "title": "Gemma 3 12b IT", "capabilities": {}},
            {"key": "MiniMax_M2_5", "title": "MiniMax M2.5", "capabilities": {}},
        ]
        request = self.factory.get("/ai/chat/")
        request.user = self._make_user(pk=1)
        request.session = {}
        request.user_info = {"userId": "sort-user"}
        request.COOKIES = {"userId": "sort-user"}
        with patch("ai.views._has_page_access", return_value=True),              patch("ai.views.AIAppSettings.get_solo", return_value=SimpleNamespace(is_enabled=True)),              patch("ai.views.get_available_model_options", return_value=models_data),              patch("ai.views._get_user_top_model_keys", return_value=["Groq_Llama_3_3_70B", "DeepSeek_V3_1"]),              patch("ai.views.render", return_value=HttpResponse("ok")) as mock_render:
            _render_ai_page(request, "ai/chat.html")
        context = mock_render.call_args[0][2]
        keys = [m["key"] for m in context["available_models"]]
        self.assertEqual(keys[0], "Groq_Llama_3_3_70B")
        self.assertEqual(keys[1], "DeepSeek_V3_1")
        self.assertEqual(keys[2], "Gemma_3_12b_it")
        self.assertEqual(keys[3], "MiniMax_M2_5")
        self.assertEqual(keys[4], "Web_DeepSeek")

    def test_no_user_top_models_strictly_alphabetical(self):
        """Without user top models: everything strictly alphabetical by title."""
        from ai.views import _render_ai_page
        models_data = [
            {"key": "DeepSeek_V3_1", "title": "DeepSeek V3.1", "capabilities": {}},
            {"key": "Web_DeepSeek", "title": "Web DeepSeek", "capabilities": {}},
            {"key": "Web_DeepSeek_Thinking", "title": "Web DeepSeek Thinking", "capabilities": {}},
            {"key": "Gemma_3_12b_it", "title": "Gemma 3 12b IT", "capabilities": {}},
            {"key": "MiniMax_M2_5", "title": "MiniMax M2.5", "capabilities": {}},
        ]
        request = self.factory.get("/ai/chat/")
        request.user = self._make_user(pk=1)
        request.session = {}
        request.user_info = {"userId": "sort-user"}
        request.COOKIES = {"userId": "sort-user"}
        with patch("ai.views._has_page_access", return_value=True),              patch("ai.views.AIAppSettings.get_solo", return_value=SimpleNamespace(is_enabled=True)),              patch("ai.views.get_available_model_options", return_value=models_data),              patch("ai.views._get_user_top_model_keys", return_value=[]),              patch("ai.views.render", return_value=HttpResponse("ok")) as mock_render:
            _render_ai_page(request, "ai/chat.html")
        context = mock_render.call_args[0][2]
        keys = [m["key"] for m in context["available_models"]]
        self.assertEqual(keys[0], "DeepSeek_V3_1")
        self.assertEqual(keys[1], "Gemma_3_12b_it")
        self.assertEqual(keys[2], "MiniMax_M2_5")
        self.assertEqual(keys[3], "Web_DeepSeek")
        self.assertEqual(keys[4], "Web_DeepSeek_Thinking")

    def test_user_top_first_no_duplicates(self):
        """User's top models first, the rest strictly alphabetical, no duplicate keys."""
        from ai.views import _render_ai_page
        models_data = [
            {"key": "Web_DeepSeek", "title": "Web DeepSeek", "capabilities": {}},
            {"key": "Web_DeepSeek_Thinking", "title": "Web DeepSeek Thinking", "capabilities": {}},
            {"key": "Gemma_3_12b_it", "title": "Gemma 3 12b IT", "capabilities": {}},
            {"key": "MiniMax_M2_5", "title": "MiniMax M2.5", "capabilities": {}},
        ]
        request = self.factory.get("/ai/chat/")
        request.user = self._make_user(pk=1)
        request.session = {}
        request.user_info = {"userId": "sort-user"}
        request.COOKIES = {"userId": "sort-user"}
        with patch("ai.views._has_page_access", return_value=True),              patch("ai.views.AIAppSettings.get_solo", return_value=SimpleNamespace(is_enabled=True)),              patch("ai.views.get_available_model_options", return_value=models_data),              patch("ai.views._get_user_top_model_keys", return_value=["Web_DeepSeek", "Gemma_3_12b_it"]),              patch("ai.views.render", return_value=HttpResponse("ok")) as mock_render:
            _render_ai_page(request, "ai/chat.html")
        context = mock_render.call_args[0][2]
        keys = [m["key"] for m in context["available_models"]]
        self.assertEqual(keys[0], "Web_DeepSeek")
        self.assertEqual(keys[1], "Gemma_3_12b_it")
        self.assertEqual(keys[2], "MiniMax_M2_5")
        self.assertEqual(keys[3], "Web_DeepSeek_Thinking")
        self.assertEqual(len(keys), len(set(keys)))

    def test_default_model_first_without_user_top(self):
        """Без фаворита модель по умолчанию — первая, ключ уходит в шаблон (selected)."""
        from ai.views import _render_ai_page
        from ai.model_clients.registry import DEFAULT_MODEL_KEY
        models_data = [
            {"key": "Aaa_Model", "title": "Aaa Model", "capabilities": {}},
            {"key": DEFAULT_MODEL_KEY, "title": "Ollama Gemma 4", "capabilities": {}},
            {"key": "Zzz_Model", "title": "Zzz Model", "capabilities": {}},
        ]
        request = self.factory.get("/ai/chat/")
        request.user = self._make_user(pk=1)
        request.session = {}
        request.user_info = {"userId": "sort-user"}
        request.COOKIES = {"userId": "sort-user"}
        with patch("ai.views._has_page_access", return_value=True),              patch("ai.views.AIAppSettings.get_solo", return_value=SimpleNamespace(is_enabled=True)),              patch("ai.views.get_available_model_options", return_value=models_data),              patch("ai.views._get_user_top_model_keys", return_value=[]),              patch("ai.views.render", return_value=HttpResponse("ok")) as mock_render:
            _render_ai_page(request, "ai/chat.html")
        context = mock_render.call_args[0][2]
        keys = [m["key"] for m in context["available_models"]]
        self.assertEqual(keys[0], DEFAULT_MODEL_KEY)
        self.assertEqual(keys[1], "Aaa_Model")
        self.assertEqual(keys[2], "Zzz_Model")
        self.assertEqual(context["default_model_key"], DEFAULT_MODEL_KEY)

    def test_user_top_beats_default_model(self):
        """Фаворит сильнее дефолта: default_model_key пуст, дефолт остаётся в списке."""
        from ai.views import _render_ai_page
        from ai.model_clients.registry import DEFAULT_MODEL_KEY
        models_data = [
            {"key": "Aaa_Model", "title": "Aaa Model", "capabilities": {}},
            {"key": DEFAULT_MODEL_KEY, "title": "Ollama Gemma 4", "capabilities": {}},
            {"key": "Zzz_Model", "title": "Zzz Model", "capabilities": {}},
        ]
        request = self.factory.get("/ai/chat/")
        request.user = self._make_user(pk=1)
        request.session = {}
        request.user_info = {"userId": "sort-user"}
        request.COOKIES = {"userId": "sort-user"}
        with patch("ai.views._has_page_access", return_value=True),              patch("ai.views.AIAppSettings.get_solo", return_value=SimpleNamespace(is_enabled=True)),              patch("ai.views.get_available_model_options", return_value=models_data),              patch("ai.views._get_user_top_model_keys", return_value=["Zzz_Model"]),              patch("ai.views.render", return_value=HttpResponse("ok")) as mock_render:
            _render_ai_page(request, "ai/chat.html")
        context = mock_render.call_args[0][2]
        keys = [m["key"] for m in context["available_models"]]
        self.assertEqual(keys[0], "Zzz_Model")
        self.assertIn(DEFAULT_MODEL_KEY, keys)
        self.assertEqual(context["default_model_key"], "")

    def test_experimental_models_bottom_separate_group(self):
        """OpenRouter (OR_*) — отдельная группа experimental_models в самом низу:
        не попадает в «Все модели», в фавориты и в deфолт."""
        from ai.views import _render_ai_page
        models_data = [
            {"key": "OR_Nemotron_Nano_12B_VL", "title": "OR Nemotron Nano 12B VL", "capabilities": {}},
            {"key": "Web_DeepSeek", "title": "Web DeepSeek", "capabilities": {}},
            {"key": "OR_Nemotron_Nano_9B", "title": "OR Nemotron Nano 9B", "capabilities": {}},
            {"key": "Gemma_3_12b_it", "title": "Gemma 3 12b IT", "capabilities": {}},
            {"key": "OR_Nemotron_Ultra_550B", "title": "OR Nemotron Ultra 550B", "capabilities": {}},
        ]
        request = self.factory.get("/ai/chat/")
        request.user = self._make_user(pk=1)
        request.session = {}
        request.user_info = {"userId": "sort-user"}
        request.COOKIES = {"userId": "sort-user"}
        # Даже если OR-модель — топ-1 пользователя, она не должна вылезти на верх.
        with patch("ai.views._has_page_access", return_value=True),              patch("ai.views.AIAppSettings.get_solo", return_value=SimpleNamespace(is_enabled=True)),              patch("ai.views.get_available_model_options", return_value=models_data),              patch("ai.views._get_user_top_model_keys", return_value=["OR_Nemotron_Ultra_550B"]),              patch("ai.views.render", return_value=HttpResponse("ok")) as mock_render:
            _render_ai_page(request, "ai/chat.html")
        context = mock_render.call_args[0][2]
        flat_keys = [m["key"] for m in context["available_models"]]
        # user_top — OR-модель, но она НЕ становится фаворитом: Experimental-
        # модели всегда в самом низу (плоский список тоже их держит в конце).
        self.assertEqual(
            flat_keys[-3:],
            ["OR_Nemotron_Nano_12B_VL", "OR_Nemotron_Nano_9B", "OR_Nemotron_Ultra_550B"],
        )
        self.assertEqual(
            [m["key"] for m in context["experimental_models"]],
            ["OR_Nemotron_Nano_12B_VL", "OR_Nemotron_Nano_9B", "OR_Nemotron_Ultra_550B"],
            "Experimental-модели — по алфавиту внизу",
        )
        self.assertNotIn("OR_Nemotron_Ultra_550B", [m["key"] for m in context["favorite_models"]])
        exp_keys = {m["key"] for m in context["experimental_models"]}
        for m in context["other_models"]:
            self.assertNotIn(m["key"], exp_keys)
        for m in context["favorite_models"]:
            self.assertNotIn(m["key"], exp_keys)
        for m in context["other_models"]:
            self.assertNotEqual(m["key"], "OR_Nemotron_Nano_12B_VL")



# ===================================================================
# Tests for problem-data API (languages, topics, prompts)
# ===================================================================

class ProblemDataApiTests(TestCase):
    """Verify that get_problem_data returns correct data for selection."""

    def setUp(self):
        self.factory = RequestFactory()
        self.user = get_user_model().objects.create_user(username="pd_user", password="test-pass")

    def _request(self, ui_language=""):
        params = {}
        if ui_language:
            params["ui_language"] = ui_language
        request = self.factory.get("/ai/api/problem-data/", data=params)
        request.user = self.user
        request.user_info = {"userId": self.user.username}
        request.COOKIES = {"userId": self.user.username}
        return request

    def test_problem_data_returns_languages(self):
        ProgrammingLanguage.objects.create(language_name="Python")
        ProgrammingLanguage.objects.create(language_name="C++")
        response = get_problem_data(self._request("Russian"))
        data = json.loads(response.content)
        lang_names = [l["language_name"] for l in data["languages"]]
        self.assertIn("Python", lang_names)
        self.assertIn("C++", lang_names)
        self.assertGreaterEqual(len(data["languages"]), 2)

    def test_problem_data_returns_topics_filtered_by_language(self):
        pl1 = ProgrammingLanguage.objects.create(language_name="Python")
        pl2 = ProgrammingLanguage.objects.create(language_name="C++")
        Topic.objects.create(topic_name_ru="Loops", programming_language=pl1)
        Topic.objects.create(topic_name_ru="Arrays", programming_language=pl1)
        Topic.objects.create(topic_name_ru="Pointers", programming_language=pl2)
        response = get_problem_data(self._request("Russian"))
        data = json.loads(response.content)
        self.assertEqual(len(data["topics"]), 3)
        # Check topics are linked to correct languages
        py_topics = [t for t in data["topics"] if t["programming_language"] == pl1.id]
        self.assertEqual(len(py_topics), 2)
        cpp_topics = [t for t in data["topics"] if t["programming_language"] == pl2.id]
        self.assertEqual(len(cpp_topics), 1)

    def test_problem_data_returns_prompts(self):
        pl = ProgrammingLanguage.objects.create(language_name="Python")
        topic = Topic.objects.create(topic_name_ru="Loops", programming_language=pl)
        Prompt.objects.create(topic=topic, prompt_name_ru="Loop helper", prompt_text_ru="Help with loops")
        response = get_problem_data(self._request("Russian"))
        data = json.loads(response.content)
        self.assertGreaterEqual(len(data["prompts"]), 1)
        self.assertEqual(data["prompts"][0]["prompt_name"], "Loop helper")

    def test_problem_data_requires_auth(self):
        request = self.factory.get("/ai/api/problem-data/")
        request.user = AnonymousUser()
        request.user_info = None
        request.COOKIES = {}
        response = get_problem_data(request)
        self.assertEqual(response.status_code, 403)

    def test_problem_data_returns_shared_prompts(self):
        pl = ProgrammingLanguage.objects.create(language_name="Python")
        sp = SharedPrompt.objects.create(prompt_name_ru="Common prep", prompt_text_ru="Common text")
        sp.programming_languages.add(pl)
        response = get_problem_data(self._request("Russian"))
        data = json.loads(response.content)
        self.assertGreaterEqual(len(data["shared_prompts"]), 1)
        self.assertEqual(data["shared_prompts"][0]["prompt_name"], "Common prep")

    def test_problem_data_response_has_all_keys(self):
        response = get_problem_data(self._request("Russian"))
        data = json.loads(response.content)
        self.assertIn("languages", data)
        self.assertIn("topics", data)
        self.assertIn("prompts", data)
        self.assertIn("shared_prompts", data)

    def test_problem_data_prompts_carry_mode(self):
        """Каждый промпт в payload несёт mode (solve/find_error) — клиент
        фильтрует список по режиму страницы на фронте."""
        pl = ProgrammingLanguage.objects.create(language_name="Python")
        topic = Topic.objects.create(topic_name_ru="Loops", programming_language=pl)
        Prompt.objects.create(
            topic=topic, prompt_name_ru="Solve it", prompt_text_ru="S",
            mode=Prompt.MODE_SOLVE,
        )
        Prompt.objects.create(
            topic=topic, prompt_name_ru="Find bug", prompt_text_ru="F",
            mode=Prompt.MODE_FIND_ERROR,
        )
        response = get_problem_data(self._request("Russian"))
        data = json.loads(response.content)
        modes = {p["prompt_name"]: p["mode"] for p in data["prompts"]}
        self.assertEqual(modes["Solve it"], "solve")
        self.assertEqual(modes["Find bug"], "find_error")


# ===================================================================
# Tests for _get_user_top_model_keys with real AIRequestLog data
# ===================================================================

class UserTopModelKeysTests(TestCase):
    """Test _get_user_top_model_keys with real AIRequestLog records."""

    def setUp(self):
        from django.core.cache import cache
        # LocMem-кеш не откатывается между тестами с TestCase, а несколько тестов
        # ниже используют одного user_id=42 с разным набором логов — без очистки
        # кеш _get_user_top_model_keys возвращал бы результат предыдущего теста.
        cache.clear()
        self.factory = RequestFactory()
        self.user = get_user_model().objects.create_user(
            username="freq_user", password="test-pass", pk=42,
        )
        # Считаем всю историю: сбрасываем дату-отсечку фаворитов, чтобы
        # созданные ниже логи (sent_at ≈ now) не отфильтровывались epoch-фильтром
        # в _get_user_top_model_keys (по умолчанию favorites_epoch = now).
        from ai.models import AIAppSettings
        settings = AIAppSettings.get_solo()
        settings.favorites_epoch = None
        settings.save(update_fields=["favorites_epoch"])

    def _make_request(self, user=None):
        request = self.factory.get("/ai/chat/")
        request.user = user or self.user
        request.session = {}
        return request

    def _make_log(self, model_names, status="success"):
        AIRequestLog.objects.create(
            user=self.user,
            username=self.user.username,
            external_user_id="42",
            user_full_name="Freq User",
            client_id="client-1",
            source=AIRequestLog.SOURCE_WEBSOCKET,
            mode=AIRequestLog.MODE_CHAT,
            sent_at=timezone.now(),
            model_names=model_names,
            message="test",
            status=status,
        )

    def test_experimental_models_excluded_from_user_top(self):
        """OR_*-модели в логах не становятся фаворитами: считаем только обычные."""
        from ai.model_clients.registry import EXPERIMENTAL_MODEL_PREFIXES

        from ai.views import _get_user_top_model_keys

        self._make_log(["OR_Nemotron_Nano_9B"])
        self._make_log(["OR_Nemotron_Nano_9B"])
        self._make_log(["Web_DeepSeek"])

        result = _get_user_top_model_keys(self._make_request(), limit=2)
        for key in result:
            self.assertFalse(key.startswith(EXPERIMENTAL_MODEL_PREFIXES))
        self.assertIn("Web_DeepSeek", result)

    def test_top_model_keys_read_keys_directly(self):
        """model_names в логах содержит КЛЮЧИ (consumers.py пишет key) — фаворит
        должен вычисляться и без title_to_key-транзита."""
        from ai.views import _get_user_top_model_keys

        self._make_log(["Web_Kimi"])
        self._make_log(["Web_Kimi"])
        self._make_log(["Web_DeepSeek"])

        result = _get_user_top_model_keys(self._make_request(), limit=2)
        self.assertEqual(result[0], "Web_Kimi")

    def test_returns_most_frequent_models(self):
        """The most frequently used models should be returned in order."""
        from ai.views import _get_user_top_model_keys
        from ai.model_clients import registry

        # Get actual model titles from registry
        all_keys = list(registry.keys())
        if len(all_keys) < 3:
            self.skipTest("Need at least 3 models in registry")
        key_a = all_keys[0]
        key_b = all_keys[1]
        key_c = all_keys[2]
        title_a = registry.title(key_a)
        title_b = registry.title(key_b)
        title_c = registry.title(key_c)

        # Create logs: key_a used 3 times, key_b used 2 times, key_c used 1 time
        for _ in range(3):
            self._make_log([title_a])
        for _ in range(2):
            self._make_log([title_b])
        for _ in range(1):
            self._make_log([title_c])

        result = _get_user_top_model_keys(self._make_request(), limit=2)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0], key_a)
        self.assertEqual(result[1], key_b)

    def test_error_logs_are_excluded(self):
        """Only success logs should be counted."""
        from ai.views import _get_user_top_model_keys
        from ai.model_clients import registry

        all_keys = list(registry.keys())
        if len(all_keys) < 2:
            self.skipTest("Need at least 2 models in registry")
        key_a = all_keys[0]
        title_a = registry.title(key_a)

        # Error log should NOT be counted
        self._make_log([title_a], status="error")
        # Success log should be counted
        self._make_log([title_a], status="success")

        result = _get_user_top_model_keys(self._make_request(), limit=2)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0], key_a)

    def test_limit_respected(self):
        """Should respect the limit parameter."""
        from ai.views import _get_user_top_model_keys
        from ai.model_clients import registry

        all_keys = list(registry.keys())
        if len(all_keys) < 5:
            self.skipTest("Need at least 5 models in registry")
        for i in range(5):
            key = all_keys[i]
            self._make_log([registry.title(key)])

        result = _get_user_top_model_keys(self._make_request(), limit=3)
        self.assertEqual(len(result), 3)

    def test_other_users_logs_excluded(self):
        """Logs from other users should NOT affect this user's top models."""
        from ai.views import _get_user_top_model_keys
        from ai.model_clients import registry

        all_keys = list(registry.keys())
        if len(all_keys) < 2:
            self.skipTest("Need at least 2 models in registry")
        key_a = all_keys[0]
        key_b = all_keys[1]
        title_a = registry.title(key_a)
        title_b = registry.title(key_b)

        # Other user's logs
        other_user = get_user_model().objects.create_user(
            username="other_freq_user", password="test-pass", pk=99,
        )
        for _ in range(5):
            AIRequestLog.objects.create(
                user=other_user,
                username=other_user.username,
                external_user_id="99",
                user_full_name="Other User",
                client_id="client-2",
                source=AIRequestLog.SOURCE_WEBSOCKET,
                mode=AIRequestLog.MODE_CHAT,
                sent_at=timezone.now(),
                model_names=[title_b],
                message="test",
                status="success",
            )
        # This user's logs
        self._make_log([title_a])

        result = _get_user_top_model_keys(self._make_request(), limit=2)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0], key_a)

    def test_result_is_cached(self):
        """Результат кешируется per-user (TTL 120 c): повторный вызов без
        инвалидации возвращает закешированный список; после cache.clear() —
        пересчитывается по актуальным логам.
        """
        from ai.views import _get_user_top_model_keys
        from ai.model_clients import registry
        from django.core.cache import cache

        all_keys = list(registry.keys())
        if len(all_keys) < 2:
            self.skipTest("Need at least 2 models in registry")
        key_a = all_keys[0]
        key_b = all_keys[1]
        title_a = registry.title(key_a)
        title_b = registry.title(key_b)

        # Сначала key_a — единственный, он и попадёт в топ.
        for _ in range(2):
            self._make_log([title_a])
        result1 = _get_user_top_model_keys(self._make_request(), limit=2)
        self.assertEqual(result1, [key_a])

        # Добавляем много логов с key_b, но кеш ещё валиден (TTL 120 c) —
        # повторный вызов должен вернуть закешированный [key_a], не key_b.
        for _ in range(5):
            self._make_log([title_b])
        result2 = _get_user_top_model_keys(self._make_request(), limit=2)
        self.assertEqual(result2, [key_a])

        # После очистки кеша — пересчёт: теперь key_b встречается чаще.
        cache.clear()
        result3 = _get_user_top_model_keys(self._make_request(), limit=2)
        self.assertEqual(result3[0], key_b)


# ===================================================================
# Tests for admin Updates section (UpdateLog)
# ===================================================================

class UpdateLogAdminViewMixin:
    """Request-заготовки для тестов страницы «Обновления» (ai/admin/updates.py)."""

    def _make_request(self, user=None, params=None):
        request = self.factory.get("/ai/admin/updates/", data=params or {})
        request.user = user or self.superuser
        request.user_info = {"userId": getattr(user, "username", "test")}
        request.COOKIES = {"userId": getattr(user, "username", "test")}
        request.session = {"admin_fresh_auth": True}
        return request

    def _call_view(self, request):
        """Call admin_updates_view bypassing admin_view decorator checks."""
        from ai.admin.updates import admin_updates_view
        # The view is wrapped by admin_view which checks DLSID auth.
        # We need to bypass that for testing — call the inner function directly.
        import ai.admin.site as site_module
        original_admin_view = site_module.ai_admin_site.admin_view

        # Temporarily replace admin_view with a passthrough
        def passthrough_admin_view(view, cacheable=False):
            return view
        site_module.ai_admin_site.admin_view = passthrough_admin_view

        # Re-import to get the unwrapped view
        import importlib
        import ai.admin.updates as updates_module
        importlib.reload(updates_module)
        response = updates_module.admin_updates_view(request)

        # Restore
        site_module.ai_admin_site.admin_view = original_admin_view
        return response


class UpdateLogAdminTests(UpdateLogAdminViewMixin, TestCase):
    """Tests for the admin updates view: access control, filtering, search."""

    def setUp(self):
        self.factory = RequestFactory()
        # Очищаем данные от миграции 0026, чтобы тесты были изолированы
        UpdateLog.objects.all().delete()
        self.superuser = get_user_model().objects.create_superuser(
            username="upd_admin", password="***", email="admin@test.com",
        )
        self.normal_user = get_user_model().objects.create_user(
            username="upd_normal", password="***",
        )
        # Baseline non-superuser audience (page.prompt_defaults и пр.).
        make_prompt_developer(self.normal_user)

        # Create some test data
        self.u1 = UpdateLog.objects.create(
            commit_date="2026-07-27", description="Нововведение: test feature",
            author="whmi1", commit_hash="abc123",
        )
        self.u2 = UpdateLog.objects.create(
            commit_date="2026-07-11", description="Исправление: bug fix",
            author="Archi", commit_hash="def456",
        )
        self.u3 = UpdateLog.objects.create(
            commit_date="2026-06-22", description="fix build v2",
            author="ArchiBaldEgo", commit_hash="ghi789",
        )

    def test_superuser_can_access(self):
        """Superuser should see the updates page."""
        from ai.admin.updates import admin_updates_view
        response = self._call_view(self._make_request(self.superuser))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Обновления")

    def test_non_superuser_forbidden(self):
        """Non-superuser should get 403."""
        from ai.admin.updates import admin_updates_view
        response = self._call_view(self._make_request(self.normal_user))
        self.assertEqual(response.status_code, 403)

    def test_filter_by_author(self):
        """Filtering by author should return only that author's commits."""
        request = self._make_request(self.superuser, {"author": "Archi"})
        response = self._call_view(request)
        self.assertEqual(response.status_code, 200)
        # The filtered results should contain Archi's commit
        self.assertContains(response, "Archi")
        # Total count should be 1 (only Archi's commit)
        self.assertContains(response, "записей: 1")

    def test_search_by_description(self):
        """Text search should match description."""
        from ai.admin.updates import admin_updates_view
        request = self._make_request(self.superuser, {"q": "test feature"})
        response = self._call_view(request)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "test feature")
        self.assertNotContains(response, "bug fix")

    def test_filter_by_date_range(self):
        """Date range filter should narrow results."""
        request = self._make_request(self.superuser, {
            "date_from": "2026-07-01", "date_to": "2026-07-31",
        })
        response = self._call_view(request)
        self.assertEqual(response.status_code, 200)
        # Should include whmi1 (2026-07-27) and Archi (2026-07-11)
        self.assertContains(response, "whmi1")
        self.assertContains(response, "Archi")
        # Total count should be 2 (both in July)
        self.assertContains(response, "записей: 2")

    def test_pagination_works(self):
        """Page parameter should not crash."""
        from ai.admin.updates import admin_updates_view
        request = self._make_request(self.superuser, {"page": "1"})
        response = self._call_view(request)
        self.assertEqual(response.status_code, 200)

    def test_combined_filter(self):
        """Author + date + search combined should work."""
        from ai.admin.updates import admin_updates_view
        request = self._make_request(self.superuser, {
            "author": "Archi", "date_from": "2026-07-01", "q": "bug",
        })
        response = self._call_view(request)
        self.assertEqual(response.status_code, 200)

    def test_empty_results(self):
        """Search with no matches should show 'no records' message."""
        from ai.admin.updates import admin_updates_view
        request = self._make_request(self.superuser, {"q": "nonexistent_xyz"})
        response = self._call_view(request)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Нет записей")

    def test_hidden_rows_not_displayed(self):
        """Скрытые записи (hidden=True) не видны на «Обновлениях» и в счётчике."""
        UpdateLog.objects.create(
            commit_date="2026-07-01", description="Скрытый: отклонённый коммит",
            author="whmi2", commit_hash="xyz1", hidden=True,
        )
        response = self._call_view(self._make_request(self.superuser))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "test feature")  # видимая запись на месте
        self.assertNotContains(response, "отклонённый коммит")
        # 3 обычных записи, скрытая не в счётчике.
        self.assertContains(response, "записей: 3")
        # И автора скрытой записи нет в дропдауне.
        self.assertNotContains(response, 'value="whmi2"')


class UpdateLogDateDedupeTests(UpdateLogAdminViewMixin, TestCase):
    """Дедупликация дат на «Обновлениях» (ТЗ 2026-10-19): несколько коммитов
    за один день — дата в колонке печатается ОДИН раз ({% ifchanged %} по
    commit_date; сортировка держит одинаковые даты подряд)."""

    def setUp(self):
        self.factory = RequestFactory()
        UpdateLog.objects.all().delete()
        self.superuser = get_user_model().objects.create_superuser(
            username="dedupe_admin", password="***", email="admin@test.com",
        )
        UpdateLog.objects.create(
            commit_date="2026-07-27", description="Нововведение: первый за день",
            author="whmi1", commit_hash="a1",
        )
        UpdateLog.objects.create(
            commit_date="2026-07-27", description="Исправление: второй за день",
            author="whmi1", commit_hash="a2",
        )
        UpdateLog.objects.create(
            commit_date="2026-07-27", description="правка: третий за день",
            author="Archi", commit_hash="a3",
        )
        UpdateLog.objects.create(
            commit_date="2026-06-22", description="fix build v2",
            author="ArchiBaldEgo", commit_hash="b1",
        )

    def test_same_day_date_rendered_once(self):
        response = self._call_view(self._make_request(self.superuser))
        self.assertEqual(response.status_code, 200)
        # Три записи дня 27.07 — дата в таблице только в первой из них.
        self.assertContains(response, "27.07.2026", count=1)
        self.assertContains(response, "22.06.2026", count=1)
        # Сами записи при этом все на месте (дата ≠ строка записи).
        self.assertContains(response, "первый за день")
        self.assertContains(response, "второй за день")
        self.assertContains(response, "третий за день")
        self.assertContains(response, "записей: 4")


class UpdateLogAdminActionTests(TestCase):
    """Действия UpdateLogAdmin: публикация скрытых и скрытие любых записей."""

    def test_publish_and_hide_actions_toggle_hidden(self):
        from ai.admin.models import UpdateLogAdmin
        from ai.admin.site import ai_admin_site

        UpdateLog.objects.all().delete()
        entry = UpdateLog.objects.create(
            commit_date="2026-10-01", description="feat: скрытый коммит",
            author="whmi2", commit_hash="h9", hidden=True,
        )
        admin = UpdateLogAdmin(UpdateLog, ai_admin_site)
        qs = UpdateLog.objects.filter(pk=entry.pk)

        admin.publish_selected(request=MagicMock(), queryset=qs)
        entry.refresh_from_db()
        self.assertFalse(entry.hidden)

        admin.hide_selected(request=MagicMock(), queryset=qs)
        entry.refresh_from_db()
        self.assertTrue(entry.hidden)


class AuthorAliasTests(UpdateLogAdminViewMixin, TestCase):
    """Справочник AuthorAlias: метки, display_map и рендер на «Обновления»."""

    def setUp(self):
        self.factory = RequestFactory()
        UpdateLog.objects.all().delete()
        AuthorAlias.objects.all().delete()
        self.superuser = get_user_model().objects.create_superuser(
            username="alias_admin", password="***", email="alias@test.com",
        )
        UpdateLog.objects.create(
            commit_date="2026-07-27", description="Нововведение: test feature",
            author="whmi1", commit_hash="abc123",
        )
        UpdateLog.objects.create(
            commit_date="2026-07-11", description="Исправление: bug fix",
            author="ArchiBaldEgo", commit_hash="def456",
        )
        self.alias = AuthorAlias.objects.create(
            nick="whmi1", full_name="Иванов Иван", group="11А",
        )

    def test_display_label_with_and_without_group(self):
        with_group = AuthorAlias.objects.get(nick="whmi1")
        without = AuthorAlias.objects.create(nick="petr", full_name="Петров Пётр")
        self.assertEqual(with_group.display_label, "Иванов Иван (11А)")
        self.assertEqual(without.display_label, "Петров Пётр")

    def test_display_map_maps_nick_to_label(self):
        AuthorAlias.objects.create(nick="sidor", full_name="Сидоров Сидор")
        mapping = AuthorAlias.display_map()
        self.assertEqual(mapping["whmi1"], "Иванов Иван (11А)")
        self.assertEqual(mapping["sidor"], "Сидоров Сидор")

    def test_updates_view_shows_full_name(self):
        """Ячейка «Автор» — ФИО (группа); ник без записи в справочнике остаётся ником."""
        response = self._call_view(self._make_request(self.superuser))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Иванов Иван (11А)")
        self.assertNotContains(response, ">whmi1</td>")
        # Ник без записи в справочнике — рендерится как есть.
        self.assertContains(response, "ArchiBaldEgo")

    def test_updates_view_search_by_full_name(self):
        """Поиск по ФИО находит коммиты ника из справочника."""
        response = self._call_view(self._make_request(self.superuser, {"q": "Иванов"}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "test feature")
        self.assertContains(response, "записей: 1")
        self.assertNotContains(response, "bug fix")

    def test_updates_view_search_by_group(self):
        """Поиск по группе тоже матчится через справочник."""
        response = self._call_view(self._make_request(self.superuser, {"q": "11А"}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "записей: 1")

    def test_updates_view_dropdown_shows_alias_labels(self):
        """Дропдаун авторов: value — сырой ник, label — ФИО из справочника."""
        response = self._call_view(self._make_request(self.superuser))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'value="whmi1"')
        self.assertContains(response, "Иванов Иван (11А)")


class SyncUpdateLogNoMergesTests(TestCase):
    """sync_update_log ходит git-логом с --no-merges (merge-коммиты не импортируются)."""

    def test_git_argv_includes_no_merges(self):
        from ai.management.commands import sync_update_log as cmd_mod

        with patch.object(cmd_mod.subprocess, "run") as run, patch.object(
            cmd_mod.sys, "stderr", new=MagicMock()
        ):
            run.return_value = SimpleNamespace(
                stdout="a1|2026-10-01|whmi1|feat: note",
                stderr="",
            )
            cmd_mod.Command().handle(rebuild=False)

        argv = run.call_args.args[0]
        self.assertIn("--no-merges", argv)
        # Обычный коммит из вывода всё же импортируется (парсинг жив).
        self.assertTrue(UpdateLog.objects.filter(commit_hash="a1").exists())


class SyncUpdateLogInteractiveTests(TestCase):
    """Интерактивный отбор коммитов: --interactive, hidden-строки, дедуп, no-TTY."""

    def setUp(self):
        UpdateLog.objects.all().delete()
        import importlib
        self.cmd_mod = importlib.import_module("ai.management.commands.sync_update_log")

    GIT_OUTPUT = (
        "h1|2026-10-05|whmi1|feat: add smoke test\n"
        "h2|2026-10-04|Archi|fix: small patch\n"
        "h3|2026-10-03|Kirill Karlow|docs: update readme\n"
    )

    def _run_handle(self, answers, interactive=True, isatty=True):
        """Прогон handle с подменённым git-выводом, isatty и вводом.

        answers — один ответ или список (для проверки повторного запроса).
        """
        if isinstance(answers, str):
            answers = [answers]
        with patch.object(self.cmd_mod.subprocess, "run", return_value=SimpleNamespace(
            stdout=self.GIT_OUTPUT, stderr="",
        )), patch.object(self.cmd_mod.sys.stdin, "isatty", return_value=isatty):
            with patch("builtins.input", side_effect=answers) as input_mock:
                self.cmd_mod.Command().handle(interactive=interactive, rebuild=False)
        return input_mock

    def test_parse_selection(self):
        parse = self.cmd_mod._parse_selection
        self.assertEqual(parse("", 3), set())
        self.assertEqual(parse("   ", 3), set())
        self.assertEqual(parse("a", 3), {1, 2, 3})
        self.assertEqual(parse("все", 3), {1, 2, 3})
        self.assertEqual(parse("2", 3), {2})
        self.assertEqual(parse("1,3", 3), {1, 3})
        self.assertEqual(parse("1,3-5", 7), {1, 3, 4, 5})
        self.assertEqual(parse(" 2, 4-5 ", 5), {2, 4, 5})
        # Мусор и вневыборочные номера → None (повторный запрос).
        self.assertIsNone(parse("x", 3))
        self.assertIsNone(parse("0", 3))
        self.assertIsNone(parse("4", 3))
        self.assertIsNone(parse("3-1", 5))
        # Пустые токены между запятыми терпимо пропускаются.
        self.assertEqual(parse("1,,2", 3), {1, 2})

    def test_interactive_decline_all_hides_all(self):
        self._run_handle("")
        self.assertEqual(UpdateLog.objects.count(), 3)
        self.assertEqual(UpdateLog.objects.filter(hidden=False).count(), 0)

    def test_interactive_answer_a_adds_all(self):
        self._run_handle("a")
        self.assertEqual(UpdateLog.objects.filter(hidden=False).count(), 3)
        self.assertEqual(UpdateLog.objects.filter(hidden=True).count(), 0)

    def test_interactive_partial_selection(self):
        self._run_handle("1,3")
        self.assertEqual(
            set(UpdateLog.objects.filter(hidden=False).values_list("commit_hash", flat=True)),
            {"h1", "h3"},
        )
        self.assertEqual(
            set(UpdateLog.objects.filter(hidden=True).values_list("commit_hash", flat=True)),
            {"h2"},
        )
        # Перевод префиксов жив: h1 — «Нововведение: …»
        self.assertTrue(
            UpdateLog.objects.filter(commit_hash="h1", description__startswith="Нововведение:").exists()
        )

    def test_interactive_invalid_answer_reprompts(self):
        self._run_handle(["что", "2"])
        self.assertEqual(
            set(UpdateLog.objects.filter(hidden=False).values_list("commit_hash", flat=True)),
            {"h2"},
        )
        self.assertEqual(UpdateLog.objects.filter(hidden=True).count(), 2)

    def test_interactive_without_tty_writes_nothing(self):
        self._run_handle("a", isatty=False)
        self.assertEqual(UpdateLog.objects.count(), 0)

    def test_dedup_covers_hidden_rows(self):
        self._run_handle("")
        self.assertEqual(UpdateLog.objects.count(), 3)
        # Повторный синк с тем же git-выводом: hidden-строки дедупятся —
        # новых вопросов и дублей нет.
        self._run_handle("a")
        self.assertEqual(UpdateLog.objects.count(), 3)
        self.assertEqual(UpdateLog.objects.filter(hidden=False).count(), 0)

    def test_auto_mode_imports_visible(self):
        self._run_handle([], interactive=False)  # input не должен вызываться
        self.assertEqual(UpdateLog.objects.filter(hidden=False).count(), 3)


class UpdateLogModelTests(TestCase):
    """Tests for the UpdateLog model itself."""

    def setUp(self):
        UpdateLog.objects.all().delete()

    def test_ordering_newest_first(self):
        """UpdateLog should be ordered by commit_date descending."""
        UpdateLog.objects.create(
            commit_date="2026-06-01", description="old", author="A", commit_hash="old1",
        )
        UpdateLog.objects.create(
            commit_date="2026-07-28", description="new", author="B", commit_hash="new1",
        )
        first = UpdateLog.objects.first()
        self.assertEqual(first.commit_hash, "new1")

    def test_str_representation(self):
        """__str__ should contain date, author, and truncated description."""
        entry = UpdateLog.objects.create(
            commit_date="2026-07-28", description="A" * 100, author="whmi1", commit_hash="h1",
        )
        s = str(entry)
        self.assertIn("2026-07-28", s)
        self.assertIn("whmi1", s)
        # Description truncated to 80 chars in __str__
        self.assertIn("A" * 80, s)
        self.assertNotIn("A" * 81, s)


class LastUpdateDateCacheTests(TestCase):
    """Tests for _get_last_update_date with caching."""

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        UpdateLog.objects.all().delete()

    def test_returns_none_when_empty(self):
        """No UpdateLog records → None."""
        from ai.views import _get_last_update_date
        result = _get_last_update_date()
        self.assertIsNone(result)

    def test_returns_date_when_populated(self):
        """Most recent commit_date should be returned."""
        from ai.views import _get_last_update_date
        UpdateLog.objects.create(
            commit_date="2026-07-28", description="test", author="A", commit_hash="h1",
        )
        UpdateLog.objects.create(
            commit_date="2026-06-01", description="old", author="B", commit_hash="h2",
        )
        result = _get_last_update_date()
        self.assertEqual(str(result), "2026-07-28")

    def test_caches_result(self):
        """Second call should use cache (no DB hit).
        With post_save/post_delete signals, cache is invalidated on change,
        so we test caching without deleting records (signals clear cache).
        """
        from ai.views import _get_last_update_date
        from django.core.cache import cache
        cache.clear()
        UpdateLog.objects.create(
            commit_date="2026-07-28", description="test", author="A", commit_hash="h1",
        )
        # First call populates cache (post_save signal clears it, then this sets it)
        result1 = _get_last_update_date()
        self.assertEqual(str(result1), "2026-07-28")
        # Second call should return cached value without DB hit
        result2 = _get_last_update_date()
        self.assertEqual(str(result2), "2026-07-28")
        # Clean up
        cache.clear()


class AvailableModelOptionsCacheTests(TestCase):
    """Tests for get_available_model_options caching + AIModelAvailability signal.

    Непустой результат кешируется на 30 c; пустой — нет (чтобы self-heal в
    _render_ai_page продолжал срабатывать). Инвалидация — сигналом
    post_save/post_delete на AIModelAvailability.
    """

    def setUp(self):
        from django.core.cache import cache
        from ai.models import AIModelAvailability
        cache.clear()
        AIModelAvailability.objects.all().delete()

    def _make_row(self, key, window_date):
        from ai.models import AIModelAvailability
        return AIModelAvailability.objects.create(
            model_key=key, model_title=key, is_available=True,
            window_date=window_date, last_message="ok",
        )

    def test_caches_nonempty_result(self):
        from ai.model_health import (
            get_available_model_options, get_health_window_date, MODEL_CATALOG_KEYS,
        )
        from django.core.cache import cache
        if not MODEL_CATALOG_KEYS:
            self.skipTest("No models in registry")
        window_date = get_health_window_date()
        key = MODEL_CATALOG_KEYS[0]
        self._make_row(key, window_date)
        cache.clear()  # create triggered the invalidation signal; be explicit

        result1 = get_available_model_options()
        self.assertEqual([m["key"] for m in result1], [key])

        # Second call served from cache → no DB queries.
        with self.assertNumQueries(0):
            result2 = get_available_model_options()
        self.assertEqual(result2, result1)

    def test_empty_result_not_cached(self):
        """Empty window → [] is NOT cached, so self-heal in _render_ai_page still fires."""
        from ai.model_health import get_available_model_options
        from ai.constants import AI_CACHE_KEY_PREFIX
        from django.core.cache import cache
        cache.clear()
        result = get_available_model_options()
        self.assertEqual(result, [])
        self.assertIsNone(cache.get(f"{AI_CACHE_KEY_PREFIX}:available_models"))

    def test_signal_invalidates_on_new_row(self):
        from ai.model_health import (
            get_available_model_options, get_health_window_date, MODEL_CATALOG_KEYS,
        )
        from django.core.cache import cache
        if len(MODEL_CATALOG_KEYS) < 2:
            self.skipTest("Need at least 2 models in registry")
        window_date = get_health_window_date()
        key_a, key_b = MODEL_CATALOG_KEYS[0], MODEL_CATALOG_KEYS[1]
        self._make_row(key_a, window_date)
        cache.clear()
        result1 = get_available_model_options()
        self.assertEqual([m["key"] for m in result1], [key_a])

        # Adding a second available model triggers post_save → cache invalidated.
        self._make_row(key_b, window_date)
        result2 = get_available_model_options()
        self.assertEqual({m["key"] for m in result2}, {key_a, key_b})


# ===================================================================
# Tests for Ollama provider (cloud models, plain chat, no tools)
# ===================================================================

class OllamaRegistryTests(SimpleTestCase):
    """Ollama-модели зарегистрированы в registry с правильными capabilities."""

    OLLAMA_KEYS = [
        "Ollama_Glm_5_3_Flash_Cloud",
        "Ollama_Glm_5_2_Cloud",
        "Ollama_DeepSeek_V4_1_Flash_Cloud",
        "Ollama_DeepSeek_V4_Pro_Cloud",
        "Ollama_Gemma_4_Cloud",
        "Ollama_Nemotron_3_Super_Cloud",
        "Ollama_Kimi_K2_7_Code_Cloud",
        "Ollama_Kimi_K2_6_Cloud",
        "Ollama_Gpt_Oss_20B_Cloud",
        "Ollama_Gpt_Oss_120B_Cloud",
        "Ollama_MiniMax_M3_1_Flash_Cloud",
    ]

    # gpt-oss:cloud (harmony) и minimax-m3.1-flash:cloud стримят отдельное поле
    # thinking → reasoning-модели.
    REASONING_KEYS = {
        "Ollama_Gpt_Oss_20B_Cloud",
        "Ollama_Gpt_Oss_120B_Cloud",
        "Ollama_MiniMax_M3_1_Flash_Cloud",
    }

    def _expected_caps(self, key):
        return {"text": True, "vision": False, "reasoning": key in self.REASONING_KEYS}

    def test_registry_contains_ollama_models(self):
        from ai.model_clients import registry, ollama
        for key in self.OLLAMA_KEYS:
            self.assertIsNotNone(registry.get(key), f"Missing registry entry for {key}")
            self.assertTrue(callable(registry.handler(key)), f"No handler for {key}")
            self.assertEqual(
                registry.capabilities(key),
                self._expected_caps(key),
                f"Wrong capabilities for {key}",
            )
            self.assertTrue(
                callable(getattr(ollama, f"ask_{key}_async", None)),
                f"Missing ollama.ask_{key}_async",
            )

    def test_ollama_table_and_registry_in_sync(self):
        """Каждая модель OLLAMA_MODELS есть в registry, и наоборот — без рассинхрона."""
        from ai.model_clients import registry, ollama
        table_keys = set(ollama.OLLAMA_MODELS)
        for key in self.OLLAMA_KEYS:
            self.assertIn(key, table_keys, f"{key} missing from OLLAMA_MODELS table")
        for key in table_keys:
            self.assertIsNotNone(
                registry.get(key),
                f"OLLAMA_MODELS entry {key} is not registered in the model registry",
            )

    def test_ollama_models_table_matches_registry(self):
        from ai.model_clients import registry, ollama
        for key in self.OLLAMA_KEYS:
            self.assertIn(key, ollama.OLLAMA_MODELS)
            self.assertEqual(registry.handler(key), getattr(ollama, f"ask_{key}_async"))

    def test_module_defines_logger(self):
        import logging as _logging
        from ai.model_clients import ollama
        self.assertTrue(hasattr(ollama, "logger"))
        self.assertIsInstance(ollama.logger, _logging.Logger)


class OllamaHandlerTests(SimpleTestCase):
    """Handler вызывает ollama.Client.chat (СТРИМОМ) и возвращает (content, tokens, is_error).

    Стриминг обязателен: без него длинные генерации ARM-solve минутами ждут
    готовый ответ, и шлюз ollama.com рвёт соединение — httpx
    ServerDisconnectedError («Server disconnected without sending a response»).
    """

    @staticmethod
    def _stream(*pieces, eval_count=0):
        """Фейковые чанки стрима: контент по кускам, eval_count и done_reason
        в финальном чанке (contract _stream_once — 4-кортеж)."""
        from types import SimpleNamespace
        chunks = []
        for piece in pieces:
            chunks.append(SimpleNamespace(
                message=SimpleNamespace(content=piece), eval_count=0))
        chunks.append(SimpleNamespace(
            message=SimpleNamespace(content=""), eval_count=eval_count,
            done_reason="stop"))
        return iter(chunks)

    async def test_handler_streams_and_accumulates(self):
        from ai.model_clients import ollama
        with patch("ai.model_clients.ollama.OLLAMA_API_KEY", "test-key"), \
             patch("ai.model_clients.ollama.OLLAMA_HOST", "https://ollama.com"), \
             patch("ai.model_clients.ollama.Client") as mock_client_cls:
            mock_client = mock_client_cls.return_value
            # Контент приходит кусками — handler обязан собрать его в строку.
            mock_client.chat.return_value = self._stream("print", "(1", ")", eval_count=5)

            # Хендлеры генерируются динамически (make_table_handlers) — getattr,
            # чтобы линтер не ругался на отсутствующий атрибут.
            glm_5_2 = getattr(ollama, "ask_Ollama_Glm_5_2_Cloud_async")
            result = await glm_5_2("1+1=?", "client")
            self.assertEqual(result, ("print(1)", 5, False, False, 0))
            mock_client.chat.assert_called_once()
            # Без tools= (обычный чат), но СТРИМОМ (stream=True) — иначе
            # ollama.com рвёт соединение на длинных генерациях.
            _, kwargs = mock_client.chat.call_args
            self.assertNotIn("tools", kwargs)
            self.assertIs(kwargs.get("stream"), True)

    async def test_deepseek_v4_1_flash_uses_cloud_model_id(self):
        """DeepSeek 4.1 Flash ходит через ollama.chat с model='deepseek-v4.1-flash:cloud'."""
        from ai.model_clients import ollama
        with patch("ai.model_clients.ollama.OLLAMA_API_KEY", "test-key"), \
             patch("ai.model_clients.ollama.OLLAMA_HOST", "https://ollama.com"), \
             patch("ai.model_clients.ollama.Client") as mock_client_cls:
            mock_client = mock_client_cls.return_value
            mock_client.chat.return_value = self._stream("ok", eval_count=1)

            deepseek_flash = getattr(ollama, "ask_Ollama_DeepSeek_V4_1_Flash_Cloud_async")
            result = await deepseek_flash("hi", "client")
            self.assertEqual(result, ("ok", 1, False, False, 0))
            _, kwargs = mock_client.chat.call_args
            self.assertEqual(kwargs.get("model"), "deepseek-v4.1-flash:cloud")

    async def test_gpt_oss_cloud_model_ids(self):
        """gpt-oss ходит через ollama.chat с model='gpt-oss:<size>b-cloud'."""
        from ai.model_clients import ollama
        for key, model_id in (
            ("Ollama_Gpt_Oss_20B_Cloud", "gpt-oss:20b-cloud"),
            ("Ollama_Gpt_Oss_120B_Cloud", "gpt-oss:120b-cloud"),
        ):
            with self.subTest(key=key):
                with patch("ai.model_clients.ollama.OLLAMA_API_KEY", "test-key"), \
                     patch("ai.model_clients.ollama.OLLAMA_HOST", "https://ollama.com"), \
                     patch("ai.model_clients.ollama.Client") as mock_client_cls:
                    mock_client = mock_client_cls.return_value
                    mock_client.chat.return_value = self._stream("ok", eval_count=1)

                    handler = getattr(ollama, f"ask_{key}_async")
                    result = await handler("hi", "client")
                    self.assertEqual(result, ("ok", 1, False, False, 0))
                    _, kwargs = mock_client.chat.call_args
                    self.assertEqual(kwargs.get("model"), model_id)

    async def test_cloud_guard_without_api_key(self):
        """Cloud host + пустой OLLAMA_API_KEY → guard-сообщение, без вызова Client."""
        from ai.model_clients import ollama
        with patch("ai.model_clients.ollama.OLLAMA_API_KEY", ""), \
             patch("ai.model_clients.ollama.OLLAMA_HOST", "https://ollama.com"), \
             patch("ai.model_clients.ollama.Client") as mock_client_cls:
            gemma_4 = getattr(ollama, "ask_Ollama_Gemma_4_Cloud_async")
            result = await gemma_4("hi", "client")
            self.assertIn("Ollama API ключ не настроен", result[0])
            mock_client_cls.assert_not_called()

    @staticmethod
    def _thinking_stream(*thinking_pieces, eval_count=0):
        """Чанки с пустым content и текстом в отдельном поле thinking."""
        from types import SimpleNamespace
        chunks = []
        for piece in thinking_pieces:
            chunks.append(SimpleNamespace(
                message=SimpleNamespace(content="", thinking=piece), eval_count=0))
        chunks.append(SimpleNamespace(
            message=SimpleNamespace(content="", thinking=""), eval_count=eval_count))
        return iter(chunks)

    async def test_empty_content_falls_back_to_thinking(self):
        """gpt-oss:cloud кладёт ответ в поле thinking при пустом content —
        отдаём его пользователю, а не «пустой ответ»."""
        from ai.model_clients import ollama
        with patch("ai.model_clients.ollama.OLLAMA_API_KEY", "test-key"), \
             patch("ai.model_clients.ollama.OLLAMA_HOST", "https://ollama.com"), \
             patch("ai.model_clients.ollama.Client") as mock_client_cls:
            mock_client = mock_client_cls.return_value
            mock_client.chat.return_value = self._thinking_stream(
                "Рассуждаю", " и отвечаю", eval_count=7)

            gpt_oss = getattr(ollama, "ask_Ollama_Gpt_Oss_20B_Cloud_async")
            result = await gpt_oss("hi", "client")
            # thinking-фолбэк: done_reason нет в чанках → не «length» → не обрез
            self.assertEqual(result, ("Рассуждаю и отвечаю", 7, False, False, 0))

    async def test_empty_content_and_thinking_returns_error(self):
        """Пустые и content, и thinking → прежняя ошибка «пустой ответ»."""
        from ai.model_clients import ollama
        with patch("ai.model_clients.ollama.OLLAMA_API_KEY", "test-key"), \
             patch("ai.model_clients.ollama.OLLAMA_HOST", "https://ollama.com"), \
             patch("ai.model_clients.ollama.Client") as mock_client_cls:
            mock_client = mock_client_cls.return_value
            mock_client.chat.return_value = self._stream("", eval_count=0)

            glm_5_2 = getattr(ollama, "ask_Ollama_Glm_5_2_Cloud_async")
            content, tokens, is_error, cut, _prompt = await glm_5_2("hi", "client")
            self.assertTrue(is_error)
            self.assertEqual(tokens, 0)
            self.assertFalse(cut)
            self.assertIn("пустой ответ", content)


class OllamaRateLimitTests(SimpleTestCase):
    """Защита от 429 Ollama Cloud: per-process семафор + ретраи с backoff.

    Ollama Cloud лимитирует одновременные запросы по тарифу (не rpm) —
    превышение отдаёт 429 (ollama.ResponseError со status_code=429).
    """

    def setUp(self):
        from ai.model_clients import ollama
        ollama._reset_limiter()

    def tearDown(self):
        from ai.model_clients import ollama
        ollama._reset_limiter()

    @staticmethod
    def _stream(*pieces, eval_count=0):
        """Фейковые чанки стрима — как в OllamaHandlerTests (тот же формат чанков)."""
        chunks = []
        for piece in pieces:
            chunks.append(SimpleNamespace(
                message=SimpleNamespace(content=piece), eval_count=0))
        chunks.append(SimpleNamespace(
            message=SimpleNamespace(content=""), eval_count=eval_count,
            done_reason="stop"))
        return iter(chunks)

    @staticmethod
    def _429():
        from ollama import ResponseError
        return ResponseError("rate limit exceeded", status_code=429)

    async def test_429_retries_then_succeeds(self):
        """Первый вызов кидает 429, второй успешен — ретрай с паузой, без ошибки."""
        from ai.model_clients import ollama
        with patch.dict(os.environ, {"OLLAMA_MAX_RETRIES": "3"}), \
             patch("ai.model_clients.ollama.OLLAMA_API_KEY", "test-key"), \
             patch("ai.model_clients.ollama.OLLAMA_HOST", "https://api.ollama.com"), \
             patch("ai.model_clients.ollama._sleep",
                   new=AsyncMock(return_value=None)) as mock_sleep, \
             patch("ai.model_clients.ollama.Client") as mock_client_cls:
            mock_client = mock_client_cls.return_value
            mock_client.chat.side_effect = [
                self._429(),
                self._stream("ok", eval_count=3),
            ]
            glm_5_2 = getattr(ollama, "ask_Ollama_Glm_5_2_Cloud_async")
            result = await glm_5_2("hi", "client")
            self.assertEqual(result, ("ok", 3, False, False, 0))
            self.assertEqual(mock_client.chat.call_count, 2)
            self.assertEqual(mock_sleep.await_count, 1)
            delay = mock_sleep.await_args.args[0]
            self.assertGreater(delay, 0)
            self.assertLessEqual(delay, 30 * 1.2)

    async def test_429_exhausted_returns_friendly(self):
        """Все ретраи исчерпаны → friendly-сообщение map_http_error с растущими паузами."""
        from ai.model_clients import ollama
        from ai.model_clients.exceptions import map_http_error
        with patch.dict(os.environ, {"OLLAMA_MAX_RETRIES": "3"}), \
             patch("ai.model_clients.ollama.OLLAMA_API_KEY", "test-key"), \
             patch("ai.model_clients.ollama.OLLAMA_HOST", "https://api.ollama.com"), \
             patch("ai.model_clients.ollama._sleep",
                   new=AsyncMock(return_value=None)) as mock_sleep, \
             patch("ai.model_clients.ollama.Client") as mock_client_cls:
            mock_client = mock_client_cls.return_value
            mock_client.chat.side_effect = self._429()
            glm_5_2 = getattr(ollama, "ask_Ollama_Glm_5_2_Cloud_async")
            content, tokens, is_error, cut, _prompt = await glm_5_2("hi", "client")
            # 1 первая попытка + 3 ретрая
            self.assertEqual(mock_client.chat.call_count, 4)
            self.assertTrue(is_error)
            self.assertFalse(cut)
            self.assertEqual(tokens, 0)
            self.assertEqual(content, map_http_error(429, "ollama"))
            self.assertNotIn("status code", content)
            # Лестница пауз ~5 / ~15 / ~30 сек (джиттер ±20%).
            delays = [c.args[0] for c in mock_sleep.await_args_list]
            self.assertEqual(len(delays), 3)
            for actual, base in zip(delays, (5.0, 15.0, 30.0)):
                self.assertGreaterEqual(actual, base * 0.8)
                self.assertLessEqual(actual, base * 1.2)

    async def test_other_response_error_not_retried(self):
        """ResponseError с иным кодом (500) не ретраится — прежний текст ошибки."""
        from ai.model_clients import ollama
        from ollama import ResponseError
        with patch.dict(os.environ, {"OLLAMA_MAX_RETRIES": "3"}), \
             patch("ai.model_clients.ollama.OLLAMA_API_KEY", "test-key"), \
             patch("ai.model_clients.ollama.OLLAMA_HOST", "https://api.ollama.com"), \
             patch("ai.model_clients.ollama._sleep", new=AsyncMock(return_value=None)) as mock_sleep, \
             patch("ai.model_clients.ollama.Client") as mock_client_cls:
            mock_client = mock_client_cls.return_value
            mock_client.chat.side_effect = ResponseError("boom", status_code=500)
            glm_5_2 = getattr(ollama, "ask_Ollama_Glm_5_2_Cloud_async")
            content, tokens, is_error, cut, _prompt = await glm_5_2("hi", "client")
            self.assertEqual(mock_client.chat.call_count, 1)
            self.assertTrue(is_error)
            self.assertFalse(cut)
            self.assertIn("Ошибка Ollama API:", content)
            mock_sleep.assert_not_awaited()

    async def test_semaphore_limits_concurrency(self):
        """OLLAMA_MAX_CONCURRENCY=1 → вызовы строго по одному, очередь, не отказ."""
        import asyncio
        from ai.model_clients import ollama
        with patch.dict(os.environ, {"OLLAMA_MAX_CONCURRENCY": "1"}), \
             patch("ai.model_clients.ollama.OLLAMA_API_KEY", "test-key"), \
             patch("ai.model_clients.ollama.OLLAMA_HOST", "https://api.ollama.com"), \
             patch("ai.model_clients.ollama.Client") as mock_client_cls:
            mock_client = mock_client_cls.return_value
            active = {"now": 0, "max": 0}

            def fake_chat(**kwargs):
                active["now"] += 1
                active["max"] = max(active["max"], active["now"])
                time.sleep(0.02)
                active["now"] -= 1
                return self._stream("ok", eval_count=1)

            mock_client.chat.side_effect = fake_chat
            handler = getattr(ollama, "ask_Ollama_Glm_5_2_Cloud_async")
            results = await asyncio.gather(
                *(handler(f"msg{i}", "client") for i in range(4))
            )
            self.assertEqual(active["max"], 1)
            self.assertEqual(len(results), 4)
            self.assertTrue(all(r == ("ok", 1, False, False, 0) for r in results))


class ArmBatchResultsPartialTests(SimpleTestCase):
    """Частичный шаблон _ai_batch_results.html не должен утекать комментариями:
    Django вырезает {# #} только в одну строку — многострочный комментарий
    попадал на страницу /arm/solve/ как видимый текст внизу. Комментарий
    частичного шаблона обязан быть {% comment %}…{% endcomment %}."""

    def test_partial_comment_not_leaked(self):
        from django.template.loader import render_to_string
        html = render_to_string("admin/ai/_ai_batch_results.html")
        self.assertNotIn("{#", html)
        self.assertNotIn("Общий CSS", html)
        # Комментарий partial'а не должен попасть на страницу как видимый текст.
        self.assertNotIn("Общий partial", html)

    def test_batch_results_css_moved_to_static_file(self):
        """CSS карточки вынесен в static/admin/ai/batch_results/index.css."""
        from pathlib import Path
        path = (
            Path(__file__).resolve().parent.parent / "static" / "admin" / "ai"
            / "batch_results" / "index.css"
        )
        src = path.read_text(encoding="utf-8")
        self.assertIn(".arm-card", src)


class SendSolutionCourseIdTests(SimpleTestCase):
    """``send_solution_to_dl`` должна передавать обязательное по контракту REST
    API поле ``courseId`` в JSON-теле. Без него dl.gsu.by отдаёт 400 Bad Request
    (ручная отправка через веб-форму работает, т.к. курс уже в URL страницы)."""

    def test_body_includes_course_id_field(self):
        from ai import dl_api_client

        captured = {}

        class FakeResponse:
            status_code = 200
            content = b'{"queueId": 42, "message": "ok"}'

        def fake_dl_request(method, path, **kwargs):
            captured["method"] = method
            captured["path"] = path
            captured["json"] = kwargs.get("json")
            return FakeResponse()

        with patch("ai.dl_api_client._dl_request", fake_dl_request):
            data = dl_api_client.send_solution_to_dl(
                "SID-1", 2606747, "program a;", ".mpc", course_id=1450,
            )

        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["path"], "/restapi/send-solution")
        body = captured["json"]
        self.assertIsNotNone(body)
        self.assertEqual(body["sessionId"], "SID-1")
        self.assertEqual(body["nodeId"], 2606747)
        self.assertEqual(body["code"], "program a;")
        self.assertEqual(body["fileExtension"], ".mpc")
        # Ключевое требование контракта: courseId присутствует в теле.
        self.assertEqual(body["courseId"], 1450)
        self.assertEqual(data, {"queueId": 42, "message": "ok"})

    def test_default_course_id_is_zero(self):
        from ai import dl_api_client

        captured = {}

        class FakeResponse:
            status_code = 200
            content = b'{"queueId": 1, "message": "ok"}'

        def fake_dl_request(method, path, **kwargs):
            captured["json"] = kwargs.get("json")
            return FakeResponse()

        with patch("ai.dl_api_client._dl_request", fake_dl_request):
            dl_api_client.send_solution_to_dl("SID", 100, "code", ".cpp")

        # Обратная совместимость: без явного course_id поле всё равно есть (=0).
        self.assertEqual(captured["json"]["courseId"], 0)

    def test_body_has_no_undocumented_filename_fields(self):
        """Контракт send-solution — строго задокументированные поля: имя файла
        решения DL генерирует сама (api_solution_*), «экспериментальные»
        fileName/solutionFileName из тела убраны (прогоны с ними отдавали 400)."""
        from ai import dl_api_client

        captured = {}

        class FakeResponse:
            status_code = 200
            content = b'{"queueId": 42, "message": "ok"}'

        def fake_dl_request(method, path, **kwargs):
            captured["json"] = kwargs.get("json")
            return FakeResponse()

        with patch("ai.dl_api_client._dl_request", fake_dl_request):
            dl_api_client.send_solution_to_dl("SID", 9101, "program a;", ".pas")
        self.assertEqual(
            set(captured["json"]),
            {"sessionId", "nodeId", "code", "fileExtension", "courseId"},
        )

    def test_payload_context_includes_course_id(self):
        from ai.dl_api_client import _send_solution_payload_context

        ctx = _send_solution_payload_context(2606747, "code...", ".mpc", 1450)
        self.assertIn("nodeId=2606747", ctx)
        self.assertIn("courseId=1450", ctx)
        self.assertIn("fileExtension='.mpc'", ctx)


class EmptyResponseDetectionTests(SimpleTestCase):
    """Пустой ответ модели должен детектиться на уровне провайдера и
    возвращаться как 3-кортеж ``is_error=True`` — осмысленное сообщение вместо
    «успеха» с пустым текстом или плейсхолдером, обходящим нижестоящие гарды."""

    def test_extract_choice_content_returns_empty_not_placeholder(self):
        from ai.model_clients.exceptions import extract_choice_content

        self.assertEqual(extract_choice_content({"choices": [{"message": {"content": ""}}]}), "")
        self.assertEqual(extract_choice_content({"choices": [{"message": {}}]}), "")
        self.assertEqual(extract_choice_content({}), "")
        # Плейсхолдер «Пустой ответ от модели.» удалён — он обходил empty-гарды.
        for obj in (
            {"choices": [{"message": {"content": ""}}]},
            {"choices": [{"message": {}}]},
            {},
        ):
            self.assertNotIn("Пустой ответ", extract_choice_content(obj))

    def test_sambanova_empty_returns_error_3tuple(self):
        from ai.model_clients import sambanova

        class FakeResponse:
            status_code = 200
            text = '{"choices": [{"message": {"content": ""}}], "usage": {"completion_tokens": 0}}'

        with patch("ai.model_clients.sambanova.requests") as mock_req:
            mock_req.post.return_value = FakeResponse()
            mock_req.exceptions = __import__("requests").exceptions
            result = sambanova._ask_sambanova_model_async("hi", 0, "Model", max_tokens=8)
            content, tokens, is_error, _cut, _prompt = async_to_sync_runner(result)
        self.assertTrue(is_error)
        self.assertEqual(tokens, 0)
        self.assertIn("пустой ответ", content.lower())

    def test_sambanova_nonempty_returns_2tuple_success(self):
        from ai.model_clients import sambanova

        class FakeResponse:
            status_code = 200
            text = ('{"choices": [{"message": {"content": "42"}}], '
                    '"usage": {"completion_tokens": 3}}')

        with patch("ai.model_clients.sambanova.requests") as mock_req:
            mock_req.post.return_value = FakeResponse()
            mock_req.exceptions = __import__("requests").exceptions
            result = sambanova._ask_sambanova_model_async("hi", 0, "Model", max_tokens=8)
            content, tokens, _is_error, _cut, _prompt = async_to_sync_runner(result)
        self.assertEqual(content, "42")
        self.assertEqual(tokens, 3)

    def test_groq_empty_returns_error_3tuple(self):
        from ai.model_clients import groq

        class FakeResponse:
            status_code = 200
            text = '{"choices": [{"message": {"content": ""}}], "usage": {"total_tokens": 0}}'
            headers = {}

            def json(self):
                import json as _json
                return _json.loads(self.text)

        async def fake_post(url, **kw):
            return FakeResponse()

        with patch("ai.model_clients.groq.GROQ_TOKEN", "tok"), \
                patch("ai.model_clients.groq.httpx.AsyncClient") as mock_cls:
            mock_client = MagicMock()
            mock_client.post = fake_post
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_cls.return_value = mock_client
            result = groq._ask_groq("groq-model", "hi", 0, model_key="Groq_Test")
            content, tokens, is_error, _cut, _prompt = async_to_sync_runner(result)
        self.assertTrue(is_error)
        self.assertEqual(tokens, 0)
        self.assertIn("пустой ответ", content.lower())
        self.assertIn("groq-model", content)


class BatchSolveModeAndCancelModelTests(SimpleTestCase):
    """Модельные константы/поля для режима «Пакетное решение» (F4) и
    мгновенного прерывания (F2): режим лога, статус прогона, поле course_id."""

    def test_ai_request_log_has_batch_solve_mode(self):
        from ai.models import AIRequestLog
        self.assertEqual(AIRequestLog.MODE_BATCH_SOLVE, "batch_solve")
        choices = dict(AIRequestLog.MODE_CHOICES)
        self.assertIn("batch_solve", choices)
        self.assertEqual(choices["batch_solve"], "Пакетное решение")

    def test_ai_model_test_run_has_cancelled_status(self):
        from ai.models import AIModelTestRun
        self.assertEqual(AIModelTestRun.STATUS_CANCELLED, "cancelled")
        choices = dict(AIModelTestRun.STATUS_CHOICES)
        self.assertIn("cancelled", choices)

    def test_ai_model_test_run_has_course_id_field(self):
        from ai.models import AIModelTestRun
        field = AIModelTestRun._meta.get_field("course_id")
        self.assertTrue(field.null)
        self.assertTrue(field.blank)


class BatchLogSnapshotTests(TestCase):
    """Детал batch-лога в Журнале запросов (F4): ``_is_batch_solve_log``
    распознаёт batch-прогоны (новый mode=batch_solve и старый mode=solve+
    sentinel), ``_build_batch_log_snapshot`` собирает снапшот мини-таблицы
    (run_id, course_id, file_extension, node_ids, results, report) из БД."""

    def setUp(self):
        from ai.models import AIModelTestRun, AIModelTestResult, Task
        self.user = get_user_model().objects.create_user(username="snap", password="x")
        self.lang = ProgrammingLanguage.objects.create(language_name="Pascal")
        self.topic = Topic.objects.create(topic_name_ru="Линейные", programming_language=self.lang)
        self.t1 = Task.objects.create(
            node_id=5001, task_id=6001, name="A", statement="x",
            topic=self.topic, programming_language=self.lang, file_extension=".pas",
        )
        self.t2 = Task.objects.create(
            node_id=5002, task_id=6002, name="B", statement="y",
            topic=self.topic, programming_language=self.lang, file_extension=".pas",
        )
        self.run_id = "a" * 32  # 32-hex sentinel-совместимый
        self.test_run = AIModelTestRun.objects.create(
            run_id=self.run_id,
            run_type=AIModelTestRun.RUN_TYPE_BATCH,
            status=AIModelTestRun.STATUS_COMPLETED,
            course_id=1450,
        )
        for task, verdict in ((self.t1, "solved"), (self.t2, "failed")):
            AIModelTestResult.objects.create(
                run=self.test_run, task=task,
                model_key="FakeModel", model_title="FakeModel",
                status="ok", verdict=verdict,
                duration_seconds=1.0, tokens=10,
                short_response="r", raw_response="raw", code="code",
                dl_comment="c", dl_queue_id=1,
                file_extension_snapshot=".pas",
                topic_name_snapshot="Линейные", prog_lang_snapshot="Pascal",
            )

    def _make_log(self, mode, message):
        return AIRequestLog.objects.create(
            user=self.user, source="arm", mode=mode, message=message,
            status=AIRequestLog.STATUS_SUCCESS, sent_at=timezone.now(),
        )

    def test_is_batch_solve_log_recognizes_new_and_legacy(self):
        from ai.admin.request_logs import _is_batch_solve_log
        new_log = self._make_log("batch_solve", f"Batch solve run {self.run_id}")
        legacy_log = self._make_log("solve", f"Batch solve run {self.run_id}")
        plain_log = self._make_log("chat", "hello")
        self.assertTrue(_is_batch_solve_log(new_log))
        self.assertTrue(_is_batch_solve_log(legacy_log))
        self.assertFalse(_is_batch_solve_log(plain_log))

    def test_build_batch_log_snapshot_fields(self):
        from ai.admin.request_logs import _build_batch_log_snapshot
        log = self._make_log("batch_solve", f"Batch solve run {self.run_id}")
        snap = _build_batch_log_snapshot(log)
        self.assertIsNotNone(snap)
        self.assertEqual(snap["run_id"], self.run_id)
        self.assertEqual(snap["course_id"], 1450)
        self.assertEqual(snap["file_extension"], ".pas")
        self.assertEqual(sorted(snap["node_ids"]), [5001, 5002])
        self.assertEqual(len(snap["results"]), 2)
        # report считается из результатов: 1 решено, 1 не решено.
        self.assertEqual(snap["report"]["solved"], 1)
        self.assertEqual(snap["report"]["failed"], 1)

    def test_snapshot_none_when_run_missing(self):
        from ai.admin.request_logs import _build_batch_log_snapshot
        log = self._make_log("batch_solve", f"Batch solve run {'b' * 32}")
        self.assertIsNone(_build_batch_log_snapshot(log))


class ArmCancelImmediateDbFlipTests(TestCase):
    """F2: ``cancel_arm_run`` сразу переводит AIModelTestRun в STATUS_CANCELLED с
    finished_at, не дожидаясь in-flight вызова — UI может перестать поллить."""

    def test_cancel_flips_db_status_immediately(self):
        from ai import arm_runner
        from ai.models import AIModelTestRun

        run_id = "cancel-flip-1"
        run = AIModelTestRun.objects.create(
            run_id=run_id, run_type=AIModelTestRun.RUN_TYPE_BATCH,
            status=AIModelTestRun.STATUS_RUNNING,
        )
        # Эмулируем живой in-memory job (как делает start_batch_solve_run).
        arm_runner._jobs[run_id] = {
            "run_id": run_id, "status": "running", "cancel_requested": False,
            "cancelled": False, "results": [], "report": None,
        }
        try:
            arm_runner.cancel_arm_run(run_id)
        finally:
            arm_runner._jobs.pop(run_id, None)

        run.refresh_from_db()
        self.assertEqual(run.status, AIModelTestRun.STATUS_CANCELLED)
        self.assertIsNotNone(run.finished_at)
        self.assertEqual(run.error_message, "Прервано пользователем")

    def test_cancel_orphan_run_marks_cancelled(self):
        from ai import arm_runner
        from ai.models import AIModelTestRun

        run_id = "cancel-orphan-1"
        AIModelTestRun.objects.create(
            run_id=run_id, run_type=AIModelTestRun.RUN_TYPE_BATCH,
            status=AIModelTestRun.STATUS_RUNNING,
        )
        # В памяти job нет — orphan-ветка.
        self.assertNotIn(run_id, arm_runner._jobs)
        arm_runner.cancel_arm_run(run_id)
        run = AIModelTestRun.objects.get(run_id=run_id)
        self.assertEqual(run.status, AIModelTestRun.STATUS_CANCELLED)
        self.assertIsNotNone(run.finished_at)


class BatchLogStatusSemanticsTests(TestCase):
    """Статус batch-лога в журнале: Success строго если ВСЕ пары задача×модель
    решены; иначе Error с краткой причиной «Решено N из M пар». ``run_params``
    (снимок формы запуска) сохраняется в AIModelTestRun как есть."""

    def setUp(self):
        from ai.models import Task
        self.user = get_user_model().objects.create_user(username="batchstat", password="x")
        self.lang = ProgrammingLanguage.objects.create(language_name="Pascal")
        self.topic = Topic.objects.create(topic_name_ru="Линейные", programming_language=self.lang)
        self.t1 = Task.objects.create(
            node_id=3101, task_id=3201, name="A", statement="x",
            topic=self.topic, programming_language=self.lang, file_extension=".pas",
        )
        self.t2 = Task.objects.create(
            node_id=3102, task_id=3202, name="B", statement="y",
            topic=self.topic, programming_language=self.lang, file_extension=".pas",
        )

    def _run_batch(self, dl_mock, run_id, run_params=None):
        """Прогон batch-воркера синхронно (как BatchRunnerIntegrationTests)."""
        import time as _t
        from ai import arm_runner

        async def fake_handler(messages, conv_id):
            return ("program a; begin writeln(1); end.", 12)

        ordered_models = [{"key": "FakeModel", "title": "FakeModel", "handler": fake_handler}]
        node_ids = [self.t1.node_id, self.t2.node_id]
        now_ts = _t.time()
        arm_runner._jobs[run_id] = {
            "run_id": run_id, "run_type": "batch", "status": "running",
            "error_message": "", "total_models": 1,
            "total_pairs": 2, "completed_pairs": 0, "completed_models": 0,
            "current_model_key": "FakeModel", "current_model_title": "FakeModel",
            "current_task_node_id": "", "current_task_name": "",
            "results": [], "report": None,
            "created_at_ts": now_ts, "updated_at_ts": now_ts,
        }
        try:
            with patch("ai.arm_runner._test_solution_on_dl", dl_mock):
                arm_runner._run_batch_job_worker(
                    run_id, node_ids, ordered_models, self.user.id, "DLSID-1",
                    ui_language="Русский", dl_test=True, run_params=run_params,
                )
        finally:
            arm_runner._jobs.pop(run_id, None)
        return AIRequestLog.objects.get(message=f"Batch solve run {run_id}")

    @staticmethod
    def _dl(verdict):
        def dl_mock(sid, node_id, code, ext, **kw):
            return {
                "verdict": verdict if node_id == 3101 else "failed",
                "comment": "c", "submit_error": "", "queue_id": 1,
                "code_sent": code,
            }
        return dl_mock

    def test_mixed_run_logs_error(self):
        """1 решена из 2 пар → Error + «Решено 1 из 2 пар»."""
        log = self._run_batch(self._dl("solved"), "batch-status-mixed-1")
        self.assertEqual(log.status, AIRequestLog.STATUS_ERROR)
        self.assertIn("Решено 1 из 2 пар", log.error_message)

    def test_all_failed_logs_error(self):
        """Ни одна пара не решена → Error + «Все пары провалены»."""
        log = self._run_batch(self._dl("failed"), "batch-status-fail-1")
        self.assertEqual(log.status, AIRequestLog.STATUS_ERROR)
        self.assertIn("Все пары провалены", log.error_message)

    def test_all_solved_logs_success(self):
        """Все пары решены → Success, без сообщения об ошибке."""
        def dl_ok(sid, node_id, code, ext, **kw):
            return {
                "verdict": "solved", "comment": "ok", "submit_error": "",
                "queue_id": 1, "code_sent": code,
            }
        log = self._run_batch(dl_ok, "batch-status-ok-1")
        self.assertEqual(log.status, AIRequestLog.STATUS_SUCCESS)
        self.assertEqual(log.error_message, "")

    def test_run_params_persisted(self):
        """Снимок формы запуска пишется в AIModelTestRun.run_params как есть."""
        from ai.models import AIModelTestRun
        rp = {
            "node_ids": [3101, 3102], "model_keys": ["FakeModel"],
            "file_extension": ".pas", "prompt_id": "",
            "dl_test": True, "ui_language": "Русский", "course_id": 1450,
        }
        self._run_batch(self._dl("solved"), "batch-params-1", run_params=rp)
        run = AIModelTestRun.objects.get(run_id="batch-params-1")
        self.assertEqual(run.run_params, rp)


class BatchLogRowContextsTests(TestCase):
    """Bulk-контекст batch-строк списка журнала (_batch_log_row_contexts):
    course_id/задачи/расширение одним запросом, без N+1 по 50 строкам.
    Решённые задачи (есть solved-пара хотя бы у одной модели) в списке
    журнала не показываются — только нерешённые."""

    def setUp(self):
        from ai.models import AIModelTestRun, AIModelTestResult, Task
        self.user = get_user_model().objects.create_user(username="rowctx", password="x")
        self.lang = ProgrammingLanguage.objects.create(language_name="Pascal")
        self.topic = Topic.objects.create(topic_name_ru="Линейные", programming_language=self.lang)
        self.t1 = Task.objects.create(
            node_id=5001, task_id=6001, name="A", statement="x",
            topic=self.topic, programming_language=self.lang, file_extension=".pas",
        )
        self.t2 = Task.objects.create(
            node_id=5002, task_id=6002, name="B", statement="y",
            topic=self.topic, programming_language=self.lang, file_extension=".pas",
        )
        self.run_id = "c" * 32
        self.test_run = AIModelTestRun.objects.create(
            run_id=self.run_id,
            run_type=AIModelTestRun.RUN_TYPE_BATCH,
            status=AIModelTestRun.STATUS_COMPLETED,
            course_id=1450,
        )
        for task, verdict in ((self.t1, "solved"), (self.t2, "failed")):
            AIModelTestResult.objects.create(
                run=self.test_run, task=task,
                model_key="FakeModel", model_title="FakeModel",
                status="ok", verdict=verdict,
                duration_seconds=1.0, tokens=10,
                short_response="r", raw_response="raw", code="code",
                dl_comment="c", dl_queue_id=1,
                file_extension_snapshot=".pas",
                topic_name_snapshot="Линейные", prog_lang_snapshot="Pascal",
            )

    def _make_log(self, mode, message):
        return AIRequestLog.objects.create(
            user=self.user, source="arm", mode=mode, message=message,
            status=AIRequestLog.STATUS_SUCCESS, sent_at=timezone.now(),
        )

    def test_context_fields(self):
        """t1 решена (solved), t2 нет → в списке журнала только t2."""
        from ai.admin.request_logs import _batch_log_row_contexts
        log = self._make_log("batch_solve", f"Batch solve run {self.run_id}")
        ctx = _batch_log_row_contexts([log])
        self.assertIn(log.pk, ctx)
        c = ctx[log.pk]
        self.assertEqual(c["run_id"], self.run_id)
        self.assertEqual(c["course_id"], 1450)
        self.assertEqual(c["file_extension"], ".pas")
        self.assertEqual([t["node_id"] for t in c["tasks"]], [5002])
        self.assertEqual(c["tasks"][0]["name"], "B")
        self.assertEqual(c["tasks_preview"], "B (5002)")
        self.assertIn("B (5002)", c["tasks_title"])
        self.assertNotIn("A (5001)", c["tasks_title"])

    def test_all_solved_tasks_hidden(self):
        """Все задачи прогона решены → список задач в журнале пуст."""
        from ai.models import AIModelTestResult
        from ai.admin.request_logs import _batch_log_row_contexts
        AIModelTestResult.objects.create(
            run=self.test_run, task=self.t2,
            model_key="FakeModel2", model_title="FakeModel2",
            status="ok", verdict="solved",
            duration_seconds=1.0, tokens=10,
            short_response="r", raw_response="raw", code="code",
            dl_comment="c", dl_queue_id=1,
            file_extension_snapshot=".pas",
            topic_name_snapshot="Линейные", prog_lang_snapshot="Pascal",
        )
        log = self._make_log("batch_solve", f"Batch solve run {self.run_id}")
        c = _batch_log_row_contexts([log])[log.pk]
        self.assertEqual(c["tasks"], [])
        self.assertEqual(c["tasks_preview"], "—")
        self.assertEqual(c["tasks_title"], "")

    def test_legacy_log_included(self):
        """Старый batch-лог (mode=solve + sentinel) тоже получает контекст."""
        from ai.admin.request_logs import _batch_log_row_contexts
        log = self._make_log("solve", f"Batch solve run {self.run_id}")
        self.assertIn(log.pk, _batch_log_row_contexts([log]))

    def test_plain_log_ignored(self):
        from ai.admin.request_logs import _batch_log_row_contexts
        plain = self._make_log("chat", "hello")
        self.assertEqual(_batch_log_row_contexts([plain]), {})

    def test_missing_run_skipped(self):
        """run_id из сообщения отсутствует в БД → контекста нет."""
        from ai.admin.request_logs import _batch_log_row_contexts
        log = self._make_log("batch_solve", f"Batch solve run {'d' * 32}")
        self.assertEqual(_batch_log_row_contexts([log]), {})

    def test_finish_meta_completed_run(self):
        """Завершённый прогон → run_finished_at (МСК) и run_duration_display."""
        from datetime import timedelta

        from ai.admin.request_logs import _batch_log_row_contexts
        from ai.constants import MOSCOW_TZ
        from ai.models import AIModelTestRun
        finished = timezone.localtime(timezone.now(), MOSCOW_TZ).replace(microsecond=0)
        AIModelTestRun.objects.filter(pk=self.test_run.pk).update(
            started_at=finished - timedelta(seconds=45), finished_at=finished,
        )
        log = self._make_log("batch_solve", f"Batch solve run {self.run_id}")
        c = _batch_log_row_contexts([log])[log.pk]
        self.assertEqual(c["run_finished_at"], finished.strftime("%d.%m.%Y %H:%M:%S"))
        self.assertEqual(c["run_duration_display"], "45 с")

    def test_finish_meta_running_run_absent(self):
        """Незавершённый (running) прогон → пустые строки (шаблон — «—»)."""
        from ai.admin.request_logs import _batch_log_row_contexts
        from ai.models import AIModelTestRun
        AIModelTestRun.objects.filter(pk=self.test_run.pk).update(finished_at=None)
        log = self._make_log("batch_solve", f"Batch solve run {self.run_id}")
        c = _batch_log_row_contexts([log])[log.pk]
        self.assertEqual(c["run_finished_at"], "")
        self.assertEqual(c["run_duration_display"], "")

    def test_tasks_all_with_marks(self):
        """Полный список задач (2026-10): решённые + нерешённые с пометками
        по моделям и dl_url; счётчики solved/unsolved."""
        from ai.models import AIModelTestResult
        from ai.admin.request_logs import _batch_log_row_contexts
        AIModelTestResult.objects.create(
            run=self.test_run, task=self.t2,
            model_key="FakeModel2", model_title="FakeModel2",
            status="ok", verdict="failed",
            duration_seconds=1.0, tokens=10,
            short_response="r", raw_response="raw", code="code",
            dl_comment="c", dl_queue_id=1,
            file_extension_snapshot=".pas",
            topic_name_snapshot="Линейные", prog_lang_snapshot="Pascal",
        )
        log = self._make_log("batch_solve", f"Batch solve run {self.run_id}")
        c = _batch_log_row_contexts([log])[log.pk]
        tasks_all = c["tasks_all"]
        self.assertEqual([t["node_id"] for t in tasks_all], [5001, 5002])
        by_node = {t["node_id"]: t for t in tasks_all}
        self.assertTrue(by_node[5001]["solved"])
        self.assertEqual(by_node[5001]["solved_models"], ["FakeModel"])
        self.assertEqual(by_node[5001]["total_models"], ["FakeModel"])
        # Вторая модель на t2 пыталась (failed) — в total_models есть.
        self.assertFalse(by_node[5002]["solved"])
        self.assertEqual(by_node[5002]["total_models"], ["FakeModel", "FakeModel2"])
        self.assertEqual(by_node[5002]["solved_models"], [])
        self.assertEqual(c["tasks_total"], 2)
        self.assertEqual(c["tasks_solved_count"], 1)
        self.assertEqual(c["tasks_unsolved_count"], 1)
        self.assertIn("task.jsp?nid=5001", by_node[5001]["dl_url"])
        self.assertIn("cid=1450", by_node[5001]["dl_url"])

    def test_tasks_all_empty_run_prompts_absent(self):
        """Без промптов у пар → пустой список prompts (шаблон покажет «—»)."""
        from ai.admin.request_logs import _batch_log_row_contexts
        log = self._make_log("batch_solve", f"Batch solve run {self.run_id}")
        c = _batch_log_row_contexts([log])[log.pk]
        self.assertEqual(c["tasks_total"], 2)
        self.assertEqual(c["tasks_solved_count"], 1)
        self.assertEqual(c["prompts"], [])

    def test_prompts_distinct_ordered(self):
        """Фактически использованные промпты: distinct по prompt_id,
        insertion-порядок; дубли (две пары с одним промптом) не задваиваются."""
        from ai.models import AIModelTestResult
        from ai.admin.request_logs import _batch_log_row_contexts
        p1 = Prompt.objects.create(prompt_name_ru="Учитель", prompt_text_ru="t")
        p2 = Prompt.objects.create(prompt_name_ru="Наставник", prompt_text_ru="t")
        AIModelTestResult.objects.filter(run=self.test_run, task=self.t1).update(
            prompt_id=p1.id, prompt_name="Учитель",
        )
        AIModelTestResult.objects.filter(run=self.test_run, task=self.t2).update(
            prompt_id=p2.id, prompt_name="Наставник",
        )
        # Дубль: та же пара перезапущена — update_or_create перезаписал бы
        # снимок; эмулируем второй строкой с тем же prompt (легаси-NULL не
        # проходит фильтр).
        AIModelTestResult.objects.create(
            run=self.test_run, task=self.t2,
            model_key="FakeModel3", model_title="FakeModel3",
            status="ok", verdict="failed",
            duration_seconds=1.0, tokens=10,
            short_response="r", raw_response="raw", code="code",
            dl_comment="c", dl_queue_id=1,
            file_extension_snapshot=".pas",
            topic_name_snapshot="Линейные", prog_lang_snapshot="Pascal",
            prompt_id=p2.id, prompt_name="Наставник",
        )
        log = self._make_log("batch_solve", f"Batch solve run {self.run_id}")
        c = _batch_log_row_contexts([log])[log.pk]
        prompts = c["prompts"]
        self.assertEqual(
            sorted((p["id"], p["name"]) for p in prompts),
            [(p1.id, "Учитель"), (p2.id, "Наставник")],
        )


class RequestLogXlsxTests(TestCase):
    """XLSX-выгрузка результатов batch-прогона (матрица задача×модель,
    автоширина колонок): единый серверный генератор (ai/admin/export.py)
    для записи журнала и для /arm/solve/ по run_id."""

    def setUp(self):
        from ai.models import AIModelTestRun, AIModelTestResult, Task
        self.factory = RequestFactory()
        self.superuser = get_user_model().objects.create_superuser(
            username="csv_admin", password="x", email="a@t.com",
        )
        self.normal_user = get_user_model().objects.create_user(username="csv_user", password="x")
        self.lang = ProgrammingLanguage.objects.create(language_name="Pascal")
        self.topic = Topic.objects.create(topic_name_ru="Линейные", programming_language=self.lang)
        self.t1 = Task.objects.create(
            node_id=5101, task_id=6101, name="A", statement="x",
            topic=self.topic, programming_language=self.lang, file_extension=".pas",
        )
        self.run_id = "e" * 32
        self.test_run = AIModelTestRun.objects.create(
            run_id=self.run_id,
            run_type=AIModelTestRun.RUN_TYPE_BATCH,
            status=AIModelTestRun.STATUS_COMPLETED,
            course_id=1450,
        )
        for task, verdict in ((self.t1, "solved"), (self.t1, "failed")):
            AIModelTestResult.objects.create(
                run=self.test_run, task=task,
                model_key="M1" if verdict == "solved" else "M2",
                model_title="Model One" if verdict == "solved" else "Model Two",
                status="ok", verdict=verdict,
                duration_seconds=1.0, tokens=10,
                short_response="r", raw_response="raw", code="code",
                dl_comment="c", dl_queue_id=1,
                file_extension_snapshot=".pas",
                topic_name_snapshot="Линейные", prog_lang_snapshot="Pascal",
            )
        self.log = AIRequestLog.objects.create(
            user=self.superuser, source="arm", mode="batch_solve",
            message=f"Batch solve run {self.run_id}",
            status=AIRequestLog.STATUS_ERROR, sent_at=timezone.now(),
        )
        self.plain_log = AIRequestLog.objects.create(
            user=self.superuser, source="chat", mode="chat", message="hi",
            status=AIRequestLog.STATUS_SUCCESS, sent_at=timezone.now(),
        )

    def test_xlsx_matrix_and_auto_width(self):
        """Три листа: «Сводка» (матрица задача×модель с сокращёнными
        названиями + сводка по моделям), «Расшифровка моделей»
        (сокращение → полное название) и «Сбои по парам» (построчная
        причина сбоя — таксономия сбоев). Автоширина — по содержимому."""
        from io import BytesIO

        from openpyxl import load_workbook

        from ai.admin.export import _MIN_COL_WIDTH, build_arm_results_xlsx
        results = [
            {"task_node_id": 1001, "task_name": "Очень длинное название задачи для проверки ширины",
             "model_key": "M1", "model_title": "Model One", "verdict": "solved", "duration": 2.0},
            {"task_node_id": 1001, "task_name": "Очень длинное название задачи для проверки ширины",
             "model_key": "M2", "model_title": "Model Two", "verdict": "failed", "duration": 4.0},
            {"task_node_id": 1002, "task_name": "B",
             "model_key": "M1", "model_title": "Model One", "verdict": None, "duration": 6.0},
        ]
        payload = build_arm_results_xlsx(results)
        wb = load_workbook(BytesIO(payload))
        self.assertEqual(wb.sheetnames, ["Сводка", "Расшифровка моделей", "Сбои по парам"])

        ws = wb["Сводка"]
        rows = [[c.value for c in row] for row in ws.iter_rows()]
        # Лист смешанной ширины (матрица 4 колонки + сводка из 5): iter_rows
        # дополняет короткие строки None-паддингом — срезаем до ширины матрицы.
        # Пустые ячейки openpyxl сохраняет как None (не "").
        self.assertEqual(
            [row[:4] for row in rows[:3]],
            [
                ["Node ID", "Задача", "MO", "MT"],
                [1001, "Очень длинное название задачи для проверки ширины", "+", "-"],
                [1002, "B", "?", None],
            ],
        )
        # Пустая строка-разделитель + сводная таблица по моделям
        # (семантика _per_bucket: skipped/None-вердикт в total, avg по всем).
        self.assertEqual(rows[4], ["Модель", "% решено", "Среднее время, сек", "Токены", "Решено/всего"])
        self.assertEqual(rows[5], ["MO", 50.0, 4.0, 0, "1/2"])
        self.assertEqual(rows[6], ["MT", 0.0, 4.0, 0, "0/1"])

        # Автоширина: каждая колонка не уже минимума и подогнана под содержимое.
        widths = [dim.width for dim in ws.column_dimensions.values()]
        self.assertTrue(widths)
        self.assertTrue(all(w >= _MIN_COL_WIDTH for w in widths))
        # Колонка «Задача» (самое длинное значение) шире колонок вердиктов.
        task_col_width = ws.column_dimensions["B"].width
        verdict_col_width = ws.column_dimensions["C"].width
        self.assertGreater(task_col_width, verdict_col_width)

        ws_legend = wb["Расшифровка моделей"]
        legend_rows = [[c.value for c in row] for row in ws_legend.iter_rows()]
        self.assertEqual(
            legend_rows,
            [
                ["Сокращение", "Полное название", "Провайдер"],
                ["MO", "Model One", "Model"],
                ["MT", "Model Two", "Model"],
            ],
        )

    def test_xlsx_endpoint_superuser(self):
        from io import BytesIO

        from openpyxl import load_workbook

        from ai.admin.export import XLSX_CONTENT_TYPE
        from ai.admin.request_logs import admin_request_log_xlsx_view
        request = self.factory.get(f"/ai/admin/ai/request_logs/{self.log.id}/xlsx/")
        request.user = self.superuser
        response = admin_request_log_xlsx_view(request, self.log.id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], XLSX_CONTENT_TYPE)
        wb = load_workbook(BytesIO(response.content))
        ws = wb["Сводка"]
        rows = [[c.value for c in row] for row in ws.iter_rows()]
        self.assertIn("+", [c for row in rows for c in row if isinstance(c, str)])
        self.assertIn("Model One", [c for row in wb["Расшифровка моделей"].iter_rows(values_only=True) for c in row])
        self.assertTrue(
            response["Content-Disposition"].startswith(
                f'attachment; filename="arm_solve_results_{self.run_id}.xlsx'
            )
        )

    def test_xlsx_report_endpoint_by_run_id(self):
        """Выгрузка по run_id (кнопка на /arm/solve/): живой прогон и DB-fallback."""
        from io import BytesIO

        from openpyxl import load_workbook

        from ai.admin.arm import admin_arm_solve_report_xlsx_view
        request = self.factory.get(f"/ai/admin/arm/solve/report/{self.run_id}/xlsx/")
        request.user = self.superuser
        response = admin_arm_solve_report_xlsx_view(request, self.run_id)
        self.assertEqual(response.status_code, 200)
        wb = load_workbook(BytesIO(response.content))
        self.assertIn(
            "Model One",
            [c for row in wb["Расшифровка моделей"].iter_rows(values_only=True) for c in row],
        )
        # Неизвестный run_id → 404.
        request = self.factory.get("/ai/admin/arm/solve/report/ffff/xlsx/")
        request.user = self.superuser
        response = admin_arm_solve_report_xlsx_view(request, "ffff")
        self.assertEqual(response.status_code, 404)

    def test_xlsx_endpoint_non_batch_404(self):
        from ai.admin.request_logs import admin_request_log_xlsx_view
        request = self.factory.get(f"/ai/admin/ai/request_logs/{self.plain_log.id}/xlsx/")
        request.user = self.superuser
        response = admin_request_log_xlsx_view(request, self.plain_log.id)
        self.assertEqual(response.status_code, 404)

    def test_xlsx_endpoint_non_staff_403(self):
        from ai.admin.request_logs import admin_request_log_xlsx_view
        request = self.factory.get(f"/ai/admin/ai/request_logs/{self.log.id}/xlsx/")
        request.user = self.normal_user
        response = admin_request_log_xlsx_view(request, self.log.id)
        self.assertEqual(response.status_code, 403)

    def test_result_download_named_model_and_task(self):
        """Имя файла решения: <модель>_<ID узла задачи><расширение> (у одной
        задачи скачивают решения нескольких моделей — пара делает имя
        однозначной); у DL API поля имени файла нет — подписываем скачивание."""
        from ai.models import AIModelTestResult
        from ai.admin.arm import admin_arm_solve_result_download_view
        result = AIModelTestResult.objects.get(model_key="M1")
        request = self.factory.get(f"/ai/admin/arm/solve/result/{result.id}/download/")
        request.user = self.superuser
        response = admin_arm_solve_result_download_view(request, result.id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response["Content-Disposition"],
            'attachment; filename="Model_One_5101.pas"',
        )

    def test_result_download_title_fallbacks_and_403(self):
        """Пробелы/спецсимволы title → «_» (точки типа «K2.7» сохраняются);
        нет доступа → 403."""
        from ai.models import AIModelTestResult
        from ai.admin.arm import admin_arm_solve_result_download_view
        result = AIModelTestResult.objects.get(model_key="M2")
        result.model_title = "Model Two K2.7"
        result.save(update_fields=["model_title"])

        request = self.factory.get(f"/ai/admin/arm/solve/result/{result.id}/download/")
        request.user = self.superuser
        response = admin_arm_solve_result_download_view(request, result.id)
        self.assertEqual(
            response["Content-Disposition"],
            'attachment; filename="Model_Two_K2.7_5101.pas"',
        )

        request = self.factory.get(f"/ai/admin/arm/solve/result/{result.id}/download/")
        request.user = self.normal_user
        response = admin_arm_solve_result_download_view(request, result.id)
        self.assertEqual(response.status_code, 403)

    def test_result_download_without_task_falls_back_by_id(self):
        """task NULL (легаси-одиночные прогоны) — <модель>_result_<id>; пустые
        title и key — result_<id> без модели."""
        from ai.models import AIModelTestResult
        from ai.admin.arm import admin_arm_solve_result_download_view

        orphan = AIModelTestResult.objects.create(
            run=self.test_run, task=None, model_key="M9", model_title="Model Nine",
            status="ok", verdict="solved", duration_seconds=1.0, tokens=10,
            code="code", file_extension_snapshot=".pas",
        )
        request = self.factory.get(f"/ai/admin/arm/solve/result/{orphan.id}/download/")
        request.user = self.superuser
        response = admin_arm_solve_result_download_view(request, orphan.id)
        self.assertEqual(
            response["Content-Disposition"],
            f'attachment; filename="Model_Nine_result_{orphan.id}.pas"',
        )

        blank = AIModelTestResult.objects.create(
            run=self.test_run, task=None, model_key="", model_title="",
            status="ok", verdict="solved", duration_seconds=1.0, tokens=10,
            code="code", file_extension_snapshot=".pas",
        )
        request = self.factory.get(f"/ai/admin/arm/solve/result/{blank.id}/download/")
        request.user = self.superuser
        response = admin_arm_solve_result_download_view(request, blank.id)
        self.assertEqual(
            response["Content-Disposition"],
            f'attachment; filename="result_{blank.id}.pas"',
        )


class ArmLightSnapshotTests(TestCase):
    """Лёгкие снапшоты batch-прогона (lazy_body): страница /arm/solve/ и поллинг
    не таскают raw_response/code/short_response/dl_comment/dl_error (большой
    прогон — десятки МБ текста); тело подтягивается через
    /arm/solve/result/<id>/body/. Полный снапшот (XLSX) не тронут."""

    def setUp(self):
        from ai.models import AIModelTestRun, AIModelTestResult, Task
        self.factory = RequestFactory()
        self.superuser = get_user_model().objects.create_superuser(
            username="light_admin", password="x", email="a@t.com",
        )
        self.normal_user = get_user_model().objects.create_user(username="light_user", password="x")
        self.lang = ProgrammingLanguage.objects.create(language_name="Pascal")
        self.topic = Topic.objects.create(topic_name_ru="Линейные", programming_language=self.lang)
        self.t1 = Task.objects.create(
            node_id=6101, task_id=7101, name="Задача А", statement="x",
            topic=self.topic, programming_language=self.lang, file_extension=".pas",
        )
        self.run_id = "f" * 32
        self.test_run = AIModelTestRun.objects.create(
            run_id=self.run_id,
            run_type=AIModelTestRun.RUN_TYPE_BATCH,
            status=AIModelTestRun.STATUS_COMPLETED,
            course_id=1450,
        )
        self.result = AIModelTestResult.objects.create(
            run=self.test_run, task=self.t1,
            model_key="M1", model_title="Model One",
            status="ok", verdict="solved",
            duration_seconds=1.5, tokens=42,
            short_response="краткий", raw_response="полный ответ",
            code="begin end.", dl_comment="Все тесты успешно пройдены",
            dl_error="", dl_queue_id=7,
            file_extension_snapshot="", topic_name_snapshot="Линейные",
            prog_lang_snapshot="Pascal",
        )

    def test_db_light_results_exclude_heavy_body(self):
        """light=True: без тяжёлого тела, с флагом lazy_body и метаданными;
        расширение fallback-ится из задачи при пустом снимке."""
        from ai.arm_runner import _batch_results_from_db
        self.result.file_extension_snapshot = ""
        self.result.save(update_fields=["file_extension_snapshot"])

        light = _batch_results_from_db(self.test_run, light=True)
        self.assertEqual(len(light), 1)
        item = light[0]
        self.assertTrue(item["lazy_body"])
        for key in ("raw_response", "code", "short_response", "dl_comment", "dl_error"):
            self.assertNotIn(key, item)
        self.assertFalse(item["truncated"])
        self.assertEqual(item["result_id"], self.result.id)
        self.assertEqual(item["task_node_id"], 6101)
        self.assertEqual(item["task_name"], "Задача А")
        self.assertEqual(item["verdict"], "solved")
        self.assertEqual(item["file_extension"], ".pas")  # fallback из Task
        self.assertEqual(item["dl_queue_id"], 7)

        # Полный вариант по-прежнему тянет тело (XLSX-экспорт).
        full = _batch_results_from_db(self.test_run)
        self.assertEqual(full[0]["raw_response"], "полный ответ")
        self.assertEqual(full[0]["code"], "begin end.")
        self.assertNotIn("lazy_body", full[0])

    def test_snapshot_from_db_light_vs_full(self):
        """get_arm_run_snapshot(light_results=True) из БД: результаты лёгкие,
        отчёт построен; полный вариант содержит raw_response."""
        from ai.arm_runner import get_arm_run_snapshot

        light = get_arm_run_snapshot(self.run_id, light_results=True)
        self.assertEqual(light["run_type"], "batch")
        self.assertEqual(light["results"][0]["lazy_body"], True)
        self.assertNotIn("raw_response", light["results"][0])
        self.assertEqual(light["report"]["total_pairs"], 1)
        self.assertEqual(light["report"]["solved"], 1)

        full = get_arm_run_snapshot(self.run_id)
        self.assertEqual(full["results"][0]["raw_response"], "полный ответ")
        self.assertNotIn("lazy_body", full["results"][0])

    def test_live_job_light_snapshot_no_deepcopy_and_no_mutation(self):
        """Живой job: light-снапшот не содержит тело и НЕ мутирует сам job;
        полный снапшот (deepcopy) по-прежнему с телом."""
        from ai import arm_runner
        job = {
            "run_id": self.run_id, "run_type": "batch", "status": "running",
            "user_id": self.superuser.id, "error_message": "",
            "total_pairs": 1, "completed_pairs": 1,
            "results": [{
                "result_id": self.result.id, "task_node_id": 6101,
                "task_name": "Задача А", "model_key": "M1",
                "model_title": "Model One", "duration": 1.0, "tokens": 5,
                "short_response": "краткий", "status": "ok",
                "verdict": "solved", "raw_response": "полный ответ",
                "code": "begin end.", "dl_comment": "ок", "dl_error": "",
                "dl_queue_id": 3, "file_extension": ".pas",
                "topic_name": "Линейные", "prog_lang_name": "Pascal",
            }],
            "report": None, "run_params": {"node_ids": [6101]},
            "created_at_ts": 1.0, "updated_at_ts": 1.0,
        }
        arm_runner._jobs[self.run_id] = job
        try:
            light = arm_runner.get_arm_run_snapshot(self.run_id, light_results=True)
            self.assertNotIn("raw_response", light["results"][0])
            self.assertTrue(light["results"][0]["lazy_body"])
            self.assertEqual(light["run_params"], {"node_ids": [6101]})
            # job не пострадал: тело на месте, флаг не добавлен.
            self.assertEqual(job["results"][0]["raw_response"], "полный ответ")
            self.assertNotIn("lazy_body", job["results"][0])

            full = arm_runner.get_arm_run_snapshot(self.run_id)
            self.assertEqual(full["results"][0]["raw_response"], "полный ответ")
        finally:
            arm_runner._jobs.pop(self.run_id, None)

    def test_result_body_endpoint(self):
        """body-эндпоинт: raw_response/code/dl_comment записи; 404 неизвестного
        id; нет доступа — 403; POST не разрешён."""
        from ai.admin.arm import admin_arm_solve_result_body_view

        request = self.factory.get(f"/ai/admin/arm/solve/result/{self.result.id}/body/")
        request.user = self.superuser
        response = admin_arm_solve_result_body_view(request, self.result.id)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertTrue(data["ok"])
        self.assertEqual(data["result"]["raw_response"], "полный ответ")
        self.assertEqual(data["result"]["code"], "begin end.")
        self.assertEqual(data["result"]["dl_comment"], "Все тесты успешно пройдены")
        self.assertEqual(data["result"]["short_response"], "краткий")

        request = self.factory.get(f"/ai/admin/arm/solve/result/{self.result.id}/body/")
        request.user = self.normal_user
        response = admin_arm_solve_result_body_view(request, self.result.id)
        self.assertEqual(response.status_code, 403)

        request = self.factory.post(f"/ai/admin/arm/solve/result/{self.result.id}/body/")
        request.user = self.superuser
        response = admin_arm_solve_result_body_view(request, self.result.id)
        self.assertEqual(response.status_code, 405)

        request = self.factory.get("/ai/admin/arm/solve/result/999999/body/")
        request.user = self.superuser
        with self.assertRaises(Http404):
            admin_arm_solve_result_body_view(request, 999999)

    def test_result_body_endpoint_chunked(self):
        """Чанковая загрузка raw_response: окно offset/length, метаданные
        raw_total/raw_offset/raw_has_more/raw_next, флаг truncated.
        Без length — полный ответ (обратная совместимость)."""
        from ai.admin.arm import admin_arm_solve_result_body_view, _BODY_RAW_CHUNK_MAX

        self.result.raw_response = "полный ответ"  # 11 символов
        self.result.truncated = True
        self.result.save(update_fields=["raw_response", "truncated"])

        request = self.factory.get(
            f"/ai/admin/arm/solve/result/{self.result.id}/body/?offset=0&length=6"
        )
        request.user = self.superuser
        data = json.loads(admin_arm_solve_result_body_view(request, self.result.id).content)["result"]
        self.assertEqual(data["raw_response"], "полный")
        self.assertEqual(data["raw_offset"], 0)
        self.assertEqual(data["raw_next"], 6)
        self.assertTrue(data["raw_has_more"])
        self.assertEqual(data["raw_total"], 12)
        self.assertTrue(data["truncated"])

        request = self.factory.get(
            f"/ai/admin/arm/solve/result/{self.result.id}/body/?offset=6&length=6"
        )
        request.user = self.superuser
        data = json.loads(admin_arm_solve_result_body_view(request, self.result.id).content)["result"]
        self.assertEqual(data["raw_response"], " ответ")
        self.assertEqual(data["raw_offset"], 6)
        self.assertFalse(data["raw_has_more"])
        self.assertIsNone(data["raw_next"])

        # Остаток за пределами длины — пустое окно, ответ не падает.
        request = self.factory.get(
            f"/ai/admin/arm/solve/result/{self.result.id}/body/?offset=100&length=6"
        )
        request.user = self.superuser
        data = json.loads(admin_arm_solve_result_body_view(request, self.result.id).content)["result"]
        self.assertEqual(data["raw_response"], "")
        self.assertFalse(data["raw_has_more"])

        # Жёсткий потолок окна: length выше _BODY_RAW_CHUNK_MAX срезается.
        self.result.raw_response = "x" * (_BODY_RAW_CHUNK_MAX + 1)
        self.result.save(update_fields=["raw_response"])
        request = self.factory.get(
            f"/ai/admin/arm/solve/result/{self.result.id}/body/?offset=0&length={_BODY_RAW_CHUNK_MAX * 10}"
        )
        request.user = self.superuser
        data = json.loads(admin_arm_solve_result_body_view(request, self.result.id).content)["result"]
        self.assertEqual(len(data["raw_response"]), _BODY_RAW_CHUNK_MAX)
        self.assertTrue(data["raw_has_more"])
        # Мусорные параметры — целый режим без окна.
        request = self.factory.get(
            f"/ai/admin/arm/solve/result/{self.result.id}/body/?offset=abc&length=-5"
        )
        request.user = self.superuser
        data = json.loads(admin_arm_solve_result_body_view(request, self.result.id).content)["result"]
        self.assertEqual(len(data["raw_response"]), _BODY_RAW_CHUNK_MAX + 1)
        self.assertFalse(data["raw_has_more"])

    def test_status_view_returns_light_results(self):
        """Поллинг статуса /arm/solve/status/ отдаёт лёгкие результаты."""
        from ai.admin.arm import admin_arm_solve_status_view
        request = self.factory.get(f"/ai/admin/arm/solve/status/?run_id={self.run_id}")
        request.user = self.superuser
        response = admin_arm_solve_status_view(request)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertTrue(data["ok"])
        item = data["run"]["results"][0]
        self.assertTrue(item["lazy_body"])
        self.assertNotIn("raw_response", item)

    def test_batch_log_detail_snapshot_is_light(self):
        """Деталь журнала (_build_batch_log_snapshot): результаты лёгкие,
        node_ids/file_extension по-прежнему вычисляются."""
        from ai.models import AIRequestLog
        from ai.admin.request_logs import _build_batch_log_snapshot
        log = AIRequestLog.objects.create(
            user=self.superuser, source="arm", mode="batch_solve",
            message=f"Batch solve run {self.run_id}",
            status=AIRequestLog.STATUS_ERROR, sent_at=timezone.now(),
        )
        snapshot = _build_batch_log_snapshot(log)
        self.assertIsNotNone(snapshot)
        self.assertTrue(snapshot["results"][0]["lazy_body"])
        self.assertNotIn("raw_response", snapshot["results"][0])
        self.assertEqual(snapshot["node_ids"], [6101])
        self.assertEqual(snapshot["file_extension"], ".pas")
        self.assertEqual(snapshot["report"]["solved"], 1)

    def test_batch_log_detail_snapshot_running_run(self):
        """Деталь журнала для ЕЩЁ ИДУЩЕГО прогона: snapshot строится
        (раньше здесь падал NameError: time не импортирован в logs.py)."""
        from ai.models import AIModelTestRun, AIRequestLog
        from ai.admin.request_logs import _build_batch_log_snapshot
        self.test_run.status = AIModelTestRun.STATUS_RUNNING
        self.test_run.save(update_fields=["status"])
        log = AIRequestLog.objects.create(
            user=self.superuser, source="arm", mode="batch_solve",
            message=f"Batch solve run {self.run_id}",
            status=AIRequestLog.STATUS_ERROR, sent_at=timezone.now(),
        )
        snapshot = _build_batch_log_snapshot(log)
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["run_status"], "running")
        self.assertIn("run_meta", snapshot["report"])


class BatchResultsFreshFirstTests(TestCase):
    """Свежие пары — сверху (ТЗ: «в логах пакетного решения свежие задачи
    добавлять наверх, а не вниз»).

    Свежесть пары — время последнего обновления её строки
    (``AIModelTestResult.updated_at``): повтор пары перезаписывает строку,
    и перезапущенная пара поднимается наверх.
    """

    def setUp(self):
        from ai.models import AIModelTestRun, AIModelTestResult, Task
        self.task = Task.objects.create(node_id=6201, name="Задача", statement="x")
        self.run = AIModelTestRun.objects.create(
            run_id="e" * 32,
            run_type=AIModelTestRun.RUN_TYPE_BATCH,
            status=AIModelTestRun.STATUS_COMPLETED,
        )
        self.old = AIModelTestResult.objects.create(
            run=self.run, task=self.task, model_key="M1", model_title="M1",
            status="ok", verdict="solved", updated_at=timezone.now() - timezone.timedelta(hours=2),
        )
        self.fresh = AIModelTestResult.objects.create(
            run=self.run, task=self.task, model_key="M2", model_title="M2",
            status="ok", verdict="solved", updated_at=timezone.now() - timezone.timedelta(minutes=1),
        )

    def test_db_results_are_fresh_first(self):
        """Выборка из БД: свежая пара выше старой (order_by -updated_at)."""
        from ai.arm_runner import _batch_results_from_db
        rows = _batch_results_from_db(self.run, light=True)
        self.assertEqual([r["result_id"] for r in rows], [self.fresh.id, self.old.id])
        # Свежесть отдаётся и в снапшоте (epoch-секунды).
        self.assertGreater(rows[0]["updated_ts"], rows[1]["updated_ts"])

    def test_helper_orders_by_freshness_then_id(self):
        """_sort_results_fresh_first: по времени, при равенстве — по id."""
        from ai.arm_runner import _sort_results_fresh_first
        items = [
            {"result_id": 1, "updated_ts": 100.0},
            {"result_id": 3, "updated_ts": 200.0},
            {"result_id": 2, "updated_ts": 200.0},
            {"result_id": 4},
        ]
        ordered = [i["result_id"] for i in _sort_results_fresh_first(items)]
        self.assertEqual(ordered, [3, 2, 1, 4])

    def test_snapshot_from_db_is_fresh_first(self):
        from ai.arm_runner import get_arm_run_snapshot
        snapshot = get_arm_run_snapshot(self.run.run_id, light_results=True)
        self.assertEqual(
            [r["result_id"] for r in snapshot["results"]],
            [self.fresh.id, self.old.id],
        )


class BatchTopicLabelTests(TestCase):
    """Темы в отчёте пакетного решения — настоящие (ТЗ: «неправильно взяты
    темы»): локальная тема по ID, иначе ПАПКА DL из пути задачи, а не имя
    задачи."""

    def test_label_prefers_topic_then_folder(self):
        from ai.arm_runner import _topic_bucket_label
        self.assertEqual(
            _topic_bucket_label({"topic_name": "Циклы", "topic_id": 3}),
            "Циклы",
        )
        # Темы нет — подписью служит папка DL (путь: «Курс\\Папка\\Задача»).
        self.assertEqual(
            _topic_bucket_label({
                "topic_name": "", "topic_id": None,
                "tree_path": "Программирование [Ассемблер i8086, C-MPA]\\Одномерные массивы\\Сумма цифр числа",
                "task_name": "Сумма цифр числа",
            }),
            "Одномерные массивы",
        )
        # Ни темы, ни пути — честная подпись по задаче.
        self.assertEqual(
            _topic_bucket_label({"topic_name": "", "topic_id": None, "task_name": "Задача 1"}),
            "Задача 1",
        )

    def test_errors_grouped_by_topic_id_then_folder(self):
        """Ошибки по темам: одинаковые названия у разных языков не сливаются;
        пары без темы группируются по папке DL."""
        from ai.arm_runner import _build_batch_report
        results = [
            {"model_key": "A", "model_title": "A", "topic_id": 1, "topic_name": "Циклы",
             "verdict": "failed", "task_node_id": 1, "task_name": "Зад1"},
            {"model_key": "B", "model_title": "B", "topic_id": 2, "topic_name": "Циклы",
             "verdict": "failed", "task_node_id": 2, "task_name": "Зад2"},
            {"model_key": "A", "model_title": "A", "topic_id": None, "topic_name": "",
             "tree_path": "Курс\\Одномерные массивы\\Зад3",
             "verdict": "failed", "task_node_id": 3, "task_name": "Зад3"},
            {"model_key": "A", "model_title": "A", "topic_id": None, "topic_name": "",
             "tree_path": "Курс\\Одномерные массивы\\Зад4",
             "verdict": "failed", "task_node_id": 4, "task_name": "Зад4"},
        ]
        report = _build_batch_report(results)
        errors = report["errors_by_topic"]
        # Три строки: две темы с одинаковым названием (разные ID) + папка DL.
        self.assertEqual(len(errors), 3)
        labels = sorted(e["topic"] for e in errors)
        self.assertEqual(labels, ["Одномерные массивы", "Циклы", "Циклы"])
        folder_row = [e for e in errors if e["topic"] == "Одномерные массивы"][0]
        self.assertEqual(folder_row["count"], 2)

    def test_report_carries_run_topic(self):
        """Тема прогона (запасная для задач без своей) — в отчёте."""
        from ai.arm_runner import _build_batch_report
        report = _build_batch_report(
            [{"model_key": "A", "model_title": "A", "topic_id": 7, "topic_name": "Циклы",
              "verdict": "solved", "task_node_id": 1, "task_name": "Зад1"}],
            run_topic_id=7, run_topic_name="Циклы",
        )
        self.assertEqual(report["run_topic"]["id"], 7)
        self.assertEqual(report["run_topic"]["name"], "Циклы")

    def test_errors_by_topic_carry_failure_reasons(self):
        """Внутри темы видно, ЧЕМ не решено (компиляция / ответ / нет кода)."""
        from ai.arm_runner import _build_batch_report
        results = [
            {"model_key": "A", "model_title": "A", "topic_id": 1, "topic_name": "Циклы",
             "verdict": "failed", "failure_reason": "compile_error",
             "task_node_id": 1, "task_name": "Зад1"},
            {"model_key": "A", "model_title": "A", "topic_id": 1, "topic_name": "Циклы",
             "verdict": "failed", "failure_reason": "compile_error",
             "task_node_id": 2, "task_name": "Зад2"},
            {"model_key": "B", "model_title": "B", "topic_id": 1, "topic_name": "Циклы",
             "verdict": "failed", "failure_reason": "wrong_answer",
             "task_node_id": 3, "task_name": "Зад3"},
            {"model_key": "A", "model_title": "A", "topic_id": 1, "topic_name": "Циклы",
             "verdict": "failed", "failure_reason": "",
             "task_node_id": 4, "task_name": "Зад4"},
            {"model_key": "A", "model_title": "A", "topic_id": 1, "topic_name": "Циклы",
             "verdict": "solved", "task_node_id": 5, "task_name": "Зад5"},
        ]
        report = _build_batch_report(results)
        row = [e for e in report["errors_by_topic"] if e["topic"] == "Циклы"][0]
        self.assertEqual(row["count"], 4)
        reasons = {r["key"]: r["count"] for r in row["reasons"]}
        self.assertEqual(reasons["compile_error"], 2)
        self.assertEqual(reasons["wrong_answer"], 1)
        # Пара без причины — честная «причина не распознана».
        self.assertEqual(reasons["undefined"], 1)
        labels = {r["key"]: r["label"] for r in row["reasons"]}
        self.assertEqual(labels["compile_error"], "Ошибка компиляции")


class TaskTreePathTests(TestCase):
    """Путь задачи в дереве DL (Task.tree_path) — источник честной темы."""

    def test_apply_dl_task_info_fills_tree_path(self):
        from ai.models import Task
        from ai.services.task_registry import apply_dl_task_info
        task = Task.objects.create(node_id=6301, name="Задача")
        apply_dl_task_info(task, {"path": "Курс\\Папка\\Задача", "name": "Задача"})
        self.assertEqual(task.tree_path, "Курс\\Папка\\Задача")

    def test_tree_path_map_from_cached_course_tree(self):
        """Пути строятся из закэшированного дерева курса (без запросов к DL)."""
        from django.core.cache import cache
        from ai.services.task_registry import dl_tree_path_map
        cache.set("ai:dl_tree:1450", {
            "tree": [{
                "nodeId": 1, "name": "Курс", "isFolder": True,
                "children": [
                    {"nodeId": 2, "name": "Папка", "isFolder": True,
                     "children": [{"nodeId": 3, "name": "Задача", "isFolder": False}]},
                ],
            }],
            "task_count": 1,
        })
        paths = dl_tree_path_map(1450)
        self.assertEqual(paths.get("3"), "Курс\\Папка\\Задача")
        self.assertEqual(dl_tree_path_map(999), {})

    def test_topic_guess_ignores_task_name_segment(self):
        """Имя задачи — не тема: задача «Сумма цифр числа» не должна давать
        тему «Цифры числа» (темы — это ПАПКИ дерева)."""
        from ai.models import ProgrammingLanguage, Topic
        from ai.services.task_registry import _guess_topic_from_path
        lang = ProgrammingLanguage.objects.create(language_name="Pascal")
        topic = Topic.objects.create(topic_name_ru="Цифры числа", programming_language=lang)
        # Путь с папкой «Цифры числа» — тема находится.
        self.assertEqual(
            _guess_topic_from_path("Курс\\Цифры числа\\Сумма цифр числа", lang.id).id,
            topic.id,
        )
        # Путь, где «Цифры числа» — только имя задачи: темы быть не должно.
        self.assertIsNone(
            _guess_topic_from_path("Курс\\Сумма цифр числа", lang.id),
        )


class ActiveRunsEndpointTests(TestCase):
    """/ai/admin/active-runs/: личное меню «мои процессы» в шапке админки.
    Каждый админ видит только СВОИ прогоны (даже суперпользователь); завершённые
    видны 10 минут (окно уведомления); обновление моделей — глобальная запись,
    видимая каждому админу (sweep общий и без владельца)."""

    def setUp(self):
        self.factory = RequestFactory()
        self.superuser = get_user_model().objects.create_superuser(
            username="badge_admin", password="x", email="a@t.com",
        )
        self.normal_user = get_user_model().objects.create_user(username="badge_user", password="x")
        self.pd_user = get_user_model().objects.create_user(username="badge_pd", password="x")
        make_prompt_developer(self.pd_user)

    def _make_request(self, user):
        request = self.factory.get("/ai/admin/active-runs/")
        request.user = user
        return request

    def test_user_sees_only_own_runs(self):
        """Свои прогоны всех 4 типов видны, чужие — нет (включая superuser)."""
        from ai import arm_runner, model_health, test_console_runner
        from ai.admin.active_runs import admin_active_runs_view
        now_ts = time.time()
        mine = {
            "badge-batch": (arm_runner._jobs, {
                "run_id": "badge-batch", "user_id": self.superuser.id,
                "run_type": "batch", "status": "running",
                "completed_pairs": 1, "total_pairs": 3, "current_task_name": "A",
                "created_at_ts": now_ts, "updated_at_ts": now_ts,
            }),
            "badge-single": (arm_runner._jobs, {
                "run_id": "badge-single", "user_id": self.superuser.id,
                "run_type": "single", "status": "running",
                "completed_models": 0, "total_models": 2,
                "current_model_title": "FakeModel",
                "created_at_ts": now_ts, "updated_at_ts": now_ts,
            }),
            "badge-console": (test_console_runner._jobs, {
                "run_id": "badge-console", "user_id": self.superuser.id,
                "status": "running",
                "completed": 1, "total": 2, "current": "ai.tests",
                "created_at_ts": now_ts, "updated_at_ts": now_ts,
            }),
        }
        foreign = {
            "badge-foreign": (arm_runner._jobs, {
                "run_id": "badge-foreign", "user_id": self.normal_user.id,
                "run_type": "batch", "status": "running",
                "completed_pairs": 0, "total_pairs": 5, "current_task_name": "X",
                "created_at_ts": now_ts, "updated_at_ts": now_ts,
            }),
        }
        injected = {**mine, **foreign}
        try:
            for run_id, (jobs_dict, job) in injected.items():
                jobs_dict[run_id] = job
            # Глобальная запись обновления моделей не должна мешать подсчёту
            # «ровно по одному своих типов» — изолируем её состояние.
            with patch.dict(model_health._refresh_progress, {}, clear=True):
                response = admin_active_runs_view(self._make_request(self.superuser))
            data = json.loads(response.content)
            self.assertTrue(data["ok"])
            by_type = {r["run_type"]: r for r in data["runs"]}
            self.assertEqual(
                set(by_type),
                {"batch", "single", "test_console"},
            )
            self.assertEqual(by_type["batch"]["page_url"], "/ai/admin/arm/solve/")
            # У single-прогонов нет своей страницы (старый ARM-скрипт
            # «В чём ошибка» удалён) — ссылка пустая.
            self.assertEqual(by_type["single"]["page_url"], "")
            self.assertEqual(by_type["test_console"]["page_url"], "/ai/admin/test-console/")
            self.assertEqual(by_type["batch"]["completed"], 1)
            self.assertEqual(by_type["batch"]["total"], 3)
            self.assertEqual(by_type["batch"]["current"], "A")
            self.assertEqual(by_type["batch"]["status"], "running")
            # Чужой batch-прогон не «перекрывает» свой: ровно один каждого типа.
            self.assertEqual(len(data["runs"]), 3)
            self.assertTrue(all(r["status"] == "running" for r in data["runs"]))
        finally:
            for run_id, (jobs_dict, _job) in injected.items():
                jobs_dict.pop(run_id, None)

    def test_recently_finished_included_old_excluded(self):
        """Свежий завершённый прогон виден (окно уведомления), старый — нет;
        running виден всегда."""
        from ai import arm_runner
        from ai.admin.active_runs import admin_active_runs_view
        now_ts = time.time()
        injected = {
            "badge-done-fresh": {
                "run_id": "badge-done-fresh", "user_id": self.superuser.id,
                "run_type": "batch", "status": "completed",
                "completed_pairs": 2, "total_pairs": 2, "current_task_name": "",
                "created_at_ts": now_ts, "updated_at_ts": now_ts,
            },
            "badge-done-stale": {
                "run_id": "badge-done-stale", "user_id": self.superuser.id,
                "run_type": "batch", "status": "failed",
                "completed_pairs": 1, "total_pairs": 2, "current_task_name": "",
                "created_at_ts": now_ts - 4000, "updated_at_ts": now_ts - 4000,
            },
            "badge-done-stale-running": {
                "run_id": "badge-done-stale-running", "user_id": self.superuser.id,
                "run_type": "single", "status": "running",
                "completed_models": 0, "total_models": 2, "current_model_title": "M",
                "created_at_ts": now_ts - 4000, "updated_at_ts": now_ts - 4000,
            },
        }
        try:
            for run_id, job in injected.items():
                arm_runner._jobs[run_id] = job
            response = admin_active_runs_view(self._make_request(self.superuser))
            data = json.loads(response.content)
            run_ids = {r["run_id"] for r in data["runs"]}
            self.assertIn("badge-done-fresh", run_ids)
            self.assertNotIn("badge-done-stale", run_ids)
            # Running виден всегда, независимо от свежести.
            self.assertIn("badge-done-stale-running", run_ids)
            statuses = {r["run_id"]: r["status"] for r in data["runs"]}
            self.assertEqual(statuses["badge-done-fresh"], "completed")
        finally:
            for run_id in injected:
                arm_runner._jobs.pop(run_id, None)

    def test_model_refresh_entry_global(self):
        """Обновление моделей — глобальная безличная запись: видна каждому
        админу (включая prompt_developer), с прогрессом N/M; завершённый sweep
        старше окна уведомления не виден; пустое состояние — записи нет."""
        import uuid

        from ai import model_health
        from ai.admin.active_runs import admin_active_runs_view
        now_ts = time.time()
        running = {
            "run_id": uuid.uuid4().hex, "status": "running",
            "total": 12, "completed": 3, "current": "Web_DeepSeek",
            "created_at_ts": now_ts, "updated_at_ts": now_ts,
        }
        stale_done = dict(running, status="completed", updated_at_ts=now_ts - 4000)
        try:
            # Running sweep виден любому админу, с прогрессом и ссылкой на страницу.
            with patch.dict(model_health._refresh_progress, running, clear=True):
                for user in (self.superuser, self.pd_user):
                    response = admin_active_runs_view(self._make_request(user))
                    data = json.loads(response.content)
                    entries = [r for r in data["runs"] if r["run_type"] == "model_refresh"]
                    self.assertEqual(len(entries), 1, user.username)
                    entry = entries[0]
                    self.assertEqual(entry["run_id"], running["run_id"])
                    self.assertEqual(entry["page_url"], "/ai/admin/arm/models/")
                    self.assertEqual(entry["status"], "running")
                    self.assertEqual(entry["completed"], 3)
                    self.assertEqual(entry["total"], 12)
                    self.assertEqual(entry["current"], "Web_DeepSeek")
            # Завершённый старше 10 минут — не виден.
            with patch.dict(model_health._refresh_progress, stale_done, clear=True):
                response = admin_active_runs_view(self._make_request(self.superuser))
                data = json.loads(response.content)
                self.assertEqual(
                    [r for r in data["runs"] if r["run_type"] == "model_refresh"], []
                )
            # Sweep ещё не было с старта процесса — записи нет.
            with patch.dict(model_health._refresh_progress, {}, clear=True):
                response = admin_active_runs_view(self._make_request(self.superuser))
                data = json.loads(response.content)
                self.assertEqual(
                    [r for r in data["runs"] if r["run_type"] == "model_refresh"], []
                )
        finally:
            model_health._refresh_progress.clear()

    def test_non_admin_forbidden(self):
        from ai.admin.active_runs import admin_active_runs_view
        response = admin_active_runs_view(self._make_request(self.normal_user))
        self.assertEqual(response.status_code, 403)

    def test_prompt_developer_allowed_own_list(self):
        """prompt_developer — полноценный участник: 200 и свой (пустой) список."""
        from ai.admin.active_runs import admin_active_runs_view
        response = admin_active_runs_view(self._make_request(self.pd_user))
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertTrue(data["ok"])
        self.assertEqual(data["runs"], [])

    def test_batch_entry_carries_run_name(self):
        """Название прогона (2026-10) — в entry «моих процессов»; у прочих
        раннеров поля нет (пусто)"""
        from ai import arm_runner
        from ai.admin.active_runs import admin_active_runs_view
        now_ts = time.time()
        arm_runner._jobs["badge-named"] = {
            "run_id": "badge-named", "user_id": self.superuser.id,
            "run_type": "batch", "status": "running",
            "completed_pairs": 0, "total_pairs": 3, "current_task_name": "A",
            "run_name": "Лаба 3 (2 модели)",
            "created_at_ts": now_ts, "updated_at_ts": now_ts,
        }
        try:
            response = admin_active_runs_view(self._make_request(self.superuser))
            data = json.loads(response.content)
            entry = next(r for r in data["runs"] if r["run_id"] == "badge-named")
            self.assertEqual(entry["name"], "Лаба 3 (2 модели)")
        finally:
            arm_runner._jobs.pop("badge-named", None)

        # Прогон без run_name → пустая строка (фронт строку не рисует).
        arm_runner._jobs["badge-unnamed"] = {
            "run_id": "badge-unnamed", "user_id": self.superuser.id,
            "run_type": "batch", "status": "running",
            "completed_pairs": 0, "total_pairs": 1, "current_task_name": "A",
            "created_at_ts": now_ts, "updated_at_ts": now_ts,
        }
        try:
            response = admin_active_runs_view(self._make_request(self.superuser))
            data = json.loads(response.content)
            entry = next(r for r in data["runs"] if r["run_id"] == "badge-unnamed")
            self.assertEqual(entry["name"], "")
        finally:
            arm_runner._jobs.pop("badge-unnamed", None)


class TestConsoleRunnerTests(TestCase):
    """Тестовая консоль: «последний прогон» пользователя (10 минут),
    привязка прогона к user_id, дисковые логи (последние 10, полный
    raw-вывод, path-traversal барьер) и их admin-endpoint'ы."""

    def setUp(self):
        self.factory = RequestFactory()
        self.superuser = get_user_model().objects.create_superuser(
            username="tc_admin", password="x", email="a@t.com",
        )
        self.other_superuser = get_user_model().objects.create_superuser(
            username="tc_other", password="x", email="b@t.com",
        )

    def _make_job(self, run_id, user_id, status="running", age_seconds=0.0):
        now_ts = time.time() - age_seconds
        return {
            "run_id": run_id, "user_id": user_id, "status": status,
            "error_message": "",
            "total": 3,
            "completed": 3 if status != "running" else 0,
            "current": "" if status != "running" else "Тесты · test_x",
            "results": [],
            "summary": None if status == "running" else {
                "ran": 3, "seconds": 1.0, "ok": True,
                "failures": 0, "errors": 0, "skipped": 0,
            },
            "log": [],
            "created_at_ts": now_ts, "updated_at_ts": now_ts,
        }

    def test_get_latest_run_snapshot_own_fresh_and_running(self):
        """Свой свежий завершённый и свой running возвращаются (deepcopy);
        устаревший и чужой — нет."""
        from ai import test_console_runner
        injected = {
            "tc-fresh": self._make_job("tc-fresh", self.superuser.id, status="completed"),
            "tc-stale": self._make_job(
                "tc-stale", self.superuser.id, status="completed",
                age_seconds=test_console_runner._LAST_RUN_TTL_SECONDS + 300,
            ),
            "tc-foreign": self._make_job("tc-foreign", self.other_superuser.id, status="completed"),
            "tc-running": self._make_job(
                "tc-running", self.superuser.id, status="running",
                age_seconds=test_console_runner._LAST_RUN_TTL_SECONDS + 300,
            ),
        }
        try:
            for run_id, job in injected.items():
                test_console_runner._jobs[run_id] = job
            fresh = test_console_runner.get_latest_run_snapshot(self.superuser.id)
            # Свежий завершённый — новее running по created_at? running старее:
            # берется fresh; deepcopy, не живой дикт.
            self.assertIn(fresh["run_id"], {"tc-fresh", "tc-running"})
            self.assertIsInstance(fresh["log"], list)
            # Устаревший завершённый не возвращается сам по себе: убираем fresh
            # и running — остаётся только stale/чужой → None.
            test_console_runner._jobs.pop("tc-fresh")
            test_console_runner._jobs.pop("tc-running")
            self.assertIsNone(test_console_runner.get_latest_run_snapshot(self.superuser.id))
            # Running возвращается всегда, независимо от возраста.
            test_console_runner._jobs["tc-running"] = injected["tc-running"]
            latest = test_console_runner.get_latest_run_snapshot(self.superuser.id)
            self.assertEqual(latest["run_id"], "tc-running")
        finally:
            for run_id in list(injected) + ["tc-fresh", "tc-running"]:
                test_console_runner._jobs.pop(run_id, None)

    def test_start_test_run_stores_user_id(self):
        """start_test_run(user_id) записывает user_id в job и list_user_runs."""
        import threading

        from ai import test_console_runner
        with patch.object(threading.Thread, "start", lambda self: None):
            run_id, err = test_console_runner.start_test_run(self.superuser.id)
        self.assertEqual(err, "")
        self.assertTrue(run_id)
        try:
            self.assertEqual(
                test_console_runner._jobs[run_id]["user_id"], self.superuser.id,
            )
            runs = test_console_runner.list_user_runs(self.superuser.id)
            self.assertEqual([r["run_id"] for r in runs], [run_id])
            self.assertEqual(runs[0]["run_type"], "test_console")
            self.assertEqual(runs[0]["page_url"], "/ai/admin/test-console/")
            self.assertEqual(runs[0]["status"], "running")
            self.assertEqual(
                test_console_runner.list_user_runs(self.other_superuser.id), [],
            )
        finally:
            test_console_runner._jobs.pop(run_id, None)

    def test_count_test_cases_returns_positive(self):
        """Реальный discovery: total известен до прогона (для % и бара)."""
        from ai.test_console_runner import _count_test_cases
        self.assertGreater(_count_test_cases(), 0)

    def test_count_test_cases_falls_back_to_zero(self):
        """Ошибка discovery → 0 (фронт рисует indeterminate-бар, не падает)."""
        from ai import test_console_runner
        with patch("django.test.runner.DiscoverRunner", side_effect=RuntimeError("boom")):
            self.assertEqual(test_console_runner._count_test_cases(), 0)

    def test_run_worker_pre_counts_total(self):
        """Worker считает total до запуска сабпроцесса и кладёт его в job —
        отсюда фронт берёт % для бара (меню + сама страница консоли)."""
        import io
        import threading as threading_mod

        from ai import test_console_runner

        class _FakeProc:
            stdout = io.StringIO("")
            returncode = 0

            def wait(self):
                return 0

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(threading_mod.Thread, "start", lambda self: self.run()), \
                    patch("ai.test_console_runner.subprocess.Popen", return_value=_FakeProc()) as mock_popen, \
                    patch.object(test_console_runner, "_count_test_cases", return_value=17), \
                    patch.object(test_console_runner, "_log_dir", return_value=Path(tmp)):
                run_id, err = test_console_runner.start_test_run(self.superuser.id)
        self.assertEqual(err, "")
        self.assertTrue(run_id)
        self.assertTrue(mock_popen.called)
        try:
            self.assertEqual(test_console_runner._jobs[run_id]["total"], 17)
            entry = test_console_runner.list_user_runs(self.superuser.id)[0]
            self.assertEqual(entry["total"], 17)
        finally:
            test_console_runner._jobs.pop(run_id, None)

    def test_log_filename_regex_blocks_traversal(self):
        """_LOG_FILENAME_RE — барьер path-traversal: только канонические имена."""
        from ai.test_console_runner import _LOG_FILENAME_RE
        self.assertTrue(_LOG_FILENAME_RE.match("test_console_20260901_120000_ab12cd34.log"))
        self.assertFalse(_LOG_FILENAME_RE.match("../settings.py.log"))
        self.assertFalse(_LOG_FILENAME_RE.match("sub/dir.log"))
        self.assertFalse(_LOG_FILENAME_RE.match("test_console_20260901_120000_ZZZZZZZZ.log"))
        self.assertFalse(_LOG_FILENAME_RE.match(""))
        self.assertFalse(_LOG_FILENAME_RE.match("test_console_20260901_120000_ab12cd34.txt"))

    def test_rotate_logs_keeps_last_10(self):
        """Ротация держит только последние _MAX_LOG_FILES логов (новейшие)."""
        from ai import test_console_runner as runner
        with tempfile.TemporaryDirectory() as tmp:
            for i in range(12):
                name = f"test_console_20260901_{i:06d}_ab12cd34.log"
                (Path(tmp) / name).write_text("x", encoding="utf-8")
            (Path(tmp) / "unrelated.log").write_text("x", encoding="utf-8")
            runner._rotate_logs(Path(tmp))
            remaining = sorted(p.name for p in Path(tmp).iterdir())
            # Посторонние файлы ротация не трогает.
            self.assertIn("unrelated.log", remaining)
            log_names = [n for n in remaining if n != "unrelated.log"]
            self.assertEqual(len(log_names), runner._MAX_LOG_FILES)
            # Удалены самые старые (минимальные timestamp-ы).
            self.assertNotIn("test_console_20260901_000000_ab12cd34.log", log_names)
            self.assertNotIn("test_console_20260901_000001_ab12cd34.log", log_names)
            self.assertIn("test_console_20260901_000011_ab12cd34.log", log_names)

    def test_list_disk_logs_metadata(self):
        """Заголовок/футер парсятся: статус+summary; без футера — interrupted."""
        from ai import test_console_runner as runner
        run_id = "ab12cd34" + "ef" * 12
        with tempfile.TemporaryDirectory() as tmp:
            stamp = time.strftime("%Y%m%d_%H%M%S", time.gmtime(time.time()))
            full = Path(tmp) / f"test_console_{stamp}_{run_id[:8]}.log"
            full.write_text(
                runner._format_log_header(run_id, time.time(), 7) + "\n"
                "test_x (ai.tests.X) ... ok\n"
                + runner._format_log_footer("completed", {
                    "ran": 1, "seconds": 2.5, "ok": True,
                    "failures": 0, "errors": 0, "skipped": 0,
                }) + "\n",
                encoding="utf-8",
            )
            interrupted = Path(tmp) / "test_console_20260901_100000_0000ff00.log"
            interrupted.write_text(
                runner._format_log_header("0000ff00" + "aa" * 12, time.time(), 7) + "\n"
                "test_y (ai.tests.Y) ... ",
                encoding="utf-8",
            )
            # Дубликат с run_id running-джобы → статус running.
            (Path(tmp) / "test_console_20260901_110000_11aa22bb.log").write_text(
                runner._format_log_header("11aa22bb" + "cc" * 12, time.time(), 7) + "\n",
                encoding="utf-8",
            )
            try:
                runner._jobs["running-11aa22bb"] = {
                    "run_id": "11aa22bb" + "cc" * 12,
                    "user_id": self.superuser.id,
                    "status": "running",
                }
                entries = runner.list_disk_logs(Path(tmp))
                by_status = {e["status"] for e in entries}
                self.assertIn("completed", by_status)
                self.assertIn("interrupted", by_status)
                self.assertIn("running", by_status)
                done = next(e for e in entries if e["status"] == "completed")
                self.assertEqual(done["run_id"], run_id)
                self.assertTrue(done["summary"]["ok"])
                self.assertEqual(done["summary"]["ran"], 1)
                self.assertEqual(done["summary"]["seconds"], 2.5)
                self.assertTrue(done["filename"].endswith(".log"))
                # Новые первыми: файл с текущим timestamp позже 100000.
                names = [e["filename"] for e in entries]
                self.assertEqual(names, sorted(names, reverse=True))
                # Чтение по имени работает, traversal отклоняется.
                text = runner.read_disk_log(done["filename"], Path(tmp))
                self.assertIn("=== RUN", text)
                self.assertIsNone(runner.read_disk_log("../settings.py", Path(tmp)))
                self.assertIsNone(runner.read_disk_log("nope.log", Path(tmp)))
            finally:
                runner._jobs.pop("running-11aa22bb", None)

    def test_admin_test_console_view_preloads_last_run(self):
        """Без ?run_id= страница подгружает последний свой свежий прогон;
        устаревший/чужой — пусто."""
        from ai import test_console_runner
        from ai.admin.test_console import admin_test_console_view
        injected = {
            "tc-view-fresh": self._make_job(
                "tc-view-fresh", self.superuser.id, status="completed",
            ),
        }
        try:
            for run_id, job in injected.items():
                test_console_runner._jobs[run_id] = job
            request = self.factory.get("/ai/admin/test-console/")
            request.user = self.superuser
            request.session = {}
            response = admin_test_console_view(request)
            self.assertEqual(response.context_data["active_run_snapshot"]["run_id"], "tc-view-fresh")
            self.assertEqual(response.context_data["active_run_id"], "tc-view-fresh")
            # Другой пользователь не видит чужой последний прогон.
            request2 = self.factory.get("/ai/admin/test-console/")
            request2.user = self.other_superuser
            request2.session = {}
            response2 = admin_test_console_view(request2)
            self.assertEqual(response2.context_data["active_run_snapshot"], {})
        finally:
            for run_id in injected:
                test_console_runner._jobs.pop(run_id, None)

    def test_console_log_endpoints(self):
        """logs/: список для superuser, 403 для остальных; logs/view/:
        JSON-контент, ?download=1 → text/plain attachment, traversal → 404."""
        from ai import test_console_runner as runner
        from ai.admin.test_console import (
            admin_test_console_log_view,
            admin_test_console_logs_view,
        )
        filename = "test_console_20260901_120000_ab12cd34.log"
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / filename).write_text(
                "=== RUN ... ===\nok\n", encoding="utf-8",
            )
            with patch("ai.test_console_runner._log_dir", return_value=Path(tmp)):
                super_request = self.factory.get("/ai/admin/test-console/logs/")
                super_request.user = self.superuser
                response = admin_test_console_logs_view(super_request)
                self.assertEqual(response.status_code, 200)
                data = json.loads(response.content)
                self.assertTrue(data["ok"])
                self.assertEqual(data["logs"][0]["filename"], filename)

                # other_superuser — тоже superuser (консоль только для них):
                # запрет проверяем обычным пользователем.
                normal_request = self.factory.get("/ai/admin/test-console/logs/")
                normal_user = get_user_model().objects.create_user(
                    username="tc_plain", password="x",
                )
                normal_request.user = normal_user
                self.assertEqual(admin_test_console_logs_view(normal_request).status_code, 403)

                view_request = self.factory.get(
                    "/ai/admin/test-console/logs/view/", {"filename": filename},
                )
                view_request.user = self.superuser
                view_response = admin_test_console_log_view(view_request)
                self.assertEqual(view_response.status_code, 200)
                view_data = json.loads(view_response.content)
                self.assertTrue(view_data["ok"])
                self.assertIn("=== RUN", view_data["content"])

                download_request = self.factory.get(
                    "/ai/admin/test-console/logs/view/",
                    {"filename": filename, "download": "1"},
                )
                download_request.user = self.superuser
                download_response = admin_test_console_log_view(download_request)
                self.assertEqual(download_response.status_code, 200)
                self.assertEqual(
                    download_response["Content-Type"], "text/plain; charset=utf-8",
                )
                self.assertIn(
                    f'attachment; filename="{filename}"',
                    download_response["Content-Disposition"],
                )

                traversal_request = self.factory.get(
                    "/ai/admin/test-console/logs/view/",
                    {"filename": "../settings.py"},
                )
                traversal_request.user = self.superuser
                traversal_response = admin_test_console_log_view(traversal_request)
                self.assertEqual(traversal_response.status_code, 404)


class EachContextSuperUserFlagTests(TestCase):
    """each_context отдаёт is_super_user: предупреждение «не покидайте
    страницу» в тестовой консоли — только для не-суперпользователей
    (меню «мои процессы» в шапке видно всем и показывает только свои
    прогоны)."""

    def setUp(self):
        self.factory = RequestFactory()
        self.superuser = get_user_model().objects.create_superuser(
            username="ctx_admin", password="x", email="a@t.com",
        )
        self.normal_user = get_user_model().objects.create_user(username="ctx_user", password="x")

    def _each_context(self, user):
        from ai.admin.site import ai_admin_site
        request = self.factory.get("/ai/admin/")
        request.user = user
        request.session = {}
        return ai_admin_site.each_context(request)

    def test_is_super_user_flag(self):
        self.assertTrue(self._each_context(self.superuser)["is_super_user"])
        self.assertFalse(self._each_context(self.normal_user)["is_super_user"])


class ArmPromptBindingTests(TestCase):
    """Инструмент «Промпты по умолчанию» (ArmPromptBinding): unique
    (язык, тема, вид ARM), суперюзер-гейт страницы, save/delete через AJAX,
    привязки обоих режимов отдаются в контекст /arm/solve/ для
    JS авто-подстановки (новый ARM-скрипт «В чём ошибка» переиспользует)."""

    def setUp(self):
        self.factory = RequestFactory()
        self.superuser = get_user_model().objects.create_superuser(
            username="pd_admin", password="x", email="a@t.com",
        )
        self.staff = get_user_model().objects.create_user(
            username="pd_staff", password="x", is_staff=True,
        )
        self.lang = ProgrammingLanguage.objects.create(language_name="Pascal")
        self.other_lang = ProgrammingLanguage.objects.create(language_name="Python")
        self.topic = Topic.objects.create(
            topic_name_ru="Массивы", programming_language=self.lang,
        )
        self.other_topic = Topic.objects.create(
            topic_name_ru="Файлы", programming_language=self.other_lang,
        )
        self.prompt = Prompt.objects.create(
            prompt_name_ru="Массивы базовый", prompt_text_ru="Текст",
            topic=self.topic, owner=self.superuser,
        )

    def _binding(self, mode=ArmPromptBinding.MODE_SOLVE, **overrides):
        defaults = {
            "programming_language": self.lang,
            "topic": self.topic,
            "mode": mode,
            "prompt": self.prompt,
        }
        defaults.update(overrides)
        return ArmPromptBinding.objects.create(**defaults)

    def test_unique_language_topic_mode(self):
        # atomic-обёртка обязательна: IntegrityError ломает транзакцию TestCase
        # (validate_no_broken_transaction), savepoint-откат её восстанавливает.
        from django.db import IntegrityError, transaction
        self._binding()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ArmPromptBinding.objects.create(
                    programming_language=self.lang, topic=self.topic,
                    mode=ArmPromptBinding.MODE_SOLVE, prompt=self.prompt,
                )
        # Тот же вид — другая тема, та же тройка с другим видом — ок.
        self._binding(topic=self.other_topic)
        self._binding(mode=ArmPromptBinding.MODE_FIND_ERROR)

    def test_unique_language_level_binding(self):
        """«На весь язык» (topic=NULL) — одна на (язык, вид): частичный
        UNIQUE-констрейнт ловит дубликаты NULL-темы (обычный UNIQUE в
        Postgres NULLs не сравнивает)."""
        from django.db import IntegrityError, transaction
        self._binding(topic=None)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ArmPromptBinding.objects.create(
                    programming_language=self.lang, topic=None,
                    mode=ArmPromptBinding.MODE_SOLVE, prompt=self.prompt,
                )
        # Другой язык или другой вид с NULL-темой — ок.
        self._binding(topic=None, programming_language=self.other_lang)
        self._binding(topic=None, mode=ArmPromptBinding.MODE_FIND_ERROR)

    def test_resolve_falls_back_to_language_level(self):
        """ArmPromptBinding.resolve: точная (язык+тема) → «на весь язык» → None.
        Сервер и JS (findBoundPrompt/findBoundPromptName) обязаны резолвить
        одинаково — точная привязка сильнее языковой."""
        resolve = ArmPromptBinding.resolve
        mode = ArmPromptBinding.MODE_SOLVE
        # Ничего нет — None.
        self.assertIsNone(resolve(
            programming_language_id=self.lang.id, topic_id=self.topic.id, mode=mode,
        ))
        # Привязка «на весь язык»: и с темой, и без.
        lang_level = self._binding(topic=None)
        self.assertEqual(resolve(
            programming_language_id=self.lang.id, topic_id=self.topic.id, mode=mode,
        ), lang_level)
        self.assertEqual(resolve(
            programming_language_id=self.lang.id, topic_id=None, mode=mode,
        ), lang_level)
        # Точная привязка темы сильнее языковой.
        exact = self._binding()
        self.assertEqual(resolve(
            programming_language_id=self.lang.id, topic_id=self.topic.id, mode=mode,
        ), exact)
        # Другая тема — снова языковая.
        self.assertEqual(resolve(
            programming_language_id=self.lang.id, topic_id=self.other_topic.id, mode=mode,
        ), lang_level)
        # Другой вид — ничего.
        self.assertIsNone(resolve(
            programming_language_id=self.lang.id, topic_id=self.topic.id,
            mode=ArmPromptBinding.MODE_FIND_ERROR,
        ))

    def test_save_language_level_binding(self):
        """save без topic_id создаёт привязку «на весь язык»: до двух строк
        (solve + find_error), upsert, промпт может быть любой темы (не только
        безтематической), но обязан совпадать режимом привязки — языку без
        тем (Python, C++) нужно дефолтное правило, а тем у него нет."""
        from ai.admin.prompt_defaults import admin_prompt_defaults_view

        def post(topic_id):
            payload = {
                "action": "save", "mode": "solve",
                "language_id": str(self.other_lang.id),
                "prompt_id": str(self.prompt.id),  # промпт чужой темы — ок для языка
            }
            if topic_id is not None:
                payload["topic_id"] = topic_id
            request = self.factory.post("/ai/admin/prompt-defaults/", payload)
            request.user = self.superuser
            request.session = {}
            response = admin_prompt_defaults_view(request)
            return response.status_code, json.loads(response.content)

        status, data = post(None)
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"], data)
        binding = ArmPromptBinding.objects.get()
        self.assertIsNone(binding.topic_id)
        self.assertEqual(binding.programming_language, self.other_lang)

        # Тот же вид снова — upsert, второй строки нет.
        status, data = post("")
        self.assertTrue(data["ok"], data)
        self.assertEqual(ArmPromptBinding.objects.count(), 1)

        # Другой вид с той же пустой темой — вторая строка. Промпт обязан
        # относиться к режиму привязки (find_error) — берём find_error-промпт.
        find_error_prompt = Prompt.objects.create(
            prompt_name_ru="Python find bug", prompt_text_ru="Текст",
            owner=self.superuser, mode=Prompt.MODE_FIND_ERROR,
        )
        payload = {
            "action": "save", "mode": "find_error",
            "language_id": str(self.other_lang.id),
            "prompt_id": str(find_error_prompt.id),
        }
        request = self.factory.post("/ai/admin/prompt-defaults/", payload)
        request.user = self.superuser
        request.session = {}
        self.assertTrue(json.loads(admin_prompt_defaults_view(request).content)["ok"])
        self.assertEqual(ArmPromptBinding.objects.count(), 2)

        # Несуществующий язык без темы — 404.
        payload["language_id"] = "99999"
        request = self.factory.post("/ai/admin/prompt-defaults/", payload)
        request.user = self.superuser
        request.session = {}
        self.assertEqual(admin_prompt_defaults_view(request).status_code, 404)
        self.assertEqual(ArmPromptBinding.objects.count(), 2)

    def test_page_requires_superuser(self):
        """page.prompt_defaults — baseline (ТЗ 2026-10-19): staff с допуском
        в админку теперь тоже видит «Промпты по умолчанию»; остаётся 403
        у юзера без допуска вовсе (не staff, без page.admin)."""
        from ai.admin.prompt_defaults import admin_prompt_defaults_view
        request = self.factory.get("/ai/admin/prompt-defaults/")
        request.user = self.staff
        request.session = {}
        response = admin_prompt_defaults_view(request)
        self.assertEqual(response.status_code, 200)

        request = self.factory.get("/ai/admin/prompt-defaults/")
        request.user = self.superuser
        request.session = {}
        response = admin_prompt_defaults_view(request)
        self.assertEqual(response.status_code, 200)
        self.assertIn("bindings", response.context_data)

    def test_save_binding_creates_and_updates(self):
        from ai.admin.prompt_defaults import admin_prompt_defaults_view
        request = self.factory.post("/ai/admin/prompt-defaults/", {
            "action": "save",
            "language_id": str(self.lang.id),
            "topic_id": str(self.topic.id),
            "mode": "solve",
            "prompt_id": str(self.prompt.id),
        })
        request.user = self.superuser
        request.session = {}
        response = admin_prompt_defaults_view(request)
        data = json.loads(response.content)
        self.assertTrue(data["ok"], data)
        binding = ArmPromptBinding.objects.get()
        self.assertEqual(binding.prompt, self.prompt)
        self.assertEqual(binding.mode, "solve")

        # Повторный save той же тройки — upsert, не ошибка.
        request = self.factory.post("/ai/admin/prompt-defaults/", {
            "action": "save",
            "language_id": str(self.lang.id),
            "topic_id": str(self.topic.id),
            "mode": "solve",
            "prompt_id": str(self.prompt.id),
        })
        request.user = self.superuser
        request.session = {}
        data = json.loads(admin_prompt_defaults_view(request).content)
        self.assertTrue(data["ok"], data)
        self.assertEqual(ArmPromptBinding.objects.count(), 1)

    def test_save_validates_language_and_prompt_topic(self):
        from ai.admin.prompt_defaults import admin_prompt_defaults_view

        def post(**fields):
            payload = {"action": "save", "mode": "solve"}
            payload.update(fields)
            request = self.factory.post("/ai/admin/prompt-defaults/", payload)
            request.user = self.superuser
            request.session = {}
            response = admin_prompt_defaults_view(request)
            return response.status_code, json.loads(response.content)

        # Тема не принадлежит выбранному языку.
        status, data = post(
            language_id=str(self.other_lang.id), topic_id=str(self.topic.id),
            prompt_id=str(self.prompt.id),
        )
        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])

        # Промпт привязан к другой теме.
        status, data = post(
            language_id=str(self.other_lang.id), topic_id=str(self.other_topic.id),
            prompt_id=str(self.prompt.id),
        )
        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])
        self.assertFalse(ArmPromptBinding.objects.exists())

    def test_delete_binding(self):
        from ai.admin.prompt_defaults import admin_prompt_defaults_view
        binding = self._binding()
        request = self.factory.post("/ai/admin/prompt-defaults/", {
            "action": "delete", "binding_id": str(binding.id),
        })
        request.user = self.superuser
        request.session = {}
        response = admin_prompt_defaults_view(request)
        data = json.loads(response.content)
        self.assertTrue(data["ok"], data)
        self.assertFalse(ArmPromptBinding.objects.exists())

        # Повторное удаление — 404 в JSON, не краш.
        request = self.factory.post("/ai/admin/prompt-defaults/", {
            "action": "delete", "binding_id": str(binding.id),
        })
        request.user = self.superuser
        request.session = {}
        response = admin_prompt_defaults_view(request)
        self.assertEqual(response.status_code, 404)

    def test_resolve_batch_prompt_prefers_run_language(self):
        """arm_runner._resolve_batch_prompt: язык формы прогона приоритетнее
        языка, сохранённого в Task (авто-определение может остаться от
        предыдущего прогона на другом языке). Регрессия бага: та же тема,
        привязки на два языка, Task хранит C-MPA — выбран Ассемблер,
        но резолвилась привязка C-MPA и модель писала код под C-MPA.
        """
        from types import SimpleNamespace

        from ai.arm_runner import _resolve_batch_prompt

        asm_prompt = Prompt.objects.create(
            prompt_name_ru="Массивы на ассемблере", prompt_text_ru="Текст",
            topic=self.topic, owner=self.superuser,
        )
        # Привязки на оба языка, одна и та же тема — как в курсе
        # «[Ассемблер i8086, C-MPA]».
        self._binding(prompt=self.prompt)  # (Pascal, тема)
        asm_binding = self._binding(
            programming_language=self.other_lang, prompt=asm_prompt,
        )
        # Task хранит stale язык Pascal (auto-определение из прошлого прогона).
        task = SimpleNamespace(
            programming_language_id=self.lang.id, topic_id=self.topic.id,
        )
        cache: dict = {}
        # Язык формы — другой (Assembler): привязка должна резолвиться по нему.
        self.assertEqual(
            _resolve_batch_prompt(task, self.other_lang.id, cache), asm_prompt.id,
        )
        # Кэш прогона тоже ключуется по языку формы.
        self.assertEqual(cache, {(self.other_lang.id, self.topic.id): asm_prompt.id})
        self.assertEqual(
            ArmPromptBinding.objects.get(pk=asm_binding.id).prompt_id, asm_prompt.id,
        )

    def test_resolve_batch_prompt_falls_back_to_task_language(self):
        """Без языка на форме — прежнее поведение: привязка по языку Task."""
        from types import SimpleNamespace

        from ai.arm_runner import _resolve_batch_prompt

        binding = self._binding()
        task = SimpleNamespace(
            programming_language_id=self.lang.id, topic_id=self.topic.id,
        )
        self.assertEqual(
            _resolve_batch_prompt(task, None, {}), binding.prompt_id,
        )
        # И когда у Task язык не проставлен вовсе.
        task_no_lang = SimpleNamespace(
            programming_language_id=None, topic_id=self.topic.id,
        )
        self.assertEqual(
            _resolve_batch_prompt(task_no_lang, self.lang.id, {}), binding.prompt_id,
        )

    def test_stale_topic_reguess_makes_binding_resolve(self):
        """Сквозная регрессия бага «сменил язык — промпт не подтянулся»:
        ensure_task с языком прогона перегадывает stale-тему из path (тема =
        имя папки DL, per-языковая), после чего _resolve_batch_prompt находит
        точную привязку нового языка. До фикса тема оставалась от языка A →
        resolve(B, тема_A) не находил точную привязку → задача без промпта.
        """
        from unittest.mock import patch as _patch

        from ai.arm_runner import _resolve_batch_prompt
        from ai.models import Task
        from ai.services.task_registry import ensure_task

        # Одноимённые темы и привязки на оба языка — как в курсе
        # «[Ассемблер i8086, C-MPA]».
        py_topic = Topic.objects.create(
            topic_name_ru="Массивы", programming_language=self.other_lang,
        )
        py_prompt = Prompt.objects.create(
            prompt_name_ru="Массивы Python", prompt_text_ru="Текст",
            topic=py_topic, owner=self.superuser,
        )
        self._binding(prompt=self.prompt)  # привязка Pascal не участвует
        self._binding(programming_language=self.other_lang, topic=py_topic, prompt=py_prompt)
        # Task от прошлого прогона на Pascal: stale язык и stale тема.
        Task.objects.create(
            node_id=555, name="Задача", statement="stmt", task_id=5,
            programming_language=self.lang, topic=self.topic,
            file_extension=".pas", active=True,
        )
        dl_data = {
            "taskId": 5, "name": "Задача", "statement": "stmt",
            "path": "Программирование\\Массивы\\Задача1",
        }
        with _patch("ai.services.task_registry.fetch_task_info", return_value=dl_data):
            task = ensure_task(
                555, session_id="DLSID", programming_language_id=self.other_lang.id,
            )
        self.assertEqual(task.programming_language_id, self.other_lang.id)
        self.assertEqual(task.topic_id, py_topic.id)
        self.assertEqual(_resolve_batch_prompt(task, self.other_lang.id, {}), py_prompt.id)

    def test_arm_solve_context_includes_bindings(self):
        from ai.admin.arm import admin_arm_solve_view
        binding = self._binding()
        request = self.factory.get("/ai/admin/arm/solve/")
        request.user = self.superuser
        request.session = {}
        response = admin_arm_solve_view(request)
        serialized = response.context_data["arm_prompt_bindings"]
        self.assertEqual(len(serialized), 1)
        self.assertEqual(serialized[0]["prompt_id"], self.prompt.id)
        self.assertEqual(serialized[0]["topic_id"], self.topic.id)
        self.assertEqual(serialized[0]["mode"], binding.mode)

    def test_arm_solve_context_includes_find_error_bindings(self):
        """Старый ARM-скрипт «В чём ошибка» удалён, но привязки обоих режимов
        по-прежнему отдаются в контекст /arm/solve/ (новый скрипт их переиспользует)."""
        from ai.admin.arm import admin_arm_solve_view
        self._binding(mode=ArmPromptBinding.MODE_FIND_ERROR)
        request = self.factory.get("/ai/admin/arm/solve/")
        request.user = self.superuser
        request.session = {}
        response = admin_arm_solve_view(request)
        serialized = response.context_data["arm_prompt_bindings"]
        self.assertEqual(len(serialized), 1)
        self.assertEqual(serialized[0]["mode"], "find_error")

    def test_arm_solve_context_language_options_and_topics(self):
        """Ручной выбор промпта убран: /arm/solve/ отдаёт селектор языков
        (solve_language_options: {id, name, extension}) и все темы с языком
        (фильтр на клиенте), привязки — по-прежнему в arm_prompt_bindings."""
        from ai.admin.arm import admin_arm_solve_view
        request = self.factory.get("/ai/admin/arm/solve/")
        request.user = self.superuser
        request.session = {}
        response = admin_arm_solve_view(request)
        language_options = response.context_data["language_options"]
        self.assertIn(
            {"id": self.lang.id, "name": "Pascal", "extension": ".pas"},
            language_options,
        )
        topic_ids = [t["id"] for t in response.context_data["topics"]]
        self.assertIn(self.topic.id, topic_ids)
        self.assertIn(self.other_topic.id, topic_ids)

    def test_arm_prompt_binding_serializer_includes_prompt_name(self):
        """Подпись «Промпт по умолчанию: …» строится на клиенте без
        загрузки списка промптов — serializer отдаёт prompt_name."""
        from ai.serializers import arm_prompt_binding
        data = arm_prompt_binding(self._binding())
        self.assertEqual(data["prompt_name"], "Массивы базовый")
        self.assertEqual(data["mode"], "solve")


# Хелпер: запустить async-корутину из синхронного test-метода (в sync-методах
# нет работающего event loop, поэтому asyncio.run безопасен).
def async_to_sync_runner(coro):
    import asyncio
    return asyncio.run(coro)


# ===================================================================
# W9: сокращённые названия моделей (XLSX-сводка /arm/solve/)
# ===================================================================

class ShortModelTitleTests(SimpleTestCase):
    """short_model_title/short_model_titles: точные примеры пользователя
    («Ollama GLM 5.2» → «OG5.2», «Ollama GPT-OSS 120B» → «GPTO120») имеют
    приоритет; без коллизий алгоритм детерминирован, при коллизии мапа
    уникальна."""

    def test_user_examples_take_precedence(self):
        from ai.model_clients.registry import SHORT_MODEL_TITLE_OVERRIDES, short_model_title
        self.assertEqual(short_model_title("Ollama GLM 5.2"), "OG5.2")
        self.assertEqual(short_model_title("Ollama GPT-OSS 120B"), "GPTO120")
        self.assertIn("Ollama GLM 5.2", SHORT_MODEL_TITLE_OVERRIDES)

    def test_algorithm_shape(self):
        from ai.model_clients.registry import short_model_title
        # Провайдер-токен → аббревиатура, буквенные → первая буква,
        # числа → целиком (включая дробные).
        self.assertEqual(short_model_title("Ollama Qwen 3 32B"), "OQ332B")
        self.assertEqual(short_model_title("Web DeepSeek"), "WD")

    def test_titles_map_unique_and_total(self):
        from ai.model_clients.registry import short_model_titles
        titles = ["Ollama GLM 5.2", "Ollama GLM 5", "Web DeepSeek"]
        mapping = short_model_titles(titles)
        self.assertEqual(set(mapping), set(titles))
        self.assertEqual(len(set(mapping.values())), len(titles))
        # Дубликат названия не перетирает сокращение следующим вариантом
        # (одна модель встречается в результатах на каждую задачу).
        mapping = short_model_titles(titles + ["Ollama GLM 5.2"])
        self.assertEqual(mapping["Ollama GLM 5.2"], "OG5.2")


# ===================================================================
# W9/Batch B: AIModelStats — скоупы (язык/тема), карта и обогащение чата
# ===================================================================

class AIModelStatsTests(TestCase):
    """record_batch_solve_stats: пара пишется в ОБЕ скоуп-строки — тему и
    всегда языковую (тема NULL); темы нет — только языковая; языка нет —
    пару не пишем. Семантика _per_bucket (total = solved+failed, skipped не
    считается; durations только не-skipped) и race-safe upsert. Читатель
    get_model_stats_map берёт РОВНО один скоуп; без языка/темы — ОБЩИЕ данные
    по всем темам и языкам (SQL-агрегат scoped-строк), легаси-строки (NULL/NULL)
    в агрегат не попадают."""

    def setUp(self):
        from ai.models import ProgrammingLanguage, Topic
        self.lang = ProgrammingLanguage.objects.create(language_name="Pascal")
        self.topic = Topic.objects.create(topic_name_ru="Массивы", programming_language=self.lang)
        self.other_lang = ProgrammingLanguage.objects.create(language_name="Python")
        self.other_topic = Topic.objects.create(topic_name_ru="Строки", programming_language=self.other_lang)

    @staticmethod
    def _pair(key, verdict, duration, lang_id, topic_id, title="M"):
        return {
            "model_key": key, "model_title": title, "verdict": verdict,
            "duration": duration,
            "programming_language_id": lang_id, "topic_id": topic_id,
        }

    def test_pair_writes_topic_and_language_rows(self):
        from ai.models import AIModelStats
        from ai.services.model_stats import record_batch_solve_stats
        record_batch_solve_stats([self._pair("K1", "solved", 2.0, self.lang.pk, self.topic.pk)])
        topic_row = AIModelStats.objects.get(
            model_key="K1", programming_language_id=self.lang.pk, topic_id=self.topic.pk)
        lang_row = AIModelStats.objects.get(
            model_key="K1", programming_language_id=self.lang.pk, topic__isnull=True)
        for row in (topic_row, lang_row):
            self.assertEqual((row.solved_count, row.total_count), (1, 1))
            self.assertEqual((row.duration_count, row.duration_sum), (1, 2.0))
        self.assertEqual(AIModelStats.objects.count(), 2)

    def test_topic_none_only_language_row(self):
        from ai.models import AIModelStats
        from ai.services.model_stats import record_batch_solve_stats
        record_batch_solve_stats([self._pair("K1", "failed", 4.0, self.lang.pk, None)])
        self.assertEqual(AIModelStats.objects.count(), 1)
        row = AIModelStats.objects.get(model_key="K1")
        self.assertIsNone(row.topic_id)
        self.assertEqual((row.solved_count, row.total_count), (0, 1))

    def test_no_language_not_recorded(self):
        """Без языка пару не пишем (не пачкаем легаси-глобальный скоуп)."""
        from ai.models import AIModelStats
        from ai.services.model_stats import record_batch_solve_stats
        record_batch_solve_stats([self._pair("K1", "solved", 2.0, None, self.topic.pk)])
        record_batch_solve_stats([self._pair("K1", "solved", 2.0, None, None)])
        self.assertEqual(AIModelStats.objects.count(), 0)

    def test_foreign_language_topic_dropped(self):
        """Тема пер-языковая: чужая тема хуже отсутствующей — пишем только
        языковую строку (легаси-поведение ensure_task)."""
        from ai.models import AIModelStats
        from ai.services.model_stats import record_batch_solve_stats
        record_batch_solve_stats([self._pair("K1", "solved", 2.0, self.lang.pk, self.other_topic.pk)])
        self.assertEqual(AIModelStats.objects.count(), 1)
        self.assertEqual(AIModelStats.objects.get(model_key="K1").topic_id, None)

    def test_accumulates_and_computes_stats(self):
        from ai.services.model_stats import get_model_stats_map, record_batch_solve_stats
        record_batch_solve_stats([
            self._pair("K1", "solved", 2.0, self.lang.pk, self.topic.pk),
            self._pair("K1", "failed", 4.0, self.lang.pk, self.topic.pk),
            self._pair("K2", "solved", 1.0, self.lang.pk, self.topic.pk),
        ])
        # Точный скоуп и языковой скоуп дают одинаковые числа (языковая строка
        # — зеркало темы).
        self.assertEqual(
            get_model_stats_map(["K1", "K2", "K3"], language_id=self.lang.pk, topic_id=self.topic.pk),
            {
                "K1": {"avg_seconds": 3.0, "percent_solved": 50.0},
                "K2": {"avg_seconds": 1.0, "percent_solved": 100.0},
            },
        )
        lang_stats = get_model_stats_map(["K1", "K2"], language_id=self.lang.pk)
        self.assertEqual(lang_stats["K1"]["avg_seconds"], 3.0)
        self.assertEqual(lang_stats["K2"]["percent_solved"], 100.0)
        self.assertNotIn("K3", lang_stats)

        # Второй прогон инкрементирует (не перетирает) ОБЕ строки.
        record_batch_solve_stats([self._pair("K1", "solved", 6.0, self.lang.pk, self.topic.pk)])
        stats = get_model_stats_map(["K1"], language_id=self.lang.pk, topic_id=self.topic.pk)
        self.assertEqual(stats["K1"], {"avg_seconds": 4.0, "percent_solved": 66.7})
        lang_stats = get_model_stats_map(["K1"], language_id=self.lang.pk)
        self.assertEqual(lang_stats["K1"], {"avg_seconds": 4.0, "percent_solved": 66.7})

    def test_skipped_semantics_matches_per_bucket(self):
        from ai.models import AIModelStats
        from ai.services.model_stats import get_model_stats_map, record_batch_solve_stats
        record_batch_solve_stats([
            self._pair("K1", "skipped", 1.0, self.lang.pk, self.topic.pk),
            self._pair("K1", "failed", 3.0, self.lang.pk, self.topic.pk),
        ])
        stats = get_model_stats_map(["K1"], language_id=self.lang.pk, topic_id=self.topic.pk)
        # skipped не в total и не в avg; его duration не портит среднее.
        self.assertEqual(stats["K1"], {"avg_seconds": 3.0, "percent_solved": 0.0})
        row = AIModelStats.objects.get(
            model_key="K1", programming_language_id=self.lang.pk, topic_id=self.topic.pk)
        self.assertEqual((row.total_count, row.duration_count), (1, 1))

    def test_scoped_map_isolation(self):
        """Карта читает РОВНО один скоуп: тема ≠ язык ≠ легаси (иначе строки
        разных скоупов в ключе model_key перезаписывали бы друг друга)."""
        from ai.models import AIModelStats
        from ai.services.model_stats import get_model_stats_map
        AIModelStats.objects.create(
            model_key="K1", solved_count=1, total_count=4, duration_count=1,
            duration_sum=8.0, programming_language=self.lang, topic=self.topic)
        AIModelStats.objects.create(
            model_key="K1", solved_count=2, total_count=2, duration_count=2,
            duration_sum=2.0, programming_language=self.lang, topic=None)
        AIModelStats.objects.create(
            model_key="K2", solved_count=1, total_count=1, duration_count=1,
            duration_sum=2.0)  # легаси (NULL, NULL)
        topic_stats = get_model_stats_map(["K1", "K2"], language_id=self.lang.pk, topic_id=self.topic.pk)
        self.assertEqual(topic_stats["K1"], {"avg_seconds": 8.0, "percent_solved": 25.0})
        self.assertNotIn("K2", topic_stats)
        lang_stats = get_model_stats_map(["K1"], language_id=self.lang.pk)
        self.assertEqual(lang_stats["K1"], {"avg_seconds": 1.0, "percent_solved": 100.0})
        # Без скоупа — общие данные по ВСЕМ темам и языкам: сумма scoped-строк
        # K1 (8с/1 замер + 2с/2 замера → 10/3 = 3.3; 3 решено из 6 = 50%).
        legacy = get_model_stats_map(["K1", "K2"])
        self.assertEqual(legacy["K1"], {"avg_seconds": 3.3, "percent_solved": 50.0})
        self.assertNotIn("K2", legacy)  # легаси-строка (NULL, NULL) в агрегат не попадает

    def test_available_model_options_enriched_with_stats(self):
        """get_available_model_options читает только легаси-скоуп (NULL/NULL) —
        data-атрибуты остались сортировке #selectModelSort (обогащение до
        cache.set — Redis-копии не мутируем)."""
        from django.core.cache import cache
        from ai.models import AIModelAvailability, AIModelStats
        from ai.model_health import get_available_model_options, get_health_window_date, MODEL_CATALOG_KEYS
        if not MODEL_CATALOG_KEYS:
            self.skipTest("No models in registry")
        key = MODEL_CATALOG_KEYS[0]
        AIModelStats.objects.create(
            model_key=key, solved_count=1, total_count=1,
            duration_count=1, duration_sum=5.0)  # легаси-строка напрямую
        AIModelAvailability.objects.create(
            model_key=key, model_title=key, is_available=True,
            window_date=get_health_window_date(), last_message="ok",
        )
        cache.clear()
        options = get_available_model_options()
        opt = next(o for o in options if o["key"] == key)
        self.assertEqual(opt["avg_seconds"], 5.0)
        self.assertEqual(opt["percent_solved"], 100.0)

    def test_solution_cache_writes_scoped_rows(self):
        """record_result (кэш решённых задач) пишет пару в скоуп TaskSolution:
        тема (topic_id) + языковая строка; решение без языка не пишется."""
        from ai.models import AIModelStats, TaskSolution
        from ai.services.solution_cache import record_result
        TaskSolution.objects.create(
            queue_id=900001, code="int main() { return 0; }",
            model_key="K1", model_title="M",
            programming_language_id=self.lang.pk, topic_id=self.topic.pk,
            task_node_id=42,
        )
        solution, verdict, solved = record_result(900001, "Все тесты пройдены")
        self.assertTrue(solved)
        self.assertEqual(verdict, "solved")
        self.assertEqual(
            AIModelStats.objects.filter(model_key="K1").count(), 2)
        topic_row = AIModelStats.objects.get(
            model_key="K1", programming_language_id=self.lang.pk,
            topic_id=self.topic.pk)
        self.assertEqual(topic_row.total_count, 1)
        AIModelStats.objects.all().delete()
        # Решение без языка — пара не пишется (как в batch-прогоне).
        TaskSolution.objects.create(
            queue_id=900002, code="int main() { return 0; }",
            model_key="K1", model_title="M",
            task_node_id=43, programming_language_id=None, topic_id=None,
        )
        record_result(900002, "Все тесты пройдены")
        self.assertEqual(AIModelStats.objects.count(), 0)


# ===================================================================
# W9: сообщение solve-прогона — промпт только из привязки
# ===================================================================

class SolveMessageTests(TestCase):
    """_build_solve_message: без привязки — базовая инструкция без текста
    промпта; с prompt_id (pk привязки) — текст промпта присутствует;
    условие задачи гарантированно входит в сообщение."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(username="sol", password="x")
        self.lang = ProgrammingLanguage.objects.create(language_name="Pascal")
        self.topic = Topic.objects.create(topic_name_ru="Массивы", programming_language=self.lang)
        self.prompt = Prompt.objects.create(
            prompt_name_ru="С особым стилем", prompt_text_ru="ПИШИ КАПСОМ: {message}",
            topic=self.topic, owner=self.user,
        )

    def _message(self, prompt_id=None):
        from ai.arm_runner import _build_solve_message
        return _build_solve_message(
            "Найти максимум в массиве", "Pascal", "Массивы",
            prompt_id=prompt_id,
        )

    def test_no_binding_means_no_preprompt(self):
        message = self._message()
        self.assertIn("Реши задачу по программированию", message)
        self.assertNotIn("ПИШИ КАПСОМ", message)
        self.assertIn("Найти максимум в массиве", message)

    def test_binding_prompt_id_includes_prompt_text(self):
        message = self._message(prompt_id=self.prompt.id)
        self.assertIn("ПИШИ КАПСОМ", message)
        # Плейсхолдер {message} подставлен условием задачи.
        self.assertIn("Найти максимум в массиве", message)

    def test_task_statement_always_included(self):
        # Промпт без {message}: условие дописывается явно (гарантия полного входа).
        self.prompt.prompt_text_ru = "Стиль без плейсхолдера"
        self.prompt.save()
        message = self._message(prompt_id=self.prompt.id)
        self.assertIn("Условие задачи:", message)
        self.assertIn("Найти максимум в массиве", message)

    def test_no_comments_instruction_appended_always(self):
        # Требование «без комментариев» приписывается к ЛЮБОМУ сообщению
        # пакетного решения — и с привязкой, и без неё.
        self.assertIn("никаких комментариев", self._message())
        message = self._message(prompt_id=self.prompt.id)
        self.assertIn("никаких комментариев", message)
        self.assertIn("ПИШИ КАПСОМ", message)

    def test_no_ramble_instruction_appended_always(self):
        # Запрет рассуждений: chain-of-thought flash-моделей сжигает бюджет
        # генерации до кода — приписка тоже ко ВСЕМ solve-сообщениям.
        self.assertIn("Не расписывай ход рассуждений", self._message())
        self.assertIn("Не расписывай ход рассуждений", self._message(prompt_id=self.prompt.id))


# ===================================================================
# W9: старт /arm/solve/ — язык, привязка, record_stats
# ===================================================================

class ArmSolveStartViewTests(TestCase):
    """admin_arm_solve_start_view: клиентский prompt_id — ручное перекрытие
    привязки (существует → промпт из списка; нет/пусто → привязка для
    вида solve); run_name прокидывается в запуск; расширение выводится из
    языка; record_stats принимается только от суперюзера."""

    def setUp(self):
        self.factory = RequestFactory()
        self.superuser = get_user_model().objects.create_superuser(
            username="ss_admin", password="x", email="s@t.com",
        )
        # Не-суперпользователь с доступом к ARM (staff, но не superuser).
        self.staff = get_user_model().objects.create_user(
            username="ss_staff", password="x", is_staff=True,
        )
        self.lang = ProgrammingLanguage.objects.create(language_name="Pascal")
        self.topic = Topic.objects.create(topic_name_ru="Массивы", programming_language=self.lang)
        self.prompt = Prompt.objects.create(
            prompt_name_ru="Массивы базовый", prompt_text_ru="Текст",
            topic=self.topic, owner=self.superuser,
        )
        self.binding = ArmPromptBinding.objects.create(
            programming_language=self.lang, topic=self.topic,
            mode=ArmPromptBinding.MODE_SOLVE, prompt=self.prompt,
        )

    def _post(self, user, payload):
        from ai.admin.arm import admin_arm_solve_start_view
        request = self.factory.post(
            "/ai/admin/arm/solve/start/", data=json.dumps(payload),
            content_type="application/json",
        )
        request.user = user
        request.session = {}
        with (
            patch("ai.admin.arm._resolve_session_id", return_value="sess-1"),
            patch("ai.admin.arm._resolve_active_course_id", return_value=(1450, "")),
            patch("ai.admin.arm.start_batch_solve_run", return_value=("run-x", None)) as start_mock,
        ):
            response = admin_arm_solve_start_view(request)
        return response, start_mock

    def test_start_without_binding_ok_and_language_resolves_extension(self):
        self.binding.delete()  # нет привязки → прогон без промпта
        payload = {
            "node_ids": [101, "102"], "models": ["FakeModel"],
            "language_id": self.lang.id, "arm_topic_id": self.topic.id,
            "dl_test": True,
        }
        response, start_mock = self._post(self.superuser, payload)
        self.assertEqual(response.status_code, 200)
        kwargs = start_mock.call_args.kwargs
        self.assertEqual(kwargs["solve_file_extension"], ".pas")
        self.assertEqual(kwargs["programming_language_id"], self.lang.id)
        self.assertIsNone(kwargs["prompt_id"])  # привязки нет → без промпта

    def test_start_nonexistent_prompt_id_leaves_choice_to_worker(self):
        payload = {
            "node_ids": [101], "models": ["FakeModel"],
            "language_id": self.lang.id, "arm_topic_id": self.topic.id,
            # Несуществующий prompt_id → «по привязке»: worker сам подбирает
            # промпт на каждую задачу (по теме из ветки DL).
            "prompt_id": 99999,
        }
        response, start_mock = self._post(self.superuser, payload)
        self.assertEqual(response.status_code, 200)
        kwargs = start_mock.call_args.kwargs
        self.assertIsNone(kwargs["prompt_id"])
        self.assertEqual(kwargs["prompt_name"], "")

    def test_start_manual_prompt_id_overrides_binding(self):
        """Существующий prompt_id (выбор из списка) перекрывает привязку."""
        other = Prompt.objects.create(
            prompt_name_ru="Общий промпт", prompt_text_ru="Текст 2",
            topic=self.topic, owner=self.superuser,
        )
        payload = {
            "node_ids": [101], "models": ["FakeModel"],
            "language_id": self.lang.id, "arm_topic_id": self.topic.id,
            "prompt_id": other.id,
        }
        response, start_mock = self._post(self.superuser, payload)
        self.assertEqual(response.status_code, 200)
        kwargs = start_mock.call_args.kwargs
        self.assertEqual(kwargs["prompt_id"], other.id)
        self.assertEqual(kwargs["prompt_name"], "Общий промпт")

    def test_start_empty_prompt_id_leaves_choice_to_worker(self):
        payload = {
            "node_ids": [101], "models": ["FakeModel"],
            "language_id": self.lang.id, "arm_topic_id": self.topic.id,
            "prompt_id": "",
        }
        response, start_mock = self._post(self.superuser, payload)
        self.assertEqual(response.status_code, 200)
        kwargs = start_mock.call_args.kwargs
        # «По привязке»: prompt_id не резолвится на старте — worker подбирает
        # привязку на каждую задачу по её теме (см. arm_runner._resolve_batch_prompt).
        self.assertIsNone(kwargs["prompt_id"])
        self.assertEqual(kwargs["prompt_name"], "")

    def test_start_passes_run_name(self):
        """Ручное название прогона тримится и передаётся в запуск."""
        payload = {
            "node_ids": [101], "models": ["FakeModel"],
            "language_id": self.lang.id, "arm_topic_id": self.topic.id,
            "run_name": "  Неделя 3  ",
        }
        response, start_mock = self._post(self.superuser, payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(start_mock.call_args.kwargs["run_name"], "Неделя 3")

    def test_non_superuser_record_stats_ignored(self):
        payload = {
            "node_ids": [101], "models": ["FakeModel"],
            "language_id": self.lang.id, "record_stats": "1",
        }
        _, start_mock = self._post(self.staff, payload)
        self.assertFalse(start_mock.call_args.kwargs["record_stats"])

    def test_superuser_record_stats_accepted(self):
        payload = {
            "node_ids": [101], "models": ["FakeModel"],
            "language_id": self.lang.id, "record_stats": "1",
        }
        _, start_mock = self._post(self.superuser, payload)
        self.assertTrue(start_mock.call_args.kwargs["record_stats"])

    def test_run_params_snapshot_contains_language_and_topic(self):
        payload = {
            "node_ids": [101], "models": ["FakeModel"],
            "language_id": self.lang.id, "arm_topic_id": self.topic.id,
        }
        _, start_mock = self._post(self.superuser, payload)
        kwargs = start_mock.call_args.kwargs
        self.assertEqual(kwargs["programming_language_id"], self.lang.id)
        self.assertEqual(kwargs["topic_id"], self.topic.id)

    def test_no_language_and_no_extension_rejected(self):
        payload = {"node_ids": [101], "models": ["FakeModel"]}
        response, _ = self._post(self.superuser, payload)
        self.assertEqual(response.status_code, 400)


class ArmSolvePreviousRunTests(TestCase):
    """/arm/solve/ без ?run_id= показывает последний batch-прогон
    пользователя (get_latest_batch_run_snapshot → is_previous_run)."""

    def setUp(self):
        from ai.models import AIModelTestRun
        self.factory = RequestFactory()
        self.superuser = get_user_model().objects.create_superuser(
            username="prev_admin", password="x", email="p@t.com",
        )
        # Не-суперпользователь с доступом к ARM (staff, но не superuser).
        self.other = get_user_model().objects.create_user(
            username="prev_other", password="x", is_staff=True,
        )
        self.run_id = "p" * 32
        AIModelTestRun.objects.create(
            run_id=self.run_id, run_type=AIModelTestRun.RUN_TYPE_BATCH,
            status=AIModelTestRun.STATUS_COMPLETED, user=self.superuser,
        )

    def test_view_without_run_id_shows_latest_run(self):
        from ai.admin.arm import admin_arm_solve_view
        request = self.factory.get("/ai/admin/arm/solve/")
        request.user = self.superuser
        request.session = {}
        response = admin_arm_solve_view(request)
        self.assertTrue(response.context_data["is_previous_run"])
        self.assertEqual(response.context_data["active_run_snapshot"]["run_id"], self.run_id)

    def test_other_users_run_not_shown(self):
        from ai.admin.arm import admin_arm_solve_view
        request = self.factory.get("/ai/admin/arm/solve/")
        request.user = self.other
        request.session = {}
        response = admin_arm_solve_view(request)
        self.assertFalse(response.context_data["is_previous_run"])
        self.assertEqual(response.context_data["active_run_snapshot"], {})

    def test_get_latest_batch_run_snapshot_scopes_by_user(self):
        from ai.arm_runner import get_latest_batch_run_snapshot
        self.assertIsNotNone(get_latest_batch_run_snapshot(self.superuser.id))
        self.assertIsNone(get_latest_batch_run_snapshot(self.other.id))
        self.assertIsNone(get_latest_batch_run_snapshot(None))


# ===================================================================
# W9: журнал запросов для prompt_developer — строго свои записи
# ===================================================================

class LogsDeveloperAccessTests(TestCase):
    """prompt_developer (non-staff): журнал запросов открыт только со своими
    записями — список фильтруется по user, чужие detail/xlsx дают 403,
    удаление запрещено."""

    def setUp(self):
        self.factory = RequestFactory()
        self.developer = get_user_model().objects.create_user(
            username="log_dev", password="x",
        )
        make_prompt_developer(self.developer)
        self.superuser = get_user_model().objects.create_superuser(
            username="log_admin", password="x", email="l@t.com",
        )
        self.own_log = AIRequestLog.objects.create(
            user=self.developer, source="chat", mode="chat", message="own",
            status=AIRequestLog.STATUS_SUCCESS, sent_at=timezone.now(),
        )
        self.foreign_log = AIRequestLog.objects.create(
            user=self.superuser, source="chat", mode="chat", message="foreign",
            status=AIRequestLog.STATUS_SUCCESS, sent_at=timezone.now(),
        )

    def test_logs_scope_is_own_user(self):
        from ai.admin.permissions import logs_scope_is_own_user
        self.assertTrue(logs_scope_is_own_user(self.developer))
        self.assertFalse(logs_scope_is_own_user(self.superuser))

    def test_list_shows_only_own_logs(self):
        from ai.admin.request_logs import admin_request_logs_view
        request = self.factory.get("/ai/admin/ai/request_logs/")
        request.user = self.developer
        request.session = {}
        response = admin_request_logs_view(request)
        self.assertEqual(response.status_code, 200)
        page_logs = list(response.context_data["page_obj"].object_list)
        self.assertIn(self.own_log, page_logs)
        self.assertNotIn(self.foreign_log, page_logs)

    def test_foreign_detail_forbidden(self):
        from ai.admin.request_logs import admin_request_log_detail_view
        request = self.factory.get(f"/ai/admin/ai/request_logs/{self.foreign_log.id}/")
        request.user = self.developer
        request.session = {}
        response = admin_request_log_detail_view(request, self.foreign_log.id)
        self.assertEqual(response.status_code, 403)

    def test_own_detail_allowed(self):
        from ai.admin.request_logs import admin_request_log_detail_view
        request = self.factory.get(f"/ai/admin/ai/request_logs/{self.own_log.id}/")
        request.user = self.developer
        request.session = {}
        response = admin_request_log_detail_view(request, self.own_log.id)
        self.assertEqual(response.status_code, 200)

    def test_delete_permission_denied_for_developer(self):
        from ai.admin.request_logs import AIRequestLogAdmin
        from ai.admin.site import ai_admin_site
        model_admin = AIRequestLogAdmin(AIRequestLog, ai_admin_site)
        request = self.factory.get("/ai/admin/")
        request.user = self.developer
        self.assertFalse(model_admin.has_delete_permission(request))
        # changelist ModelAdmin'а разработчику недоступен (non-staff).
        self.assertFalse(model_admin.has_module_permission(request))


# ===================================================================
# Левое меню админки: сгруппированные AI-инструменты + скрытие разделов
# ===================================================================

class AdminNavToolGroupTests(TestCase):
    """Левое меню (each_context → available_apps + наш оверрайд
    admin/app_list.html): первым идёт закреплённый пункт «Настройка
    ИИ-приложения» (ai-pinned, без заголовка), затем группы инструментов
    (Промпты → ARM → Диагностика → Система), каждый с иконкой и подсказкой;
    реальные приложения («Раздел ИИ») рендерятся той же единой разметкой
    .ai-nav-group с иконками; дубликаты ModelAdmin-строк в инструменты не
    добавляются."""

    def setUp(self):
        self.factory = RequestFactory()
        self.superuser = get_user_model().objects.create_superuser(
            username="nav_admin", password="x", email="n@t.com",
        )

    def _each_context(self, user):
        from ai.admin.site import ai_admin_site
        request = self.factory.get("/ai/admin/")
        request.user = user
        request.session = {}
        return ai_admin_site.each_context(request)

    def test_tool_groups_present_and_ordered(self):
        ctx = self._each_context(self.superuser)
        tools = [
            app for app in ctx["available_apps"]
            if app["app_label"].startswith("ai-tools")
            or app["app_label"] == "ai-pinned"
        ]
        self.assertEqual(
            [a["name"] for a in tools],
            ["Закреплено", "Промпты", "ARM", "Диагностика", "Система"],
        )

    def test_tool_models_carry_icon_and_hint(self):
        ctx = self._each_context(self.superuser)
        arm = next(a for a in ctx["available_apps"] if a["name"] == "ARM")
        solve = next(m for m in arm["models"] if m["object_name"] == "AiArmSolve")
        self.assertTrue(solve["icon"])
        self.assertTrue(solve["hint"])

    def test_no_duplicate_modeladmin_rows_in_tools(self):
        """Инструменты не дублируют реальные ModelAdmin-строки: «Промпты»
        живут в группе «Раздел ИИ»; «Настройка ИИ-приложения» закреплена
        отдельным пунктом и из «Раздела ИИ» убрана — дубля нет нигде."""
        ctx = self._each_context(self.superuser)
        tool_object_names = {
            m["object_name"]
            for app in ctx["available_apps"]
            if app["app_label"].startswith("ai-tools")
            or app["app_label"] == "ai-pinned"
            for m in app["models"]
        }
        self.assertNotIn("Prompt", tool_object_names)
        self.assertIn("AiAppSettings", tool_object_names)
        ai_app = next(a for a in ctx["available_apps"] if a["app_label"] == "ai")
        self.assertNotIn(
            "AIAppSettings", [m["object_name"] for m in ai_app["models"]],
        )

    def test_app_list_override_renders_groups_before_real_apps(self):
        """Наш оверрайд admin/app_list.html рендерит группы инструментов выше
        реальных приложений, ЕДИНОЙ разметкой .ai-nav-group с иконками;
        Поиск ошибки скрыт."""
        from django.template.loader import render_to_string
        ctx = self._each_context(self.superuser)
        request = self.factory.get("/ai/admin/")
        request.user = self.superuser
        request.session = {}
        html = render_to_string("admin/app_list.html", {
            "app_list": ctx["available_apps"],
            "request": request,
            "show_changelinks": False,
        })
        self.assertIn("ai-nav-group", html)
        self.assertIn('class="ai-nav-icon"', html)
        # Инструменты идут раньше реальных приложений («Раздел ИИ»).
        self.assertLess(html.find("Пакетное решение"), html.find("Раздел ИИ"))
        self.assertNotIn("Поиск ошибки (ARM)", html)

    def test_real_apps_render_in_unified_style(self):
        """«Раздел ИИ» рендерится той же разметкой, что и группы инструментов:
        div.ai-nav-group + строки .ai-nav-item с иконками и «+» добавления,
        без стоковых таблиц (th[scope=row] больше нет — их фильтровал только
        штатный nav_sidebar.js)."""
        from django.template.loader import render_to_string
        ctx = self._each_context(self.superuser)
        request = self.factory.get("/ai/admin/")
        request.user = self.superuser
        request.session = {}
        html = render_to_string("admin/app_list.html", {
            "app_list": ctx["available_apps"],
            "request": request,
            "show_changelinks": False,
        })
        self.assertIn('ai-nav-group app-ai', html)
        self.assertIn('model-aiappsettings', html)
        self.assertIn('model-prompt', html)
        self.assertIn('class="ai-nav-add"', html)
        self.assertNotIn('th scope="row"', html)

    def test_tools_first_in_available_apps(self):
        """available_apps: сначала все инжектированные группы инструментов
        (ai-pinned / ai-docs / ai-tools-*), затем реальные приложения —
        порядок задаётся в each_context."""
        ctx = self._each_context(self.superuser)

        def is_tool(label):
            return label == "ai-pinned" or label.startswith("ai-tools")

        labels = [a["app_label"] for a in ctx["available_apps"]]
        self.assertTrue(labels)
        first_real = next(
            i for i, label in enumerate(labels) if not label.startswith("ai-")
        )
        self.assertTrue(all(l.startswith("ai-") for l in labels[:first_real]))
        self.assertFalse(any(l.startswith("ai-") for l in labels[first_real:]))

    def test_real_model_rows_get_icons(self):
        """Строки реальных моделей украшаются иконками (_REAL_MODEL_ICONS) —
        «Раздел ИИ» в том же иконизированном стиле, что и инструменты.
        «Настройка ИИ-приложения» (AIAppSettings) в «Раздел ИИ» не входит:
        она закреплена первым пунктом навигации (группа ai-pinned)."""
        ctx = self._each_context(self.superuser)
        ai_app = next(
            a for a in ctx["available_apps"] if a["app_label"] == "ai"
        )
        icons = {m["object_name"]: m.get("icon") for m in ai_app["models"]}
        self.assertNotIn("AIAppSettings", icons)
        self.assertEqual(icons.get("Prompt"), "✎")
        self.assertTrue(icons.get("ExternalDLAccount"))
        pinned = next(
            a for a in ctx["available_apps"] if a["app_label"] == "ai-pinned"
        )
        self.assertEqual(pinned["models"][0]["object_name"], "AiAppSettings")
        self.assertEqual(pinned["models"][0]["icon"], "⚙")


# ===================================================================
# «Настройки ИИ-приложения»: таблица последних 5 запросов (по ТЗ)
# ===================================================================

class RecentLogRowsTests(TestCase):
    """build_recent_log_rows: последние N записей журнала для встраивания в
    страницу настроек — лимит, порядок (свежие сверху), права (только свои
    для prompt_developer) и ссылка на поиск по всему журналу."""

    def setUp(self):
        self.factory = RequestFactory()
        self.superuser = get_user_model().objects.create_superuser(
            username="recent_admin", password="x", email="r@t.com",
        )
        self.developer = get_user_model().objects.create_user(
            username="recent_dev", password="x",
        )
        make_prompt_developer(self.developer)
        base = timezone.now()
        self.logs = [
            AIRequestLog.objects.create(
                user=self.superuser, source="chat", mode="chat",
                message=f"msg-{i}", status=AIRequestLog.STATUS_SUCCESS,
                sent_at=base - timezone.timedelta(minutes=10 - i),
            )
            for i in range(7)
        ]

    def _request(self, user):
        request = self.factory.get("/ai/admin/ai/aiappsettings/1/change/")
        request.user = user
        request.session = {}
        return request

    def test_limit_and_order(self):
        from ai.admin.request_logs import build_recent_log_rows
        data = build_recent_log_rows(self._request(self.superuser), limit=5)
        self.assertEqual(len(data["recent_logs"]), 5)
        self.assertEqual(data["recent_logs_limit"], 5)
        expected = [log.id for log in reversed(self.logs)][:5]
        self.assertEqual([r["id"] for r in data["recent_logs"]], expected)

    def test_search_url_and_flag(self):
        from ai.admin.request_logs import build_recent_log_rows
        data = build_recent_log_rows(self._request(self.superuser), limit=5)
        self.assertTrue(data["can_view_logs"])
        self.assertEqual(data["logs_search_url"], "/ai/admin/ai/request_logs/")

    def test_developer_sees_only_own_logs(self):
        from ai.admin.request_logs import build_recent_log_rows
        own = AIRequestLog.objects.create(
            user=self.developer, source="chat", mode="chat", message="dev own",
            status=AIRequestLog.STATUS_SUCCESS, sent_at=timezone.now(),
        )
        data = build_recent_log_rows(self._request(self.developer), limit=5)
        ids = [r["id"] for r in data["recent_logs"]]
        self.assertIn(own.id, ids)
        self.assertNotIn(self.logs[-1].id, ids)

    def test_settings_change_form_renders_table(self):
        from ai.admin.models import AIAppSettingsAdmin
        from ai.admin.site import ai_admin_site
        from ai.models import AIAppSettings
        solo = AIAppSettings.get_solo()
        model_admin = AIAppSettingsAdmin(AIAppSettings, ai_admin_site)
        request = self._request(self.superuser)
        response = model_admin.changeform_view(request, object_id=str(solo.pk))
        html = response.render().content.decode()
        self.assertIn("Последние запросы", html)
        self.assertIn("Поиск по всему журналу запросов", html)
        self.assertIn(str(self.logs[-1].id), html)

    def test_changeform_view_passes_recent_logs_context(self):
        from ai.admin.models import AIAppSettingsAdmin
        from ai.admin.site import ai_admin_site
        from ai.models import AIAppSettings
        solo = AIAppSettings.get_solo()
        model_admin = AIAppSettingsAdmin(AIAppSettings, ai_admin_site)
        request = self._request(self.superuser)
        response = model_admin.changeform_view(request, object_id=str(solo.pk))
        self.assertEqual(response.status_code, 200)
        self.assertIn("recent_logs", response.context_data)
        self.assertIn("logs_search_url", response.context_data)


# ===================================================================
# Журнал запросов: поиск по всему журналу (q + id) — по ТЗ
# ===================================================================

class RequestLogSearchTests(TestCase):
    """Фильтры admin_request_logs_view: режим, модель, пользователь,
    период «С»/«По». Устаревшие GET-параметры (``q``, ``id``, ``status``,
    ``task``, ``source``) игнорируются — соответствующие поля убраны из
    формы журнала."""

    def setUp(self):
        self.factory = RequestFactory()
        self.superuser = get_user_model().objects.create_superuser(
            username="search_admin", password="x", email="s@t.com",
        )
        self.log_a = AIRequestLog.objects.create(
            user=self.superuser, source="chat", mode="chat",
            message="найди ошибку в коде", response_text="ок",
            status=AIRequestLog.STATUS_SUCCESS, sent_at=timezone.now(),
        )
        self.log_b = AIRequestLog.objects.create(
            user=self.superuser, source="arm", mode="batch_solve",
            message="другой запрос", response_text="ответ с меткой ZEBRA",
            model_names=["FakeModel"],
            user_full_name="Иван Тестов",
            status=AIRequestLog.STATUS_SUCCESS,
            sent_at=timezone.now() - timedelta(days=3),
        )

    def _list(self, params, user=None):
        from ai.admin.request_logs import admin_request_logs_view
        request = self.factory.get("/ai/admin/ai/request_logs/", params)
        request.user = user or self.superuser
        request.session = {}
        response = admin_request_logs_view(request)
        return list(response.context_data["page_obj"].object_list)

    def test_mode_filter(self):
        rows = self._list({"mode": "batch_solve"})
        self.assertIn(self.log_b, rows)
        self.assertNotIn(self.log_a, rows)

    def test_model_filter(self):
        # JSON-lookup ``contains`` не поддерживается sqlite — на Postgres
        # (docker-compose) фильтр работает; здесь же просто не роняем SUITE.
        from django.db import connection as db_connection
        if db_connection.vendor == "sqlite":
            self.skipTest("JSON contains не поддерживается sqlite")
        rows = self._list({"model": "FakeModel"})
        self.assertIn(self.log_b, rows)
        self.assertNotIn(self.log_a, rows)

    def test_user_filter_matches_full_name(self):
        rows = self._list({"user": "Тестов"})
        self.assertEqual(rows, [self.log_b])

    def test_date_range_filters(self):
        today = timezone.now().date()
        rows = self._list({"date_from": today.isoformat()})
        self.assertEqual(rows, [self.log_a])
        rows = self._list({"date_to": (today - timedelta(days=1)).isoformat()})
        self.assertEqual(rows, [self.log_b])

    def test_legacy_params_are_ignored(self):
        """q/id/status/task/source не применяются: ответ 200, список полон."""
        rows = self._list({
            "q": "ошибк", "id": str(self.log_a.id),
            "status": "success", "task": "7101", "source": "chat",
        })
        self.assertEqual(rows, [self.log_a, self.log_b])

    def _view(self, params=None, xhr=False):
        from ai.admin.request_logs import admin_request_logs_view
        extra = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"} if xhr else {}
        request = self.factory.get("/ai/admin/ai/request_logs/", params or {}, **extra)
        request.user = self.superuser
        request.session = {}
        return admin_request_logs_view(request)

    def test_xhr_returns_table_template_and_slim_context(self):
        """AJAX-замена таблицы рендерит ТОЛЬКО партиал, и тот читает из
        контекста page_obj/rows/filters_query/moscow_tz — heavy-переменные
        (each_context, choices, filters) на каждый keystroke не собираются."""
        response = self._view({"mode": "chat"}, xhr=True)
        self.assertEqual(response.template_name, "admin/ai/request_logs_table.html")
        for key in ("page_obj", "rows", "filters_query",
                    "moscow_tz"):
            self.assertIn(key, response.context_data)
        for gone in ("filters", "model_choices", "mode_choices",
                     "arm_actions", "own_logs_only", "title"):
            self.assertNotIn(gone, response.context_data)

    def test_full_page_keeps_heavy_context(self):
        """Полная страница (не-XHR) не изменилась: heavy-контекст собирается."""
        response = self._view()
        self.assertEqual(response.template_name, "admin/ai/request_logs.html")
        for key in ("page_obj", "rows", "filters_query",
                    "moscow_tz", "filters", "model_choices", "mode_choices"):
            self.assertIn(key, response.context_data)

    def test_list_rows_defer_response_text(self):
        """response_text — самая тяжёлая колонка (были AJAX-ответы по МБ), в
        таблицу он больше не вшивается (ленивый detail-json) — его не таскаем
        из БД. message НЕ defer: по нему парсится run-id batch-записей."""
        response = self._view()
        rows = list(response.context_data["page_obj"].object_list)
        self.assertTrue(rows)
        self.assertTrue(all(row.get_deferred_fields() == {"response_text"} for row in rows))

    def test_filters_echoed_in_context(self):
        from ai.admin.request_logs import admin_request_logs_view
        request = self.factory.get(
            "/ai/admin/ai/request_logs/", {"mode": "chat", "user": "Иван"},
        )
        request.user = self.superuser
        request.session = {}
        response = admin_request_logs_view(request)
        self.assertEqual(response.context_data["filters"]["mode"], "chat")
        self.assertEqual(response.context_data["filters"]["user"], "Иван")
        self.assertNotIn("q", response.context_data["filters"])
        self.assertNotIn("id", response.context_data["filters"])


# ===================================================================
# Детал batch-лога «Пакетное решение (ARM)»: только таблица результатов
# ===================================================================

class BatchLogDetailTemplateTests(TestCase):
    """Детальная страница batch-solve записи журнала показывает ТОЛЬКУ
    таблицу результатов — ту же, что на /arm/solve/ после прогона (общий
    partial _ai_batch_results.html): без метаданных и блоков «текст
    запроса/ответа». Обычные записи сохраняют прежний расширенный
    layout."""

    def setUp(self):
        from ai.models import AIModelTestRun, AIModelTestResult, Task
        self.factory = RequestFactory()
        self.superuser = get_user_model().objects.create_superuser(
            username="bd_admin", password="x", email="bd@t.com",
        )
        self.lang = ProgrammingLanguage.objects.create(language_name="Pascal")
        self.topic = Topic.objects.create(
            topic_name_ru="Линейные", programming_language=self.lang,
        )
        task = Task.objects.create(
            node_id=7101, task_id=7201, name="BD", statement="s",
            topic=self.topic, programming_language=self.lang,
            file_extension=".pas",
        )
        self.run_id = "c" * 32
        test_run = AIModelTestRun.objects.create(
            run_id=self.run_id,
            run_type=AIModelTestRun.RUN_TYPE_BATCH,
            status=AIModelTestRun.STATUS_COMPLETED,
            course_id=1450,
        )
        AIModelTestResult.objects.create(
            run=test_run, task=task,
            model_key="FakeModel", model_title="FakeModel",
            status="ok", verdict="solved",
            duration_seconds=2.0, tokens=7,
            short_response="ok", raw_response="raw", code="code",
            dl_comment="c", dl_queue_id=1,
            file_extension_snapshot=".pas",
            topic_name_snapshot="Линейные", prog_lang_snapshot="Pascal",
        )
        self.batch_log = AIRequestLog.objects.create(
            user=self.superuser, source="arm", mode="batch_solve",
            message=f"Batch solve run {self.run_id}",
            status=AIRequestLog.STATUS_SUCCESS, sent_at=timezone.now(),
        )
        self.plain_log = AIRequestLog.objects.create(
            user=self.superuser, source="chat", mode="chat",
            message="обычный запрос", response_text="обычный ответ",
            status=AIRequestLog.STATUS_SUCCESS, sent_at=timezone.now(),
        )

    def _detail(self, log):
        from ai.admin.request_logs import admin_request_log_detail_view
        request = self.factory.get(f"/ai/admin/ai/request_logs/{log.id}/")
        request.user = self.superuser
        request.session = {}
        response = admin_request_log_detail_view(request, log.id)
        self.assertEqual(response.status_code, 200)
        return response.render().content.decode()

    def _batch_detail_js(self):
        """JS детальной страницы журнала — вынесен в static."""
        from pathlib import Path
        return (
            Path(__file__).resolve().parent.parent / "static" / "admin" / "ai"
            / "request_logs_detail" / "index.js"
        ).read_text(encoding="utf-8")

    def test_batch_detail_shows_only_results_table(self):
        html = self._detail(self.batch_log)
        # Скрипт развёртки вынесен в static (шаблон — только контейнер),
        # поэтому маркер ищем в файле, а не в отрендеренном HTML.
        self.assertIn("ArmBatchResults.init", self._batch_detail_js())
        self.assertIn("результаты прогона", html)
        # Метаданные и тексты запроса/ответа НЕ показываем.
        self.assertNotIn("Кто отправлял", html)
        self.assertNotIn("Текст, который отправил пользователь", html)
        self.assertNotIn("Текст, который ответила модель", html)

    def test_batch_detail_rerun_left_of_xlsx_same_style(self):
        """«Повторить прогон» вставляется скриптом карточки слева от кнопки
        «Скачать результаты (XLSX)» (тот же стиль .arm-button), «Назад к
        логам» — отдельной строкой ссылок, не в стиле .default."""
        html = self._detail(self.batch_log)
        js = self._batch_detail_js()
        self.assertIn('id="resend-btn" class="arm-button"', html)
        self.assertIn("group.className = 'arm-head-actions'", js)
        self.assertIn("group.appendChild(built.downloadBtn)", js)
        # Обе кнопки группы нормализованы до одного размера (button vs a) —
        # правило живёт в вынесенном CSS карточки.
        from pathlib import Path
        css = (
            Path(__file__).resolve().parent.parent / "static" / "admin" / "ai"
            / "batch_results" / "index.css"
        ).read_text(encoding="utf-8")
        self.assertIn(".arm-head-actions .arm-button {", css)
        self.assertNotIn('id="resend-btn" class="default"', html)
        self.assertIn("← Назад к логам", html)

    def test_plain_detail_keeps_full_layout(self):
        html = self._detail(self.plain_log)
        self.assertIn("Текст, который отправил пользователь", html)
        self.assertIn("Текст, который ответила модель", html)
        self.assertIn("Кто отправлял", html)
        self.assertNotIn("ArmBatchResults.init", html)


# ===================================================================
# ARM solve: регрессия ключа тем (serialize_topic → programming_language)
# ===================================================================

class ArmSolveTopicsKeyTests(TestCase):
    """Темы в контексте /arm/solve/ обязаны нести ключ сериализатора
    serialize_topic (``programming_language``): JS arm_solve.html фильтрует
    темы по ``langIds.has(String(t.programming_language))`` (объединение тем
    ВСЕХ выбранных языков) — чтение другого ключа оставляет селектор тем
    пустым и ломает привязки «Промпты по умолчанию» (регрессия из
    удалённой страницы arm_find_error.html)."""

    def setUp(self):
        self.factory = RequestFactory()
        self.superuser = get_user_model().objects.create_superuser(
            username="fe_admin", password="x", email="fe@t.com",
        )
        self.lang = ProgrammingLanguage.objects.create(language_name="Pascal")
        self.topic = Topic.objects.create(
            topic_name_ru="Массивы", programming_language=self.lang,
        )

    def test_view_topics_carry_programming_language_key(self):
        from ai.admin.arm import admin_arm_solve_view
        request = self.factory.get("/ai/admin/arm/solve/")
        request.user = self.superuser
        request.session = {}
        response = admin_arm_solve_view(request)
        self.assertEqual(response.status_code, 200)
        topics = response.context_data["topics"]
        self.assertTrue(topics)
        self.assertEqual(topics[0]["programming_language"], self.lang.id)
        self.assertNotIn("programming_language_id", topics[0])

    def test_js_filltopics_matches_serializer_key(self):
        from pathlib import Path
        # JS страницы вынесен в static (шаблон — только разметка).
        src = (
            Path(__file__).resolve().parent.parent / "static" / "admin" / "ai"
            / "arm_solve" / "index.js"
        ).read_text(encoding="utf-8")
        # Темы фильтруются по ключу сериализатора через Set (объединение
        # тем всех выбранных языков).
        self.assertIn(
            "langIds.has(String(t.programming_language))", src,
        )
        self.assertNotIn("t.programming_language_id", src)


class ArmSolveTemplateMarkersTests(SimpleTestCase):
    """Точечные регрессии шаблонов /arm/solve/ по коммитам 2026-10-08.

    - POST «Перезапустить нерешённые пары» обязан слать X-CSRFToken
      (иначе admin_view отвечает 403 «CSRF token missing» — коммит
      bcb1099 «исправлениее истекшей сессии»);
    - AJAX-парсеры обязаны отличать редирект на страницу логина
      (HTML при протухшей сессии) от JSON-ответа — внятное сообщение
      «Сессия истекла…» вместо «Unexpected token '<'»;
    - кнопка «Повторить прогон» восстанавливает свою подпись и rerun-лог
      («Batch solve rerun …») ведёт в rerun-arm, а не в resend.
    """

    def _source(self, name):
        from django.template.loader import get_template
        return get_template(name).template.source

    @staticmethod
    def _static_source(*parts):
        """Источник вынесенного статического файла (CSS/JS админки)."""
        from pathlib import Path
        path = Path(__file__).resolve().parent.parent.joinpath("static", *parts)
        return path.read_text(encoding="utf-8")

    def test_rerun_pairs_post_sends_csrf_token(self):
        src = self._static_source("admin", "ai", "arm_solve", "index.js")
        i = src.find("await fetch(rerunPairsUrl")
        self.assertNotEqual(i, -1, "блок POST rerun-pairs не найден")
        self.assertIn("'X-CSRFToken': getCsrfToken()", src[i:i + 400])

    def test_arm_solve_has_friendly_expired_session_parser(self):
        src = self._static_source("admin", "ai", "arm_solve", "index.js")
        self.assertIn("function parseJsonResponse", src)
        self.assertIn("Сессия истекла", src)

    def test_batch_results_body_fetch_has_expired_session_parser(self):
        """Ленивое тело результата (partial используется на /arm/solve/ и в
        деталях журнала): не-JSON ответ (страница логина) → внятная ошибка.
        Чанковая загрузка raw_response и агрегатор состояния — на месте."""
        src = self._static_source("admin", "ai", "batch_results", "index.js")
        self.assertIn("function parseJsonResponse", src)
        self.assertIn("Сессия истекла", src)
        i = src.find("function fetchResultChunk")
        self.assertNotEqual(i, -1)
        self.assertIn(".then(parseJsonResponse)", src[i:i + 800])
        self.assertIn("function loadResultBodyState", src)

    def test_log_detail_resend_session_label_and_rerun_route(self):
        src = self._static_source("admin", "ai", "request_logs_detail", "index.js")
        # Протухшая сессия — внятное сообщение вместо JSON.parse-мусора.
        self.assertIn("Сессия истекла", src)
        # Подпись кнопки восстанавливается из отрендеренной, а не литералом.
        self.assertIn("originalLabel", src)
        self.assertIn("btn.textContent = originalLabel", src)
        # rerun-лог ведёт в rerun-arm (не в resend одной модели).
        self.assertIn("logMessage.indexOf('Batch solve rerun')", src)


# ===================================================================
# Разделение промптов по режимам (Prompt.mode)
# ===================================================================

class PromptModeTests(TestCase):
    """Prompt.mode: режим «Реши задачу»/«В чём ошибка» — дефолт, массовые
    действия админки, страховка композитора, ARM-фильтры."""

    def setUp(self):
        self.factory = RequestFactory()
        self.user = get_user_model().objects.create_user(username="pm_user", password="x")
        self.superuser = get_user_model().objects.create_superuser(
            username="pm_admin", password="x", email="pm@t.com",
        )
        self.lang = ProgrammingLanguage.objects.create(language_name="Pascal")
        self.topic = Topic.objects.create(topic_name_ru="Массивы", programming_language=self.lang)
        self.solve_prompt = Prompt.objects.create(
            prompt_name_ru="Solve prep", prompt_text_ru="РЕШАЙ ПО ШАГАМ",
            topic=self.topic, owner=self.user,
        )
        self.find_error_prompt = Prompt.objects.create(
            prompt_name_ru="Find bug prep", prompt_text_ru="НАЙДИ ОШИБКУ",
            topic=self.topic, owner=self.user,
            mode=Prompt.MODE_FIND_ERROR,
        )

    def test_prompt_mode_default_is_solve(self):
        """Промпт без явного режима — «Реши задачу» (дефолт поля)."""
        p = Prompt.objects.create(prompt_name_ru="No mode", prompt_text_ru="T")
        self.assertEqual(p.mode, Prompt.MODE_SOLVE)

    # --- Композитор: чужой preprompt не применяется ---

    async def _compose(self, type_key, prompt_id):
        composer = MessageComposer()
        data = {
            "type": type_key,
            "message": "Условие задачи",
            "code": "begin end." if type_key == "3" else "",
            "language": "Русский",
            "preprompt": prompt_id,
        }
        return await composer.compose(data)

    async def test_solve_builder_ignores_find_error_prompt(self):
        message, log_mode = await self._compose("2", self.find_error_prompt.id)
        self.assertEqual(log_mode, "solve")
        self.assertNotIn("НАЙДИ ОШИБКУ", message)
        self.assertNotIn("Промпт:", message)

    async def test_find_error_builder_ignores_solve_prompt(self):
        message, log_mode = await self._compose("3", self.solve_prompt.id)
        self.assertEqual(log_mode, "find_error")
        self.assertNotIn("Промпт:", message)

    async def test_matching_mode_prompt_is_applied(self):
        message, log_mode = await self._compose("2", self.solve_prompt.id)
        self.assertEqual(log_mode, "solve")
        self.assertIn("Промпт: РЕШАЙ ПО ШАГАМ", message)

    async def test_shared_prompt_preprompt_works_in_both_modes(self):
        """Общие промпты ('shared_<pk>') не фильтруются по режиму."""
        sp = await sync_to_async(SharedPrompt.objects.create)(
            prompt_name_ru="Common", prompt_text_ru="ОБЩИЙ ТЕКСТ",
        )
        message, _ = await self._compose("2", f"shared_{sp.id}")
        self.assertIn("Промпт: ОБЩИЙ ТЕКСТ", message)
        message, _ = await self._compose("3", f"shared_{sp.id}")
        self.assertIn("Промпт: ОБЩИЙ ТЕКСТ", message)

    # --- ARM solve: только solve-промпты ---

    def _arm_request(self, user, url="/ai/admin/arm/solve/"):
        request = self.factory.get(url)
        request.user = user
        request.session = {}
        return request

    def test_arm_solve_prompt_options_only_solve(self):
        from ai.admin.arm import admin_arm_solve_view
        response = admin_arm_solve_view(self._arm_request(self.superuser))
        options = response.context_data["prompt_options"]
        self.assertEqual(len(options), 1)
        self.assertEqual(options[0]["id"], self.solve_prompt.id)

    def test_arm_solve_start_rejects_find_error_prompt(self):
        from ai.admin.arm import admin_arm_solve_start_view
        request = self.factory.post(
            "/ai/admin/arm/solve/start/",
            data=json.dumps({
                "prompt_id": str(self.find_error_prompt.id),
                "models": ["FakeModel"], "model_keys": ["FakeModel"],
                "file_extension": ".pas",
            }),
            content_type="application/json",
        )
        request.user = self.superuser
        request.session = {}
        response = admin_arm_solve_start_view(request)
        self.assertEqual(response.status_code, 400)
        self.assertIn("не относится к режиму", json.loads(response.content)["message"])

    def test_resolve_batch_prompt_rejects_find_error_binding(self):
        """Привязка с промптом чужого режима не подставляется в пакетное решение."""
        from ai.arm_runner import _resolve_batch_prompt
        from ai.models import Task
        task = Task.objects.create(name="T1", node_id=1, topic=self.topic)
        ArmPromptBinding.objects.create(
            programming_language=self.lang, topic=self.topic,
            mode=ArmPromptBinding.MODE_SOLVE, prompt=self.find_error_prompt,
        )
        self.assertIsNone(_resolve_batch_prompt(task, self.lang.id, {}))

    # --- «Промпты по умолчанию»: промпт обязан совпадать режимом ---

    def test_prompt_defaults_save_rejects_mode_mismatch(self):
        from ai.admin.prompt_defaults import admin_prompt_defaults_view
        request = self.factory.post(
            "/ai/admin/prompt-defaults/",
            data={
                "action": "save",
                "language_id": str(self.lang.id),
                "topic_id": str(self.topic.id),
                "mode": "solve",
                "prompt_id": str(self.find_error_prompt.id),
            },
        )
        request.user = self.superuser
        response = admin_prompt_defaults_view(request)
        self.assertEqual(response.status_code, 400)
        self.assertIn("не относится к выбранному режиму", json.loads(response.content)["error"])

    # --- Админка: массовое назначение режима ---

    def test_admin_bulk_set_mode_actions(self):
        from ai.admin.models import PromptAdmin
        admin = PromptAdmin(Prompt, AdminSite())
        request = self.factory.get("/ai/admin/")
        request.user = self.superuser
        # message_user требует хранилище сообщений, которого у RequestFactory нет.
        request._messages = CookieStorage(request)
        queryset = Prompt.objects.filter(pk=self.solve_prompt.pk)
        admin.set_mode_find_error(request, queryset)
        self.solve_prompt.refresh_from_db()
        self.assertEqual(self.solve_prompt.mode, Prompt.MODE_FIND_ERROR)
        admin.set_mode_solve(request, queryset)
        self.solve_prompt.refresh_from_db()
        self.assertEqual(self.solve_prompt.mode, Prompt.MODE_SOLVE)

    # --- Старые URL /ai/admin/arm/find-error… удалены ---

    def test_arm_find_error_urls_removed(self):
        from django.urls import reverse
        from django.urls.exceptions import NoReverseMatch
        for name in ("ai_arm_find_error", "ai_arm_find_error_start", "ai_arm_find_error_status"):
            with self.assertRaises(NoReverseMatch):
                reverse(name)

    def test_arm_find_error_pages_404(self):
        from ai.admin.urls import get_ai_admin_urls
        from django.urls import resolve, Resolver404
        # URL-паттерны отсутствуют в таблице маршрутов админки.
        patterns = [str(p.pattern) for p in get_ai_admin_urls()]
        self.assertFalse(any("find-error" in p for p in patterns))


# ===================================================================
# fetch(form.action) vs <input name="action">: затенение DOM-свойств формы
# ===================================================================

class FormActionShadowingTests(SimpleTestCase):
    """Именованные поля формы затеняют DOM-свойства: в pdForm лежит
    <input name="action">, поэтому ``form.action`` возвращал сам инпут, и
    POST уходил на ``/ai/admin/prompt-defaults/[object HTMLInputElement]``
    (404 в access-логах), а parseJsonResponse показывала «Сессия истекла…».
    Все AJAX-отправки обязаны читать адрес формы через
    ``form.getAttribute('action')`` — атрибут затенением не страдает."""

    def test_prompt_defaults_post_url_uses_get_attribute(self):
        from django.conf import settings
        import os
        # JS страницы вынесен в static (шаблон — только разметка).
        js_path = os.path.join(
            settings.BASE_DIR, "static", "admin", "ai", "prompt_defaults", "index.js",
        )
        with open(js_path, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("form.getAttribute('action')", src)
        self.assertNotIn("fetch(form.action", src)

    def test_request_logs_rerun_url_uses_get_attribute(self):
        """Журнал разрезан (2026-10): rerun-форма и её JS живут в партиале
        admin/ai/_ai_batch_detail.html + статическом index.js — маркер
        проверяем там."""
        from django.template.loader import get_template
        from django.conf import settings

        src = get_template("admin/ai/_ai_batch_detail.html").template.source
        self.assertIn("ai-batch-rerun-form", src)
        self.assertNotIn("fetch(form.action", src)
        js_path = os.path.join(
            settings.BASE_DIR, "static", "admin", "ai", "request_logs", "index.js",
        )
        with open(js_path, encoding="utf-8") as fh:
            js = fh.read()
        self.assertIn("rerunForm.getAttribute('action')", js)
        self.assertNotIn("fetch(form.action", js)


# ===================================================================
# Селектор моделей: статистика не должна залипать в закрытом селекте
# ===================================================================

class ModelStatsSuffixStripTests(SimpleTestCase):
    """Суффикс статистики («Model — 12.3с·45%») виден только в развёрнутом
    списке. Закрытие списка «клик мимо» не даёт ни change (значение то же),
    ни blur (фокус остаётся на селекте) — суффикс висел в закрытом селекте
    до первого клика по странице. Регрессионный гард на источник: слушатели
    document pointerdown/click вне селекта обязаны оставаться в
    initModelSortSelector (static/admin/js/ai-common.js)."""

    def _source(self):
        from pathlib import Path
        path = (
            Path(__file__).resolve().parent.parent
            / "static" / "admin" / "js" / "ai-common.js"
        )
        return path.read_text(encoding="utf-8")

    def test_strip_listeners_present(self):
        src = self._source()
        self.assertIn(
            "document.addEventListener('pointerdown', function (event) {\n"
            "        if (!modelSelect.contains(event.target)) stripModelOptionSuffixes();",
            src,
        )
        self.assertIn(
            "document.addEventListener('click', function (event) {\n"
            "        var onModelSelect = modelSelect.contains(event.target);",
            src,
        )

    def test_open_click_still_applies_suffixes(self):
        """Открывающий клик по селекту не должен снимать суффиксы: флаг\n        selectPointerActive различает реальный клик и синтетический (после\n        выбора опции в нативном списке)."""
        src = self._source()
        self.assertIn("selectPointerActive = true", src)
        self.assertIn("if (!onModelSelect || !selectPointerActive)", src)


# ===================================================================
# dl.gsu.by требует courseId в get-task-info (иначе 400 → наш 502)
# ===================================================================

class TaskInfoCourseIdTests(TestCase):
    """REST API dl.gsu.by стал требовать courseId в GET /restapi/get-task-info:
    без него возвращается 400 Bad Request, который _raise_for_status маппит в
    DLServerError → наш 502 («Bad Gateway», 2026-09-14, nodeId=2606747).
    get_task_info_view обязан передавать course_id: явный параметр запроса
    имеет приоритет, иначе courseID текущего курса из validated user_info
    (кладёт ExternalAuthMiddleware на каждый запрос)."""

    def setUp(self):
        self.factory = RequestFactory()
        self.superuser = get_user_model().objects.create_superuser(
            username="ti_admin", password="***", email="ti@t.com",
        )

    def _request(self, path="/ai/api/task-info/", user_info=None, **params):
        from ai.views import get_task_info_view
        request = self.factory.get(path, params)
        request.user = self.superuser
        request.session = {}
        if user_info is not None:
            request.user_info = user_info
        return get_task_info_view(request)

    @patch("ai.views.fetch_task_info")
    def test_course_id_taken_from_user_info(self, mock_fetch):
        mock_fetch.return_value = {"name": "t", "taskId": 1, "statement": "s"}
        response = self._request(user_info={"userId": 186638, "courseID": 1450}, nodeId="2606747")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mock_fetch.call_args.kwargs.get("course_id"), 1450)

    @patch("ai.views.fetch_task_info")
    def test_explicit_course_id_param_wins(self, mock_fetch):
        mock_fetch.return_value = {"name": "t", "taskId": 1, "statement": "s"}
        # userId в user_info обязателен: иначе _has_page_access вернёт 403.
        self._request(user_info={"userId": 186638, "courseID": 1450}, nodeId="2606747", courseId="999")
        self.assertEqual(mock_fetch.call_args.kwargs.get("course_id"), 999)

    @patch("ai.views.fetch_task_info")
    def test_no_course_id_passes_none(self, mock_fetch):
        """user_info без courseID (или вовсе без user_info) → course_id=None:
        запрос уходит как раньше (и упадёт с 400 уже на стороне DL — но
        приложение теперь честно передаёт курс, когда он известен)."""
        mock_fetch.return_value = {"name": "t", "taskId": 1, "statement": "s"}
        self._request(user_info={"userId": 186638}, nodeId="2606747")
        self.assertIsNone(mock_fetch.call_args.kwargs.get("course_id"))

    @patch("ai.views.fetch_task_info")
    def test_bad_course_id_param_falls_back_to_user_info(self, mock_fetch):
        mock_fetch.return_value = {"name": "t", "taskId": 1, "statement": "s"}
        self._request(user_info={"userId": 186638, "courseID": 1450}, nodeId="2606747", courseId="abc")
        self.assertEqual(mock_fetch.call_args.kwargs.get("course_id"), 1450)


class DLApiClientErrorLoggingTests(SimpleTestCase):
    """dl_error_response отдаёт наружу обезличенный JSON ("Server error"),
    а причина (код DL и сниппет тела) раньше терялась — диагностика апстрима
    сводилась к голому «Bad Gateway». Теперь причина пишется в лог
    (кроме штатных 401/403 — протухшая пользовательская сессия)."""

    def test_error_response_logs_cause(self):
        import logging as _logging
        from ai.dl_api_client import DLServerError, dl_error_response
        exc = DLServerError("DL API вернул ошибку (код 400)Bad Request")
        with self.assertLogs("ai.dl_api_client", level=_logging.WARNING) as captured:
            response = dl_error_response(exc)
        self.assertEqual(response.status_code, 502)
        joined = "\n".join(captured.output)
        self.assertIn("HTTP 502", joined)
        self.assertIn("код 400", joined)

    def test_auth_errors_not_logged_as_warnings(self):
        from ai.dl_api_client import DLUnauthorizedError, dl_error_response
        with self.assertNoLogs("ai.dl_api_client", level="WARNING"):
            dl_error_response(DLUnauthorizedError())

    def test_consumer_resolves_course_id_from_session(self):
        """Консьюмер берёт courseID из user_info WS-аутентификации, а при её
        отсутствии — из кеша external_user_info в Django-сессии (scope)."""
        from ai.consumers import MyConsumer
        consumer = MyConsumer()
        consumer.scope = {}
        consumer.user_info = {"courseID": 1450}
        self.assertEqual(consumer._resolve_course_id(), 1450)

        consumer.user_info = None
        consumer.scope = {
            "session": {"external_user_info": {"courseID": 777}},
        }
        self.assertEqual(consumer._resolve_course_id(), 777)

        consumer.user_info = None
        consumer.scope = {}
        self.assertIsNone(consumer._resolve_course_id())

class DocsServiceTests(SimpleTestCase):
    """ai/services/docs.py: главы, права по рангу, markdown → HTML."""

    def test_all_chapters_resolve(self):
        from ai.services.docs import all_chapters, read_chapter_markdown
        for chapter in all_chapters():
            data = read_chapter_markdown(chapter.slug)
            self.assertTrue(data["text"].strip())
            self.assertEqual(data["title"], chapter.title)
            self.assertTrue(data["filename"].endswith(".md"))

    def test_docx_chapter_boundaries(self):
        """Глава режется только по «## Инструкция для …» — соседние главы
        и приложения тестера/сисадмина в текст не попадают."""
        from ai.services.docs import read_chapter_markdown
        pd = read_chapter_markdown("prompt-developer")["text"]
        self.assertIn("## Инструкция для разработчика промптов", pd)
        self.assertNotIn("## Инструкция для суперадмина", pd)
        self.assertNotIn("Приложение", pd)
        su = read_chapter_markdown("superuser")["text"]
        self.assertIn("## Инструкция для суперадмина", su)
        self.assertNotIn("## Инструкция для пользователя", su)

    def test_render_html_contains_tables(self):
        from ai.services.docs import render_chapter_html
        rendered = render_chapter_html("developer")
        self.assertTrue(rendered["html"].lstrip().startswith("<"))
        # Техдок для разработчика насыщен таблицами — extension "tables".
        self.assertIn("<table>", rendered["html"])

    def test_visible_chapter_slugs_by_role(self):
        from ai.services.docs import visible_chapter_slugs
        self.assertEqual(
            visible_chapter_slugs(is_superuser=False, is_staff=False, is_authenticated=True),
            ["user", "prompt-developer"],
        )
        staff_slugs = visible_chapter_slugs(is_superuser=False, is_staff=True, is_authenticated=True)
        self.assertIn("developer", staff_slugs)
        self.assertNotIn("superuser", staff_slugs)
        self.assertEqual(
            visible_chapter_slugs(is_superuser=True, is_staff=True, is_authenticated=True),
            ["user", "prompt-developer", "developer", "superuser"],
        )
        self.assertEqual(
            visible_chapter_slugs(is_superuser=False, is_staff=False, is_authenticated=False),
            [],
        )

    def test_unknown_slug_raises(self):
        from ai.services.docs import DocUnavailableError, read_chapter_markdown
        with self.assertRaises(DocUnavailableError):
            read_chapter_markdown("no-such-chapter")

    def test_missing_docx_raises_friendly_error(self):
        from pathlib import Path
        from unittest.mock import patch
        from ai.services import docs as docs_module
        with patch.object(docs_module, "_base_dir", return_value=Path("/nonexistent")):
            with self.assertRaises(docs_module.DocUnavailableError):
                docs_module.read_chapter_markdown("user")


class _AdminViewRequestMixin:
    """Request с сессией, провижненным пользователем и admin_fresh_auth —
    чтобы @ai_admin_site.admin_view пропустил запрос (патчится внешний id)."""

    def _admin_request(self, user, method="get", path="/ai/admin/", data=None, content_type=None):
        from django.contrib.messages.middleware import MessageMiddleware
        from django.contrib.sessions.middleware import SessionMiddleware

        # content_type="application/json" — когда data сырой JSON-текст
        # (encode_multipart понимает только словари).
        if content_type is not None:
            request = getattr(self.factory, method)(
                path, data=data or {}, content_type=content_type,
            )
        else:
            request = getattr(self.factory, method)(path, data=data or {})
        SessionMiddleware(lambda req: None).process_request(request)
        MessageMiddleware(lambda req: None).process_request(request)
        request.user = user
        request._ai_provisioned_user = user
        request.session["admin_fresh_auth"] = True
        # admin_view оборачивает вью в csrf_protect — для RequestFactory
        # (без CsrfViewMiddleware) это всегда 403. Документированный способ
        # отключить проверку в тестах — атрибут _dont_enforce_csrf_checks.
        request._dont_enforce_csrf_checks = True
        return request

    def _view_as_group(self, name, keys):
        """Группа доступа с набором ключей (для честной эмуляции «Посмотреть как…»)."""
        from ai.models import AIAccessGroup, AIAdminGroupPermission
        group = AIAccessGroup.objects.create(name=name)
        for key in keys:
            perm, _ = AIAdminGroupPermission.objects.get_or_create(
                key=key,
                defaults={"label": key, "section": key.split(".")[0]},
            )
            group.permissions.add(perm)
        return group

    def _enter_view_as(self, request, group):
        """Включить честную эмуляцию: сессия + stash на request.user
        (admin_view проставляет stash в проде — в тесте зовём вручную)."""
        from ai.admin.guest_mode import SESSION_KEY, apply_view_as_user
        request.session[SESSION_KEY] = group.pk
        apply_view_as_user(request)

    def _leave_view_as(self, user):
        """Снять stash с ИНСТАНСА юзера: в тестах юзер переиспользуется между
        запросами (в проде Django пересоздаёт request.user на каждый запрос),
        и эмуляция иначе «прилипла» бы к следующим проверкам того же объекта."""
        from ai.admin.guest_mode import VIEW_AS_ATTR
        if hasattr(user, VIEW_AS_ATTR):
            delattr(user, VIEW_AS_ATTR)


class DocsAdminAccessTests(_AdminViewRequestMixin, TestCase):
    """Роль-гейты страниц «Документация» + скачивание .md."""

    def setUp(self):
        self.factory = RequestFactory()
        self.user_model = get_user_model()

    def _view(self, user, slug, view_name="admin_docs_view"):
        from unittest.mock import patch
        from ai.admin import docs as docs_views

        view = getattr(docs_views, view_name)
        path = f"/ai/admin/docs/{slug}/"
        if view_name == "admin_docs_view":
            request = self._admin_request(user, path=path)
            with patch("ai.admin.site.get_external_user_id_from_request", return_value="12345"):
                return view(request, slug=slug)
        request = self._admin_request(user, path=f"{path}download/")
        with patch("ai.admin.site.get_external_user_id_from_request", return_value="12345"):
            return view(request, slug=slug)

    def _pd_user(self, grant_docs=False):
        import uuid
        user = self.user_model.objects.create_user(
            username=f"pd-docs-{uuid.uuid4().hex[:8]}", password="x",
        )
        make_prompt_developer(user)
        if grant_docs:
            from ai.models import AIAccessGroup, AIAdminGroupPermission
            perm, _ = AIAdminGroupPermission.objects.get_or_create(
                key="docs.prompt_developer",
                defaults={"label": "Док «Разработчик промптов»", "section": "docs"},
            )
            group = AIAccessGroup.objects.create(name="Ключи-docs")
            group.permissions.add(perm)
            user.ai_access_groups.add(group)
        return user

    def test_prompt_developer_no_chapter_without_key(self):
        # Решение владельца 2026-10-20: документация НЕ идёт с ролью pd —
        # глава выдаётся ключом docs.prompt_developer в «Группах доступа».
        user = self._pd_user()
        self.assertEqual(self._view(user, "prompt-developer").status_code, 403)
        self.assertEqual(self._view(user, "developer").status_code, 403)
        self.assertEqual(self._view(user, "superuser").status_code, 403)
        pd_with_key = self._pd_user(grant_docs=True)
        self.assertEqual(self._view(pd_with_key, "prompt-developer").status_code, 200,
                         "ключ docs.prompt_developer открывает главу pd-юзеру")

    def test_staff_sees_developer_chapter(self):
        user = self.user_model.objects.create_user(username="staff-docs", password="x", is_staff=True)
        self.assertEqual(self._view(user, "developer").status_code, 200)
        self.assertEqual(self._view(user, "superuser").status_code, 403)

    def test_superuser_sees_all_chapters(self):
        user = self.user_model.objects.create_user(
            username="super-docs", password="x", is_superuser=True, is_staff=True,
        )
        for slug in ("prompt-developer", "developer", "superuser"):
            self.assertEqual(self._view(user, slug).status_code, 200)

    def test_view_as_group_honest_chapter_access(self):
        """Эмуляция честная: главы docs.* РЕШАЮТСЯ ключами эмулируемой
        группы (документация не идёт с ролью), супер-обход отключён."""
        from ai.admin.docs import admin_docs_view
        from unittest.mock import patch
        user = self.user_model.objects.create_user(
            username="viewas-docs", password="x", is_superuser=True, is_staff=True,
        )
        pd_group = self._view_as_group("Эмуля-PD", ["page.admin", "docs.prompt_developer"])
        staff_group = self._view_as_group("Эмуля-Doc", ["page.admin", "docs.developer"])
        request = self._admin_request(user, path="/ai/admin/docs/developer/")
        self._enter_view_as(request, pd_group)
        with patch("ai.admin.site.get_external_user_id_from_request", return_value="12345"):
            response = admin_docs_view(request, slug="developer")
        self.assertEqual(response.status_code, 403,
                         "staff-глава закрыта: ключа docs.developer у группы нет")
        with patch("ai.admin.site.get_external_user_id_from_request", return_value="12345"):
            response = admin_docs_view(request, slug="prompt-developer")
        self.assertEqual(response.status_code, 200,
                         "docs.prompt_developer у группы — глава открыта")
        request = self._admin_request(user, path="/ai/admin/docs/developer/")
        self._enter_view_as(request, staff_group)
        with patch("ai.admin.site.get_external_user_id_from_request", return_value="12345"):
            response = admin_docs_view(request, slug="developer")
        self.assertEqual(response.status_code, 200)

    def test_download_returns_markdown_attachment(self):
        user = self.user_model.objects.create_user(username="dl-docs", password="x", is_staff=True)
        response = self._view(user, "developer", view_name="admin_docs_download_view")
        self.assertEqual(response["Content-Type"], "text/markdown; charset=utf-8")
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertIn("filename*", response["Content-Disposition"])

    def test_download_denied_for_wrong_role(self):
        user = self._pd_user()
        response = self._view(user, "superuser", view_name="admin_docs_download_view")
        self.assertEqual(response.status_code, 403)


class ViewAsViewTests(_AdminViewRequestMixin, TestCase):
    """Окно выбора группы «Посмотреть как…» (ai/admin/guest_mode.py):
    GET-список (только группы с page.admin) и POST-переключатель."""

    def setUp(self):
        self.factory = RequestFactory()
        self.user_model = get_user_model()

    def _superuser(self, username="viewas-super"):
        return self.user_model.objects.create_user(
            username=username, password="x", is_superuser=True, is_staff=True,
        )

    def _select(self, user, group_id):
        """POST-переключатель; возвращает (response, request) — сессия живёт
        на запросе (в проде гоняется кукой)."""
        from unittest.mock import patch
        from ai.admin.guest_mode import admin_view_as_select_view
        request = self._admin_request(
            user, method="post", path="/ai/admin/view-as/select/",
            data={"group_id": str(group_id)},
        )
        with patch("ai.admin.site.get_external_user_id_from_request", return_value="12345"):
            response = admin_view_as_select_view(request)
        return response, request

    def test_groups_view_lists_only_admin_groups(self):
        from unittest.mock import patch
        from ai.admin.guest_mode import (
            SESSION_KEY,
            admin_view_as_groups_view,
            admin_view_as_select_view,
        )
        superuser = self._superuser()
        admin_group = self._view_as_group("Эмуля-вход", ["page.admin"])
        no_admin_group = self._view_as_group("Эмуля-безвыхода", ["page.model_status"])
        request = self._admin_request(superuser, path="/ai/admin/view-as/groups/")
        with patch("ai.admin.site.get_external_user_id_from_request", return_value="12345"):
            response = admin_view_as_groups_view(request)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        names = {g["name"]: g for g in data["groups"]}
        self.assertIn(admin_group.name, names)
        self.assertNotIn(no_admin_group.name, names,
                         "группа без page.admin не входит в админку — смотреть нечего")
        self.assertFalse(names[admin_group.name]["current"])

        # Выбор группы: сессия живёт на запросе; следующий запрос в проде
        # находит её по кукам — в тесте подсунули то же хранилище.
        select_request = self._admin_request(
            superuser, method="post", path="/ai/admin/view-as/select/",
            data={"group_id": str(admin_group.id)},
        )
        with patch("ai.admin.site.get_external_user_id_from_request", return_value="12345"):
            response = admin_view_as_select_view(select_request)
        self.assertEqual(response.status_code, 200)
        self.assertIn(SESSION_KEY, select_request.session)
        # Прямой вызов view (вне admin_view) НЕТ url_name-исключения —
        # select проставил stash на юзера; снимаем, чтобы групп-листинг
        # видел реального супера.
        self._leave_view_as(superuser)
        list_request = self._admin_request(superuser, path="/ai/admin/view-as/groups/")
        list_request.session = select_request.session
        with patch("ai.admin.site.get_external_user_id_from_request", return_value="12345"):
            response = admin_view_as_groups_view(list_request)
        data = json.loads(response.content)
        self.assertTrue(next(g for g in data["groups"] if g["id"] == admin_group.id)["current"])

    def test_select_enables_and_disable(self):
        from ai.admin.guest_mode import SESSION_KEY
        superuser = self._superuser()
        group = self._view_as_group("Эмуля-два", ["page.admin", "page.model_status"])
        response, request = self._select(superuser, group.id)
        self.assertEqual(response.status_code, 200)
        self.assertIn(SESSION_KEY, request.session)
        self.assertEqual(request.session[SESSION_KEY], group.pk)
        response, request = self._select(superuser, 0)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(SESSION_KEY, request.session)

    def test_select_unknown_group_404(self):
        response, _ = self._select(self._superuser(username="viewas-404"), 999999)
        self.assertEqual(response.status_code, 404)

    def test_get_method_forbidden(self):
        from unittest.mock import patch
        from ai.admin.guest_mode import admin_view_as_select_view
        request = self._admin_request(
            self._superuser(), method="get", path="/ai/admin/view-as/select/",
        )
        with patch("ai.admin.site.get_external_user_id_from_request", return_value="12345"):
            response = admin_view_as_select_view(request)
        self.assertEqual(response.status_code, 405)

    def test_non_superuser_forbidden(self):
        from unittest.mock import patch
        from ai.admin.guest_mode import admin_view_as_select_view
        user = self.user_model.objects.create_user(
            username="viewas-staff", password="x", is_staff=True,
        )
        request = self._admin_request(
            user, method="post", path="/ai/admin/view-as/select/",
            data={"group_id": "0"},
        )
        with patch("ai.admin.site.get_external_user_id_from_request", return_value="12345"):
            response = admin_view_as_select_view(request)
        self.assertEqual(response.status_code, 403)

    def test_honest_checks_fail_for_viewed_group_user(self):
        """Изнутри эмуляции is_superuser_user/is_staff_or_superuser → False."""
        from ai.admin.permissions import is_staff_or_superuser, is_superuser_user
        superuser = self._superuser()
        group = self._view_as_group("Эмуля-три", ["page.admin"])
        request = self._admin_request(superuser, path="/ai/admin/")
        self._enter_view_as(request, group)
        self.assertFalse(is_staff_or_superuser(request.user))
        self.assertFalse(is_superuser_user(request.user))
        # Ключи честные: только группа (+baseline при page.admin).
        from ai.admin.permissions import user_access_keys
        self.assertIn("page.admin", user_access_keys(request.user))
        self.assertIn("page.arm_solve", user_access_keys(request.user),
                      "baseline выдаётся в эмуляции тоже")
        self.assertNotIn("page.model_status", user_access_keys(request.user))
        superuser2 = self._superuser(username="viewas-super-two")
        request = self._admin_request(superuser2, path="/ai/admin/")
        from ai.admin.permissions import user_access_keys as _uak
        # superuser2 БЕЗ эмуляции: полный набор ключей супер-ветки —
        # это не противоречит честности, stash здесь уже снят.

    def test_role_membership_resolved_from_stash_group(self):
        """Ролевые проверки в эмуляции смотрят ВЫБРАННУЮ группу, а не
        реальныеmembership'ы юзера: супер в группе «PD-Эмуля» — не pd;
        кто угодно в эмуляции «Разработчик промптов» — pd."""
        from ai.admin.permissions import is_prompt_developer_user
        from ai.models import AIAccessGroup
        user = self.user_model.objects.create_user(
            username="viewas-role", password="x", is_superuser=True, is_staff=True,
        )
        make_prompt_developer(user)
        other = self._view_as_group("Эмуля-role-другая", ["page.admin"])
        request = self._admin_request(user, path="/ai/admin/")
        self._enter_view_as(request, other)
        self.assertFalse(is_prompt_developer_user(request.user),
                         "реальное членство в эмуляции игнорируется")
        builtin_pd = AIAccessGroup.objects.get_or_create(
            name="Разработчик промптов", defaults={"is_builtin": True},
        )[0]
        request = self._admin_request(user, path="/ai/admin/")
        self._enter_view_as(request, builtin_pd)
        self.assertTrue(is_prompt_developer_user(request.user),
                        "в эмуляции роль = выбранная группа, своё членство неважно")

    def test_view_as_group_name_flag_hides_superuser_tools(self):
        """Реальные группы юзера игнорируются: супер с ключом page.updates
        из личной группы в эмуляции «Разработчик промптов» Обновлений не видит."""
        from ai.admin.permissions import can_access_updates
        group = self._view_as_group("Эмуля-PD-два", ["page.admin"])
        superuser2 = self.user_model.objects.create_user(
            username="viewas-mixed", password="x", is_superuser=True, is_staff=True,
        )
        personal = self._view_as_group("Личная-архив", ["page.updates"])
        superuser2.ai_access_groups.add(personal)
        before = list(superuser2.ai_access_groups.all())
        request = self._admin_request(superuser2, path="/ai/admin/")
        self._enter_view_as(request, group)
        self.assertFalse(can_access_updates(request),
                         "в эмуляции ключи только группы — личная page.updates не в счёт")
        self.assertEqual(before, list(superuser2.ai_access_groups.all()),
                         "реальные группы не тронуты")


class ViewAsContextTests(_AdminViewRequestMixin, TestCase):
    """each_context в честной эмуляции «Посмотреть как…»: доступ РОВНО
    по ключам эмулируемой группы (+baseline при page.admin), супер-обход
    отключён; тур онбординга при этом не показывается."""

    def setUp(self):
        self.factory = RequestFactory()
        self.user_model = get_user_model()

    def _context(self, user, group=None):
        from unittest.mock import patch
        from ai.admin.site import ai_admin_site
        request = self._admin_request(user, path="/ai/admin/")
        if group is not None:
            self._enter_view_as(request, group)
        with patch("ai.admin.site.get_external_user_id_from_request", return_value="12345"):
            return ai_admin_site.each_context(request)

    def _superuser(self):
        return self.user_model.objects.create_user(
            username="ctx-super", password="x", is_superuser=True, is_staff=True,
        )

    def _doc_tool_names(self, context):
        doc_app = next(
            (app for app in context["available_apps"] if app["app_label"] == "ai-docs"),
            None,
        )
        return [m["name"] for m in doc_app["models"]] if doc_app else []

    def test_normal_superuser_context(self):
        context = self._context(self._superuser())
        self.assertFalse(context["is_viewing_as_guest"])
        self.assertTrue(context["real_super_user"])
        self.assertFalse(context["view_as_group_name"])
        self.assertTrue(context["show_model_status_link"])
        self.assertTrue(context["show_test_console_link"])
        self.assertEqual(
            self._doc_tool_names(context),
            ["Разработчик промптов", "Разработчик", "Суперюзер"],
        )

    def test_view_as_group_shows_only_group_pages(self):
        """Группа с page.admin + модельным статусом: только её страницы и
        baseline; остальное (консоль/обновления) скрыто честно."""
        from ai.admin.guest_mode import SESSION_KEY
        group = self._view_as_group(
            "Эмуля-наблюдатель", ["page.admin", "page.model_status"],
        )
        user = self.user_model.objects.create_user(
            username="ctx-viewas", password="x", is_superuser=True, is_staff=True,
        )
        context = self._context(user, group=group)
        self.assertTrue(context["is_viewing_as_guest"])
        self.assertIn("Эмуля-наблюдатель", context["view_as_group_name"])
        self.assertIn("как группу", context["user_role_label"])
        self.assertTrue(context["show_model_status_link"],
                        "у группы ключ есть — пункт виден ЧЕСТНО")
        self.assertFalse(context["show_test_console_link"])
        self.assertFalse(context["show_updates_link"])
        # baseline НЕ отменяется у группы с page.admin: пакетное решение видно.
        self.assertTrue(context["show_arm_link"])
        wizard = context["ai_wizard"]
        self.assertFalse(wizard["show"],
                         "в эмуляции тур не показывается (не двигает состояние реального юзера)")

    def test_view_as_group_baseline_only(self):
        """Группа только с page.admin: видны baseline-страницы, но не ключи."""
        group = self._view_as_group("Эмуля-минимум", ["page.admin"])
        user = self.user_model.objects.create_user(
            username="ctx-viewas2", password="x", is_superuser=True, is_staff=True,
        )
        context = self._context(user, group=group)
        self.assertTrue(context["show_arm_link"])
        self.assertTrue(context["show_prompt_link"])
        self.assertTrue(context["show_logs_link"])
        self.assertFalse(context["show_model_status_link"])
        self.assertFalse(self._doc_tool_names(context))

    def test_prompt_developer_no_docs_without_key(self):
        # Решение владельца 2026-10-20: документация НЕ идёт с ролью —
        # выдаётся ключом docs.prompt_developer в «Группах доступа».
        user = self.user_model.objects.create_user(username="ctx-pd", password="x")
        make_prompt_developer(user)
        context = self._context(user)
        self.assertFalse(context["is_viewing_as_guest"])
        self.assertFalse(self._doc_tool_names(context))


class RestrictedUserEditorsInlineTests(TestCase):
    """Выдача прав на чужие промпты со страницы пользователя."""

    def setUp(self):
        self.factory = RequestFactory()
        self.user_model = get_user_model()
        self.admin = get_user_model().objects.create_user(
            username="inline-super", password="x", is_superuser=True, is_staff=True,
        )
        self.staff = get_user_model().objects.create_user(username="inline-staff", password="x", is_staff=True)

    def test_inline_only_for_superuser(self):
        from ai.admin.models import PromptEditorshipInline, RestrictedUserAdmin
        from django.contrib.admin.sites import AdminSite
        model_admin = RestrictedUserAdmin(self.user_model, AdminSite())
        request = self.factory.get("/ai/admin/auth/user/")
        request.user = self.admin
        inlines = model_admin.get_inlines(request, obj=self.staff)
        self.assertIn(PromptEditorshipInline, inlines)
        request.user = self.staff
        inlines = model_admin.get_inlines(request, obj=self.staff)
        self.assertNotIn(PromptEditorshipInline, inlines)

    def test_through_model_links_editor(self):
        prompt = Prompt.objects.create(prompt_name_ru="t", prompt_text_ru="текст")
        editor = self.user_model.objects.create_user(username="inline-editor", password="x")
        Prompt.editors.through.objects.create(prompt=prompt, user=editor)
        self.assertIn(editor, prompt.editors.all())
        self.assertIn(prompt, editor.editable_prompts.all())

    def test_through_model_not_registered_in_admin_site(self):
        from ai.admin.site import ai_admin_site
        from ai.models import Prompt
        self.assertNotIn(
            Prompt.editors.through,
            {model for model, _ in ai_admin_site._registry.items()},
        )


class DashboardGreetingTests(TestCase):
    """Приветствие «[id] ФИО» вместо Recent actions на дашборде."""

    def setUp(self):
        self.factory = RequestFactory()
        self.user_model = get_user_model()

    def test_user_dl_id(self):
        from ai.admin.site import AIAdminSite
        user = self.user_model.objects.create_user(username="user_123", password="x")
        self.assertEqual(AIAdminSite._user_dl_id(user), "123")
        local = self.user_model.objects.create_user(username="alice", password="x")
        self.assertEqual(AIAdminSite._user_dl_id(local), "alice")
        self.assertEqual(AIAdminSite._user_dl_id(AnonymousUser()), "")

    def test_dashboard_template_greeting(self):
        from django.template.loader import render_to_string
        user = self.user_model.objects.create_user(username="user_42", password="x")
        request = self.factory.get("/ai/admin/")
        request.user = user
        html = render_to_string(
            "admin/ai/index.html",
            {
                "user_dl_id": "42",
                "user_display_name": "Иван Иванов",
                "user_role_label": "Суперпользователь",
                "user": user,
                "available_apps": [],
            },
            request=request,
        )
        self.assertIn("ai-dashboard-greeting", html)
        self.assertIn("[42]", html)
        self.assertIn("Иван Иванов", html)
        self.assertNotIn("recent-actions-module", html)
        self.assertNotIn("get_admin_log", html)


class ChatUserDocsViewTests(TestCase):
    """Модалка «?» в чате: GET /ai/docs/ + /ai/docs/download/."""

    def setUp(self):
        self.factory = RequestFactory()
        self.user_model = get_user_model()

    def _request(self, path):
        request = self.factory.get(path)
        request.user = self.user_model.objects.create_user(username="chat-docs", password="x")
        return request

    def test_forbidden_without_external_id(self):
        from ai.views import chat_user_docs_view
        response = chat_user_docs_view(self._request("/ai/docs/"))
        self.assertEqual(response.status_code, 403)

    def test_docs_json_payload(self):
        from unittest.mock import patch
        from ai.views import chat_user_docs_view
        with patch("ai.views.get_external_user_id_from_request", return_value="12345"):
            response = chat_user_docs_view(self._request("/ai/docs/"))
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertTrue(data["success"])
        self.assertIn("<h", data["html"])
        self.assertEqual(data["lang"], "ru")
        self.assertEqual(len(data["fingerprint"]), 2)
        self.assertEqual(data["download_url"], "/ai/docs/download/?lang=ru")

    def test_download_headers(self):
        from unittest.mock import patch
        from ai.views import chat_user_docs_download_view
        with patch("ai.views.get_external_user_id_from_request", return_value="12345"):
            response = chat_user_docs_download_view(self._request("/ai/docs/download/"))
        self.assertEqual(response["Content-Type"], "text/markdown; charset=utf-8")
        self.assertIn("attachment", response["Content-Disposition"])

    def test_download_forbidden_without_external_id(self):
        from ai.views import chat_user_docs_download_view
        response = chat_user_docs_download_view(self._request("/ai/docs/download/"))
        self.assertEqual(response.status_code, 403)


class BatchRunStatusDisplayTests(SimpleTestCase):
    """Маппинг статуса batch-прогона в журналах: только «Выполнен»/«Прерван»
    (+ переходное «Выполняется…»); неизвестный статус — пусто (шаблоны
    падают на статус записи журнала). AIRequestLog.status в БД не меняется."""

    def test_status_mapping(self):
        from ai.admin.request_logs import batch_run_status_display
        self.assertEqual(batch_run_status_display("completed"), "Выполнен")
        self.assertEqual(batch_run_status_display("cancelled"), "Прерван")
        self.assertEqual(batch_run_status_display("failed"), "Прерван")
        self.assertEqual(batch_run_status_display("running"), "Выполняется…")
        self.assertEqual(batch_run_status_display(""), "")
        self.assertEqual(batch_run_status_display("weird"), "")


class TaskSolutionListTests(_AdminViewRequestMixin, TestCase):
    """Кастомный список «Решённые задачи»: название задачи (ссылка в DL по
    клику) с припиской — путь в дереве задач DL (tree_path), имя языка
    вместо сырого id, дата МСК, промпт юзера; без фильтров (в кэш
    попадают только passed) — работает поиск."""

    def setUp(self):
        self.factory = RequestFactory()

    def _admin(self):
        from ai.admin.models import TaskSolutionAdmin
        from ai.admin.site import ai_admin_site
        from ai.models import TaskSolution
        return TaskSolutionAdmin(TaskSolution, ai_admin_site)

    def _view(self, su, params=None):
        request = self._admin_request(su, path="/ai/admin/ai/tasksolution/", data=params or {})
        return self._admin().changelist_view(request)

    def test_changelist_rows(self):
        from ai.models import ProgrammingLanguage, Task

        su = get_user_model().objects.create_user(
            username="sol-su", password="x", is_superuser=True, is_staff=True,
        )
        lang = ProgrammingLanguage.objects.create(language_name="Pascal")
        Task.objects.create(node_id=9001, task_id=9101, name="Сумма чисел", statement="x")
        TaskSolution.objects.create(
            task_node_id=9001, programming_language_id=lang.pk, course_id=1450,
            file_extension=".pas", code="begin end.",
            verdict=TaskSolution.VERDICT_PASSED,
            topic_name="Линейные", prompt_name="Реши задачу",
            external_user_id="4242",
            tree_path="Программирование\\Линейные\\Сумма чисел",
        )
        response = self._view(su)
        self.assertEqual(response.status_code, 200)
        rows = response.context_data["sol_rows"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["task_name"], "Сумма чисел")
        self.assertEqual(row["task_url"], "https://dl.gsu.by/task.jsp?nid=9001&cid=1450")
        self.assertEqual(row["tree_path"], "Программирование\\Линейные\\Сумма чисел")
        self.assertEqual(row["lang_name"], "Pascal")
        self.assertEqual(row["prompt_name"], "Реши задачу")
        self.assertRegex(row["date"], r"\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}")

    def test_task_without_course_links_default_tree(self):
        """Без известного курса (старые записи) cid всё равно ставится:
        DL_DEFAULT_COURSE_ID — единственное дерево задач DL (приписка
        &cid=<дерево> в конце ссылки не должна пропадать)."""
        su = get_user_model().objects.create_user(
            username="sol-su2", password="x", is_superuser=True, is_staff=True,
        )
        TaskSolution.objects.create(
            task_node_id=9002, programming_language_id=None,
            code="x", verdict=TaskSolution.VERDICT_FAILED,
        )
        response = self._view(su)
        rows = response.context_data["sol_rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["task_url"], "https://dl.gsu.by/task.jsp?nid=9002&cid=1450")
        self.assertEqual(rows[0]["task_name"], "Задача #9002")

    def test_search_filters_rows(self):
        """Поиск (без list_filter — в кэш попадают только passed) работает."""
        su = get_user_model().objects.create_user(
            username="sol-su3", password="x", is_superuser=True, is_staff=True,
        )
        TaskSolution.objects.create(
            task_node_id=9003, verdict=TaskSolution.VERDICT_PASSED, code="ok",
            prompt_name="Реши задачу",
        )
        TaskSolution.objects.create(
            task_node_id=9004, verdict=TaskSolution.VERDICT_PASSED, code="no",
            prompt_name="Другой промпт",
        )
        response = self._view(su, params={"q": "Реши задачу"})
        rows = response.context_data["sol_rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["obj"].task_node_id, 9003)


class FilterSelectBarTests(_AdminViewRequestMixin, TestCase):
    """Фильтры changelist — селекторы НАД таблицей (не чипы и не боковая
    колонка): каждая группа рендерится как select[data-ai-filter], активная
    опция помечена selected, выбор уводит на choice.query_string."""

    def setUp(self):
        self.factory = RequestFactory()

    def _render(self, params=None):
        from ai.admin.models import PromptAdmin
        from ai.admin.site import ai_admin_site
        from ai.models import Prompt

        su = get_user_model().objects.create_user(
            username="filt-su", password="x", is_superuser=True, is_staff=True,
        )
        admin = PromptAdmin(Prompt, ai_admin_site)
        request = self._admin_request(
            su, path="/ai/admin/ai/prompt/", data=params or {},
        )
        response = admin.changelist_view(request)
        self.assertEqual(response.status_code, 200)
        return response.rendered_content

    def test_filters_render_as_selects(self):
        html = self._render()
        self.assertIn('<select data-ai-filter', html)
        # Каждая группа — подпись + селектор; «Все» — первая опция.
        groups = re.findall(r'<label class="ai-filter-select">(.*?)</label>', html, re.DOTALL)
        self.assertTrue(groups)
        for group in groups:
            self.assertIn("<select", group)
            self.assertIn("<option", group)

    def test_active_filter_preselected_and_preserved_in_target(self):
        """Активный фильтр ?mode__exact=solve: опция selected, опция «Все»
        сбрасывает свой параметр («?») — query_string приходит из стокового
        контракта choices (ChoicesFieldListFilter, параметр mode__exact)."""
        from ai.models import ProgrammingLanguage, Topic

        lang = ProgrammingLanguage.objects.create(language_name="Pascal")
        Topic.objects.create(topic_name_ru="Линейные", programming_language=lang)
        html = self._render(params={"mode__exact": "solve"})
        mode_group = re.search(
            r'<select data-ai-filter[^>]*>(.*?)</select>', html, re.DOTALL,
        )
        self.assertIn('value="?mode__exact=solve" selected', mode_group.group(1))
        self.assertIn('value="?"', mode_group.group(1))
        # Языковая группа предлагает созданный язык.
        self.assertIn("Pascal", html)

    def test_chip_markup_is_gone(self):
        html = self._render()
        self.assertNotIn("ai-filter-chip-group", html)


class ArmCodeExtractionTests(SimpleTestCase):
    """Извлечение чистого кода из ответа модели (_extract_code_from_response):
    longest-fence побеждает; текст без фенсов — рассуждения, а не код (пусто);
    think-блоки вырезаются целиком (в т.ч. незакрытые и код внутри них)."""

    def _extract(self, text):
        from ai.arm_runner import _extract_code_from_response
        return _extract_code_from_response(text)

    def test_longest_fence_wins(self):
        text = "```asm\nmov ax, 1\n```\nмусор\n```pascal\nprogram a; begin end.\n```"
        self.assertEqual(self._extract(text), "program a; begin end.")

    def test_no_fences_returns_empty(self):
        self.assertEqual(self._extract("We need answer code only, no explanations."), "")
        self.assertEqual(self._extract(""), "")

    def test_no_fences_code_like_text_is_accepted(self):
        """Код без фенсов (фейковые/короткие ответы моделей) не теряется."""
        self.assertEqual(
            self._extract("program a; begin writeln(1); end."),
            "program a; begin writeln(1); end.",
        )

    def test_prose_with_stray_punctuation_is_rejected(self):
        """Рассуждения со случайными ';'/скобками — не код (реальный кейс из чата)."""
        prose = (
            "We need answer code only, no explanations.\n"
            "Need solve assembler i86.\n"
            "Need infer syntax? Likely x86 16-bit? maybe MASM/TASM.\n"
            "Need determine variables sizes: a,b,RES word (2 bytes signed?); c,d byte (signed).\n"
            "Need compute based on condition b>0 or c>0 -> first; if b<0 and c<=0 -> second.\n"
            "Need produce assembly code.\n"
            "Need include data segment? Need compute expression.\n"
            "Let's parse expressions carefully.."
        )
        self.assertEqual(self._extract(prose), "")

    def test_strip_think_blocks_full_and_unclosed(self):
        from ai.arm_runner import _strip_think_blocks
        # Литеральные теги собираются, чтобы не мешать парсингу этого файла.
        open_tag = chr(60) + "think>"
        close_tag = chr(60) + "/think>"
        # Закрытый блок — удалён вместе с содержимым, текст после блока остаётся.
        self.assertEqual(
            _strip_think_blocks(f"{open_tag}рассуждения{close_tag}ответ"),
            "ответ",
        )
        # Незакрытый блок — до конца текста.
        self.assertEqual(_strip_think_blocks(f"{open_tag}only reasoning"), "")

    def test_code_inside_think_is_ignored(self):
        from ai.arm_runner import _strip_think_blocks
        open_tag = chr(60) + "think>"
        close_tag = chr(60) + "/think>"
        text = _strip_think_blocks(
            f"{open_tag}```asm\nmov ax, 1\n```{close_tag}Финальный ответ:\n```pascal\nbegin end.\n```"
        )
        self.assertEqual(self._extract(text), "begin end.")

    def test_think_stripped_before_extraction_pipeline(self):
        """Ответ, целиком состоящий из рассуждений → нет ни кода, ни текста."""
        from ai.arm_runner import _strip_think_blocks
        text = _strip_think_blocks(
            chr(60) + "think>Need solve assembler i86. Need infer syntax."
        )
        self.assertEqual(self._extract(text), "")

    def test_unclosed_fence_taken_to_end(self):
        """Незакрытая оградка (модель забывает ```) — код до конца текста."""
        text = "Вот решение:\n```asm\nmov ax, 1\nadd ax, 2"
        self.assertEqual(self._extract(text), "mov ax, 1\nadd ax, 2")

    def test_unclosed_fence_trims_trailing_prose(self):
        """Незакрытая оградка + прощание после кода — прощание срезано."""
        text = "```pascal\nprogram a;\nbegin\nwriteln(1);\nend.\nУдачи!\nВот и всё."
        self.assertEqual(self._extract(text), "program a;\nbegin\nwriteln(1);\nend.")

    def test_fence_language_with_symbols_not_leaked(self):
        """Язык-тег с символами («c-mpa», «c++») не попадает внутрь кода."""
        text = "```c-mpa\nint main(void) {\nreturn 0;\n}\n```"
        self.assertEqual(self._extract(text), "int main(void) {\nreturn 0;\n}")
        self.assertEqual(
            self._extract("```c++\nint main(void) {\n}\n```"),
            "int main(void) {\n}",
        )

    def test_unfenced_code_after_prose_intro_carved(self):
        """Код без оградок после прозаического вступления — карвинг кода."""
        text = (
            "Решение задачи: считаем сумму и выводим ответ.\n"
            "program a;\n"
            "var a, b: integer;\n"
            "begin\n"
            "writeln(a+b);\n"
            "end."
        )
        self.assertEqual(
            self._extract(text),
            "program a;\nvar a, b: integer;\nbegin\nwriteln(a+b);\nend.",
        )

    def test_cot_plus_unfenced_code_carved(self):
        """Смесь «рассуждения + код без оградок» — код выделен, проза срезана."""
        text = (
            "We need solve this task in assembler i86 here.\n"
            "Считаем по формуле: сначала считаем сумму элементов массива.\n"
            "mov ax, a\n"
            "cbw\n"
            "idiv b\n"
            "mov R, ax"
        )
        self.assertEqual(self._extract(text), "mov ax, a\ncbw\nidiv b\nmov R, ax")


class StructuralCarverTests(SimpleTestCase):
    """Структурные экстракторы С-МПА (services/code_carver.py):

    Регрессии реальных прогонов: res#3987 (цитата-болванка каркаса в CoT
    притягивала экстрактор — прозя и болванка склеивались с программой),
    res#4160 (программа дана дважды — грязный первый заход с хвост-прозой
    затирал чистый финал), res#3980 (рассуждение-вставка посреди кода).
    """

    def _c(self, text, ext=""):
        from ai.arm_runner import _extract_code_from_response
        return _extract_code_from_response(text, file_extension=ext).strip()

    # --- asm .i86 ---

    def test_asm_outline_in_cot_rejected_real_wins(self):
        """res#3987: болванка в мини-фенсе внутри CoT + настоящая программа.

        Болванке не хватает концевика → её кандидат тянется до концевика
        настоящей программы и бракуется по повторному «jmp begin»;
        анкер настоящей программы даёт чистый кандидат.
        """
        outline = "```asm\njmp begin\nn dw 5\nb dw 3,5,4-1,2\na dw 0,0\ntemp1 dw 0\n```"
        cot = ("We need solve the task in assembler i86 now here.\n"
               "Need sort array b into array a. Wait, allowed commands? jge fine.\n")
        real = ("jmp begin\nb dw 3,5,4\na dw 0,0,0\nn dw 5\n"
                "begin:\nmov si, 0\nouter_loop:\nmov ax, si\n"
                "cmp ax, bx\njge sort_done\nmov min_off, si\n"
                "Ends:\njmp Ends ;$E")
        text = cot + "\n" + outline + "\n" + cot + "\n" + real
        got = self._c(text, ext=".i86")
        self.assertIn("jmp begin", got)
        self.assertIn(";$E", got)
        self.assertNotIn("We need solve", got)

    def test_asm_program_repeated_clean_last_wins(self):
        """res#4160: модель дала программу дважды; первый заход с CoT-хвостом,
        второй чистый с «jmp Ends ;$E» — берётся ПОСЛЕДНИЙ валидный."""
        tail_quote = ("```\nEnds:\njmp Ends ;$E\n```\n"
                      "So okay.\n\nNeed structure: start jmp begin, then variables, then"
                      " begin label. End with Ends: jmp Ends ;$E. They want `Ends:` and"
                      " `jmp Ends ;$E`. In example they have `Ends:` then `jmp Ends ; done`.")
        attempt1 = ("jmp begin\n\nn dw 12345\nz dw 0\nten dw 10\n\nbegin:\n"
                    "mov z, 0\nloop_start:\nmov ax, n\n")
        attempt2 = ("jmp begin\n\nn dw 12345\nz dw 0\nten dw 10\nplace dw 1\n\n"
                    "begin:\nmov z, 0\nmov place, 1\nloop_start:\nmov ax, n\n"
                    "cmp ax, 0\nje done\n\ndone:\njmp Ends ;$E")
        text = attempt1 + "\n" + tail_quote + "\n" + attempt2
        got = self._c(text, ext=".i86")
        self.assertIn("place dw 1", got)
        self.assertIn("je done", got)
        self.assertTrue(got.rstrip().endswith("jmp Ends ;$E"))
        self.assertNotIn("So okay", got)
        self.assertNotIn("Need structure", got)

    def test_asm_mid_prose_cut_keeps_program(self):
        """res#3980: рассуждение-вставка посреди кода срезается, код жив."""
        code = ("jmp begin\n\nn dw 5\nb dw 3,5,4\na dw 0,0,0\n\n"
                "begin:\nmov cx, n\nmov si, 0\ncopy_loop:\n"
                "Need implement with byte offsets carefully. Let n be small.\n"
                "copy_done:\nmov di, a\n"
                "Ends:\njmp Ends ;$E")
        got = self._c(code, ext=".i86")
        self.assertIn("copy_done:", got)
        self.assertNotIn("Need implement", got)

    def test_asm_short_cot_noise_line_cut(self):
        """CoT-фразы без точки («Need maybe `step`? Not») — тоже мусор."""
        code = ("jmp begin\nn dw 5\nb dw 3\n\nbegin:\nmov ax, n\n"
                "Need maybe `step`? Not\nmov R, ax\nEnds:\njmp Ends ;$E")
        got = self._c(code, ext=".i86")
        self.assertNotIn("Need maybe", got)
        self.assertIn("mov R, ax", got)

    def test_asm_marker_in_cot_quote_not_end(self):
        """;$E внутри CoT-цитаты (простыня со словом jmp) — не конец программы;
        настоящий концевик ниже перехватывает span."""
        long_cot = ("Need structure: start jmp begin, then variables, then begin label."
                    " End with Ends: jmp Ends ;$E The user theory says ok then done")
        real2 = "jmp begin\nb dw 2\nbegin:\nmov ax, b\nmov R, ax\nEnds:\njmp Ends ;$E"
        text = "jmp begin\nb dw 1\nbegin:\nmov ax, b\n" + long_cot + "\n" + real2
        got = self._c(text, ext=".i86")
        self.assertIn("mov R, ax", got)
        self.assertTrue(got.endswith("jmp Ends ;$E"))

    def test_asm_data_section_not_cut(self):
        """Дата-секция (dw/db/dup) и русский коммент не считаются прозя."""
        code = ("jmp begin\n\n; Входные данные\nx dw 2\ny dw -1\na db 1\nc db 3\n"
                "RES dw 0\n\nbegin:\nmov al, a\ncbw\nmov a_16, ax\n"
                "Ends:\njmp Ends ;$E")
        got = self._c(code, ext=".i86")
        self.assertIn("x dw 2", got)
        self.assertIn("Входные данные", got)

    def test_asm_data_section_inline_comment_with_period_not_cut(self):
        """Выкусывание: реальная строка кода с хвостовым комментарием,
        кончающимся точкой («x dw 2 ; число икс.»), не срезается как
        прозя-предложение (судим по кодовой части строки)."""
        code = ("jmp begin\n\nx dw 2 ; число икс.\ny dw -1\n"
                "RES dw 0\n\nbegin:\nmov al, x\ncbw\nmov a_16, ax\n"
                "Ends:\njmp Ends ;$E")
        got = self._c(code, ext=".i86")
        self.assertIn("x dw 2 ; число икс.", got)
        self.assertIn("mov a_16, ax", got)

    def test_mpc_inline_comment_with_period_not_cut(self):
        """То же для С-МПА: «y = a + b; // сумма двух операндов.» — код."""
        code = ("int __in __bits(8) a;\nint __out __bits(8) y;\nvoid main()\n{\n"
                "y = a + b; // сумма двух операндов тут.\n}")
        got = self._c(code, ext=".mpc")
        self.assertIn("y = a + b; // сумма двух операндов тут.", got)
        self.assertIn("int __in __bits(8) a;", got)

    def test_asm_masm_skeleton_extracted(self):
        """MASM-скелет без «jmp begin» (.model/.stack/.data/.code/…/end start)
        тоже извлекается целиком — включая дату-секцию (прежде: анкер один —
        «jmp begin», программа проваливалась в карвинг и без оградки теряла
        данные; директивный анкер теперь даёт кандидата от первой директивы)."""
        masm = (".model small\n.stack 100h\n.data\nx dw 2\ny dw -1\n"
                ".code\nstart:\nmov ax, x\nadd ax, y\nend start")
        got = self._c("Решение на MASM:\n" + masm + "\nУдачи!", ext=".i86")
        self.assertIn(".model small", got)
        self.assertIn("x dw 2", got)
        self.assertIn("mov ax, x", got)
        self.assertIn("end start", got)
        self.assertNotIn("Удачи", got)

    def test_asm_skewed_skeleton_label_extracted(self):
        """Скошенный каркас «jmp start / start: …» (модели иного диалекта):
        генерализованные якоря не теряют дату-секцию, лейблы циклов
        (jmp loop_start) анкерами НЕ становятся."""
        code = ("jmp start\nx dw 2\ny dw -1\nRES dw 0\n\n"
                "start:\nmov ax, x\ncbw\nadd ax, y\nmov RES, ax\nend start")
        got = self._c("Сначала объясню: решение простое.\n" + code, ext=".i86")
        self.assertIn("jmp start", got)
        self.assertIn("x dw 2", got)
        self.assertIn("end start", got)
        self.assertNotIn("решение простое", got)

    def test_alt_cut_requires_code_right_after_heading(self):
        """Ранний заголовок «Альтернатива:» в CoT НЕ должен срезать
        ФИНАЛЬНУЮ программу, записанную ниже: между заголовком и кодом
        прозя-строка — среза нет (res: «Финальная программа:» терялась)."""
        early = ("Сначала покажу как не надо:\n"
                 "jmp begin\nn dw 5\n\nbegin:\nmov ax, n\n"
                 "Альтернатива: нужно иначе.\nФинальная программа:\n"
                 "jmp begin\nn dw 4\nplace dw 1\n\nbegin:\nmov z, 0\n"
                 "loop_start:\nmov ax, n\ncmp ax, 0\n\nje done\n\ndone:\njmp Ends ;$E")
        got = self._c(early, ext=".i86")
        self.assertIn("place dw 1", got)
        self.assertIn("je done", got)

    def test_mpc_glued_decl_window_salvaged(self):
        """Склейка «декларации + прозя + повтор main» в окне от
        якоря-декларации больше не бракуется целиком: декларации спасаются
        (иначе DL падал «Undefined variable»), кодовая вставка между
        main-ами выкусывается."""
        text = (
            "int __in __bits(8) a;\nint __out __bits(8) y;\n"
            "void main() {\ny = a +\n"
            "Now I reconsider. The correct one:\n"
            "void main() {\ny = a + 1;\ny = a + 2;\n}\n"
        )
        got = self._c(text, ext=".mpc")
        self.assertIn("int __in __bits(8) a;", got)
        self.assertIn("y = a + 2;", got)
        self.assertNotIn("Now I reconsider", got)

    def test_language_code_incomplete(self):
        """Гейт обрыва: код без концевика каркаса (.i86) / без main или
        несбалансированные скобки (.mpc) — неполный."""
        from ai.services.code_carver import language_code_incomplete
        self.assertTrue(language_code_incomplete(
            "jmp begin\nbegin:\nmov ax, 1", ".i86"))
        self.assertFalse(language_code_incomplete(
            "jmp begin\nbegin:\nmov ax, 1\nEnds:\njmp Ends ;$E", ".i86"))
        self.assertFalse(language_code_incomplete(
            ".model small\n.code\nmov ax, 1\nend start", ".i86"))
        self.assertTrue(language_code_incomplete(
            "int __in __bits(8) a;\nvoid main() {\ny = a;", ".mpc"))
        self.assertTrue(language_code_incomplete(
            "int __in __bits(8) a;", ".mpc"))
        self.assertFalse(language_code_incomplete(
            "int __in __bits(8) a;\nvoid main() {\ny = a; }", ".mpc"))
        self.assertFalse(language_code_incomplete("whatever", ""))

    # --- С-МПА .mpc ---

    def test_mpc_decls_plus_prose_plus_main(self):
        """Прозя между декларациями и main срезается; полный кандидат жив."""
        text = ("Разбор задачи: считаем сумму двух байтов.\n"
                "int __in __bits(8) a;\nint __in __bits(8) b;\nint __out __bits(8) y;\n\n"
                "Также нужно помнить правило про промежуточные переменные.\n"
                "void main()\n{\ny = a + b;\n}\n\nУдачи!")
        got = self._c(text, ext=".mpc")
        self.assertIn("int __in __bits(8) a;", got)
        self.assertIn("void main()", got)
        self.assertIn("y = a + b;", got)
        self.assertNotIn("Также нужно помнить", got)
        self.assertNotIn("Удачи!", got)

    def test_mpc_outline_glue_rejected_final_main_wins(self):
        """res#1225-класс: болванки-фенсы в CoT; склейка «болванка + финал»
        бракуется по двум main; финальный main-блок — единственный валидный."""
        globals_quote = "```c\nint __in __bits(16) X;\nint __in __bits(16) Y;\n```"
        main_quote = "```c\nvoid main() {\nRES = X + Y;\n}\n```"
        final = "void main() {\nif (X * Y >= 0) {\nRES = ((X - Y) * 2 + 1) / Y;\n} else {\nRES = X;\n}\n}"
        cot = ("*   Wait, check Rule 5 (Conditional expressions): Составные условия"
               " раскрывай явно. Need think about this problem now.")
        text = "Thinking Process:\n" + globals_quote + "\n" + main_quote + "\n" + cot + "\n" + final
        got = self._c(text, ext=".mpc")
        self.assertIn("void main()", got)
        self.assertIn("RES = X", got)
        self.assertNotIn("__bits(16) X", got)  # болванка-декларации НЕ склеены

    def test_mpc_hydration_junk_empty(self):
        """HYDRATION-мусор веб-пула — не код (гейт до DL)."""
        from ai.services.code_carver import has_language_marker as _hlm
        junk = 'window.HYDRATION_INIT_STATE={"mutations":[],"queries":[{"dehydratedAt":1788301366911}]}'
        self.assertEqual(self._c(junk, ext=".mpc"), "")
        self.assertEqual(self._c("[ВСТАВЬТЕ ВАШ КОД]", ext=".mpc"), "")
        self.assertFalse(_hlm(junk, ".mpc"))

    def test_mpc_comments_and_braces_legit(self):
        """//-комменты и фигурные скобки — часть кода, не мусор."""
        code = ("// Объявление глобальных переменных\n"
                "int __in __bits(8) a;\nint __out __bits(8) y;\n\n"
                "// Главная функция программы\nvoid main()\n{\n\n// Вычисление суммы\ny = a + b;\n\nreturn 0;\n}")
        got = self._c(code, ext=".mpc")
        self.assertIn(int_text := "int __in __bits(8) a;", got)
        self.assertIn("// Главная функция", got)
        self.assertTrue(got.rstrip().endswith("}"))

    # --- Гейт языковых маркеров ---

    def test_gate_markers(self):
        from ai.services.code_carver import has_language_marker
        self.assertTrue(has_language_marker("jmp begin\na dw 2\n", ".i86"))
        self.assertTrue(has_language_marker(".model tiny\n.code\norg 100h\nmov ax, 1\n", ".i86"))
        self.assertTrue(has_language_marker("int __in __bits(8) a;\n", ".mpc"))
        self.assertTrue(has_language_marker("void main(){}\n", ".mpc"))
        self.assertFalse(has_language_marker("window.HYDRATION...{},", ".i86"))
        self.assertFalse(has_language_marker("Need answer only in Russian.", ".mpc"))
        # Не-курсовые расширения гейт не отвергает
        self.assertTrue(has_language_marker("anything at all", ""))
        self.assertTrue(has_language_marker("anything at all", ".pas"))

    def test_worker_gate_rejects_junk_before_dl(self):
        """Батч-воркер: мусору без маркеров не ходит в DL (проверка константы)."""
        from ai.arm_runner import has_language_marker
        self.assertFalse(has_language_marker('[ВСТАВЬТЕ ВАШ КОД]', '.mpc'))


class ArmVerdictFixCommandTests(TestCase):
    """fix_arm_verdicts: пересчёт вердиктов по dl_comment (dry-run/commit)."""

    @staticmethod
    def _make_row(run, **kw):
        from ai.models import AIModelTestResult
        return AIModelTestResult.objects.create(
            run=run, model_key=kw.get("model_key", "M"), model_title=kw.get("model_key", "M"),
            verdict=kw.get("verdict", "solved"),
            dl_comment=kw.get("dl_comment", ""),
            code=kw.get("code", "jmp begin\nEnds:\njmp Ends ;$E"),
            status=kw.get("status", "ok"),
        )

    def setUp(self):
        from django.contrib.auth import get_user_model
        from ai.models import AIModelTestRun
        self.run = AIModelTestRun.objects.create(
            run_id="verdict-fix-1", run_type=AIModelTestRun.RUN_TYPE_BATCH)

    def test_dry_run_touches_nothing(self):
        from django.core.management import call_command
        self._make_row(self.run, verdict="solved", dl_comment="[test1]: Ошибка компиляции: Ошибка синтаксиса (строка 1): junk.", model_key="M1")
        call_command("fix_arm_verdicts")
        row = self.run.results.first()
        self.assertEqual(row.verdict, "solved")
        self.assertEqual(row.status, "ok")

    def test_commit_fixes_compile_error_solved(self):
        from django.core.management import call_command
        self._make_row(self.run, verdict="solved", dl_comment="[test1]: Ошибка компиляции: junk.", model_key="M2")
        good = self._make_row(self.run, verdict="solved", dl_comment="[test1]: Все тесты пройдены.", model_key="M3")
        call_command("fix_arm_verdicts", commit=True)
        bad = self.run.results.get(model_key="M2")
        self.assertEqual(bad.verdict, "failed")
        self.assertEqual(bad.status, "error")
        good.refresh_from_db()
        self.assertEqual(good.verdict, "solved")

    def test_empty_comment_untouched(self):
        from django.core.management import call_command
        self._make_row(self.run, verdict="solved", dl_comment="")
        call_command("fix_arm_verdicts", commit=True)
        row = self.run.results.first()
        self.assertEqual(row.verdict, "solved")  # пустую не трогаем


class TemplateInlineCommentTests(SimpleTestCase):
    """Регрессия: однострочные комментарии {# … #} в шаблонах НЕ могут
    переносить строки — Django рендерит их содержимое как текст прямо
    на странице (реальный кейс с чипами фильтров). Многострочный текст
    оформляем тегом {% comment %}…{% endcomment %}."""

    def test_no_multiline_inline_comments(self):
        import glob
        import os
        # Шаблоны лежат в ai/templates (раньше путь считался как <repo>/templates
        # — его не существует, glob был пуст и тест проходил ничего не проверяя).
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
        offenders = []
        for path in glob.glob(os.path.join(base, "**", "*.html"), recursive=True):
            with open(path, encoding="utf-8") as fh:
                for lineno, line in enumerate(fh, 1):
                    if "{#" in line and "#}" not in line:
                        offenders.append(f"{os.path.relpath(path, base)}:{lineno}")
        self.assertEqual(offenders, [])


class ReextractArmCodesCommandTests(TestCase):
    """reextract_arm_codes: восстановление кода по сырому ответу;
    solved не трогает; мусор без маркеров языка в code не пишет."""

    def setUp(self):
        from ai.models import AIModelTestRun
        self.run = AIModelTestRun.objects.create(
            run_id="reextract-1", run_type=AIModelTestRun.RUN_TYPE_BATCH)

    @staticmethod
    def _raw_real():
        return ("Вот решение:\n```asm\njmp begin\nn dw 5\nb dw 3\na dw 0\n\n"
                "begin:\nmov ax, b\nadd ax, n\nmov a, ax\nEnds:\njmp Ends ;$E\n```\nУдачи!")

    def _make(self, model_key, verdict, raw, code="", ext=".i86"):
        from ai.models import AIModelTestResult
        return AIModelTestResult.objects.create(
            run=self.run, model_key=model_key, model_title=model_key,
            verdict=verdict, raw_response=raw, code=code,
            dl_comment="Не удалось извлечь код из ответа модели",
            file_extension_snapshot=ext,
        )

    def test_commit_fills_missing_code_skips_solved_and_junk(self):
        from django.core.management import call_command
        from ai.models import AIModelTestResult
        missing = self._make("A1", "failed", self._raw_real())
        solved = self._make("A2", "solved", self._raw_real(), code="")
        junk = self._make("A3", "failed", "window.HYDRATION_INIT_STATE={}\n", code="", ext=".mpc")
        call_command("reextract_arm_codes", run_ids=[self.run.pk], commit=True)
        missing.refresh_from_db()
        solved.refresh_from_db()
        junk.refresh_from_db()
        self.assertIn("jmp Ends ;$E", missing.code)
        self.assertEqual(solved.code, "")   # solved не трогаем
        self.assertEqual(junk.code, "")     # мусор в code не пишется

    def test_dry_run_writes_nothing(self):
        from django.core.management import call_command
        missing = self._make("B1", "failed", self._raw_real())
        call_command("reextract_arm_codes", run_ids=[self.run.pk])
        missing.refresh_from_db()
        self.assertEqual(missing.code, "")




class BatchRunNameTests(_AdminViewRequestMixin, TestCase):
    """Названия прогонов: воркер пишет run_name прямо в AIModelTestRun,
    журналы и таблица «Последние пакетные решения» читают его с прогона
    (run_name_for); для старых прогонов — фолбэк на run_params."""

    def setUp(self):
        self.factory = RequestFactory()
        self.user = get_user_model().objects.create_user(username="runname", password="x")

    def _run_batch_with_name(self, run_id, run_name):
        """Прогон batch-воркера синхронно с run_name (как в IntegrationTests)."""
        import time as _t
        from ai import arm_runner
        from ai.models import Task

        lang = ProgrammingLanguage.objects.create(language_name="Pascal")
        topic = Topic.objects.create(topic_name_ru="Линейные", programming_language=lang)
        t1 = Task.objects.create(
            node_id=7001, task_id=7101, name="A", statement="x",
            topic=topic, programming_language=lang, file_extension=".pas",
        )

        async def fake_handler(messages, conv_id):
            return ("program a; begin writeln(1); end.", 12)

        ordered_models = [{"key": "FakeModel", "title": "FakeModel", "handler": fake_handler}]
        dl_ok = lambda sid, node_id, code, ext, **kw: {
            "verdict": "solved", "comment": "ok", "submit_error": "",
            "queue_id": 1, "code_sent": code,
        }
        now_ts = _t.time()
        arm_runner._jobs[run_id] = {
            "run_id": run_id, "run_type": "batch", "status": "running",
            "error_message": "", "total_models": 1,
            "total_pairs": 1, "completed_pairs": 0, "completed_models": 0,
            "current_model_key": "FakeModel", "current_model_title": "FakeModel",
            "current_task_node_id": "", "current_task_name": "",
            "results": [], "report": None,
            "created_at_ts": now_ts, "updated_at_ts": now_ts,
        }
        try:
            with patch("ai.arm_runner._test_solution_on_dl", dl_ok):
                arm_runner._run_batch_job_worker(
                    run_id, [t1.node_id], ordered_models, self.user.id, "DLSID-1",
                    ui_language="Русский", dl_test=True, run_name=run_name,
                )
        finally:
            arm_runner._jobs.pop(run_id, None)
        return AIRequestLog.objects.get(message=f"Batch solve run {run_id}")

    def test_worker_writes_run_name_and_journals_read_it(self):
        """Воркер пишет run_name в AIModelTestRun; журналы и таблица
        «Последние пакетные решения» читают название с прогона."""
        from ai.admin.request_logs import (
            _build_batch_log_snapshot,
            build_recent_batch_rows,
            build_recent_log_rows,
            run_name_for,
        )
        from ai.models import AIModelTestRun

        run_id = "b" * 32
        log = self._run_batch_with_name(run_id, "Неделя 3")

        run = AIModelTestRun.objects.get(run_id=run_id)
        self.assertEqual(run.run_name, "Неделя 3")
        self.assertEqual(run_name_for(run), "Неделя 3")

        request = RequestFactory().get("/ai/admin/")
        request.user = get_user_model().objects.create_user(
            username="runname-su", password="x", is_superuser=True, is_staff=True,
        )
        batch_rows = build_recent_batch_rows(request)["recent_batch_runs"]
        self.assertEqual(batch_rows[0]["run_name"], "Неделя 3")
        self.assertEqual(batch_rows[0]["run_status_display"], "Выполнен")
        # Пакетная строка журнала — в ОБЩЕМ списке (одна таблица журнала).
        log_data = build_recent_log_rows(request)
        log_rows = log_data["recent_logs"]
        self.assertEqual(len(log_rows), 1)
        self.assertTrue(log_rows[0]["is_batch"])
        self.assertEqual(log_rows[0]["run_name"], "Неделя 3")
        self.assertEqual(log_rows[0]["run_status_display"], "Выполнен")

        snapshot = _build_batch_log_snapshot(log)
        self.assertEqual(snapshot["run_name"], "Неделя 3")
        self.assertEqual(snapshot["run_status"], "completed")

    def test_worker_without_run_name_writes_nothing(self):
        from ai.admin.request_logs import run_name_for
        from ai.models import AIModelTestRun

        run_id = "c" * 32
        self._run_batch_with_name(run_id, "")
        run = AIModelTestRun.objects.get(run_id=run_id)
        self.assertEqual(run.run_name, "")
        self.assertEqual(run_name_for(run), "—")
        self.assertEqual(run_name_for(None), "—")

    def test_batch_row_shows_full_name(self):
        """«Кто отправлял» в таблице «Последние пакетные решения» — ФИО
        (user_full_name), а не технический логин; при пустом ФИО — логин."""
        from ai.admin.request_logs import build_recent_batch_rows

        run_id = "d" * 32
        log = self._run_batch_with_name(run_id, "Неделя 4")
        AIRequestLog.objects.filter(pk=log.pk).update(
            username="ivanov_ii", user_full_name="Иванов Иван Иванович",
        )
        request = RequestFactory().get("/ai/admin/")
        request.user = get_user_model().objects.create_user(
            username="fname-su", password="x", is_superuser=True, is_staff=True,
        )
        rows = build_recent_batch_rows(request)["recent_batch_runs"]
        row = next(r for r in rows if r["id"] == log.id)
        self.assertEqual(row["sender_display"], "Иванов Иван Иванович")

        AIRequestLog.objects.filter(pk=log.pk).update(user_full_name="")
        rows = build_recent_batch_rows(request)["recent_batch_runs"]
        row = next(r for r in rows if r["id"] == log.id)
        self.assertEqual(row["sender_display"], "ivanov_ii")

    def test_recent_batch_rows_are_lazy(self):
        """Строки «Последних пакетных решений» лёгкие: полные снапшоты
        (тексты решений) в HTML страницы больше не встраиваются — только
        шапка + count-агрегат + snapshot_url для ленивой загрузки AJAX-ом.
        Иначе большой прогон раздувал страницу настроек до ~6 МБ."""
        from ai.admin.request_logs import build_recent_batch_rows

        run_id = "e" * 32
        log = self._run_batch_with_name(run_id, "Неделя 5")
        request = RequestFactory().get("/ai/admin/")
        request.user = get_user_model().objects.create_user(
            username="lazy-su", password="x", is_superuser=True, is_staff=True,
        )
        rows = build_recent_batch_rows(request)["recent_batch_runs"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertNotIn("snapshot", row)
        self.assertNotIn("json_id", row)
        self.assertEqual(row["snapshot_url"], f"/ai/admin/ai/request_logs/{log.id}/batch-snapshot/")
        self.assertEqual(row["run_name"], "Неделя 5")
        # Count-агрегат по результатам прогона (1 задача × 1 модель, solved).
        self.assertEqual(row["total_pairs"], 1)
        self.assertEqual(row["solved"], 1)
        self.assertEqual(row["failed"], 0)
        self.assertEqual(row["file_extension"], ".pas")

    def test_batch_snapshot_endpoint(self):
        """AJAX-снапшот прогона: суперюзеру — 200 с JSON снапшота;
        не-батч запись журнала — 404; без доступа к журналу — 403.
        Вьюха зовётся напрямую (site-level has_permission в ai_admin_site
        требует внешний DLSID — это проверяется уровнем выше, см.
        _AdminViewRequestMixin)."""
        from ai.admin.request_logs import admin_batch_snapshot_view
        from ai.models import AIRequestLog

        run_id = "f" * 32
        log = self._run_batch_with_name(run_id, "Неделя 6")
        su = get_user_model().objects.create_user(
            username="snap-su", password="x", is_superuser=True, is_staff=True,
        )
        url = f"/ai/admin/ai/request_logs/{log.id}/batch-snapshot/"

        request = self._admin_request(su, path=url)
        response = admin_batch_snapshot_view(request, log_id=log.id)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["run_name"], "Неделя 6")
        self.assertEqual(data["run_status"], "completed")
        self.assertEqual(data["run_id"], run_id)
        self.assertEqual(len(data["results"]), 1)
        self.assertEqual(data["report"]["solved"], 1)

        # Не-батч запись журнала (обычный чат) — снапшота нет → 404.
        plain = AIRequestLog.objects.create(
            source=AIRequestLog.SOURCE_HTTP, mode=AIRequestLog.MODE_CHAT,
            sent_at=timezone.now(), message="привет",
        )
        request = self._admin_request(su, path=f"/ai/admin/ai/request_logs/{plain.id}/batch-snapshot/")
        response = admin_batch_snapshot_view(request, log_id=plain.id)
        self.assertEqual(response.status_code, 404)

        # Без доступа к журналу (аноним) — 403 от самой вьюхи.
        from django.contrib.auth.models import AnonymousUser
        request = self._admin_request(AnonymousUser(), path=url)
        response = admin_batch_snapshot_view(request, log_id=log.id)
        self.assertEqual(response.status_code, 403)

    def test_snapshot_from_test_run_carries_run_name(self):
        """Снапшот страницы прогона берёт run_name с прогона (фолбэк —
        run_params для прогонов, созданных до колонки)."""
        from ai import arm_runner
        from ai.models import AIModelTestRun
        run = AIModelTestRun.objects.create(
            run_id="a" * 32,
            run_type=AIModelTestRun.RUN_TYPE_BATCH,
            status=AIModelTestRun.STATUS_RUNNING,
            run_name="Неделя 3",
        )
        snapshot = arm_runner._snapshot_from_test_run(run)
        self.assertEqual(snapshot["run_name"], "Неделя 3")

        legacy = AIModelTestRun.objects.create(
            run_id="d" * 32,
            run_type=AIModelTestRun.RUN_TYPE_BATCH,
            status=AIModelTestRun.STATUS_RUNNING,
            run_params={"run_name": "Старый прогон"},
        )
        self.assertEqual(arm_runner._snapshot_from_test_run(legacy)["run_name"], "Старый прогон")


class DailyReportTests(_AdminViewRequestMixin, TestCase):
    """Дневной отчёт журнала: день по МСК (00:00–23:59), строки — студенты
    (фамилия / время последней отправки без даты / последняя тема и
    промпт / последний режим / всего запросов за день), развёртка —
    все записи дня; ARM (source=arm) в отчёт не попадает;
    prompt_developer видит только свои записи."""

    def setUp(self):
        self.factory = RequestFactory()
        self.user_model = get_user_model()
        self.su = self.user_model.objects.create_user(
            username="daily-su", password="***", is_superuser=True, is_staff=True,
        )

    @staticmethod
    def _msk_at(hour, minute=0, second=0, *, day_offset=0):
        """Сегодня (или день_offset назад) в hour:minute по МСК → aware UTC."""
        from datetime import timedelta
        from zoneinfo import ZoneInfo
        now_msk = timezone.localtime(timezone.now(), ZoneInfo("Europe/Moscow"))
        naive = (now_msk + timedelta(days=day_offset)).replace(
            hour=hour, minute=minute, second=second, microsecond=0,
        )
        return naive.astimezone(ZoneInfo("Europe/Moscow"))

    def _log(self, *, user, external_user_id, full_name, sent_at, topic="",
             prompt="", mode=None, status=None, source=None):
        from ai.models import AIRequestLog
        return AIRequestLog.objects.create(
            user=user,
            source=source or AIRequestLog.SOURCE_WEBSOCKET,
            mode=mode or AIRequestLog.MODE_CHAT,
            status=status or AIRequestLog.STATUS_SUCCESS,
            sent_at=sent_at,
            external_user_id=external_user_id,
            username=f"user_{external_user_id}" if external_user_id else user.username,
            user_full_name=full_name,
            topic_name=topic,
            prompt_name=prompt,
            model_names=["FakeModel"],
        )

    def _report(self, user, data=None):
        from ai.admin.request_logs import admin_daily_report_view
        request = self._admin_request(user, path="/ai/admin/ai/request_logs/daily-report/", data=data)
        return admin_daily_report_view(request)

    def test_daily_report_aggregates_students(self):
        """Строки — по студенту: количество, последнее время/тема/промпт/
        режим; записи дня в развёртке — новые сверху; сортировка по фамилии;
        ARM-запись в счёт не идёт."""
        from ai.models import AIRequestLog

        ivanov = "Иванов Иван"
        petrov = "Петров Пётр"
        self._log(user=self.su, external_user_id="101", full_name=ivanov,
                  sent_at=self._msk_at(9, 15), topic="Циклы", prompt="Реши по шагам")
        log_1230 = self._log(user=self.su, external_user_id="101", full_name=ivanov,
                             sent_at=self._msk_at(12, 30), topic="Массивы", prompt="Реши задачу",
                             mode=AIRequestLog.MODE_SOLVE)
        self._log(user=self.su, external_user_id="200", full_name=petrov,
                  sent_at=self._msk_at(10, 0), topic="Строки", prompt="Найди ошибку",
                  mode=AIRequestLog.MODE_FIND_ERROR)
        # ARM-прогон — операторская запись, в студенческом отчёте не считается.
        self._log(user=self.su, external_user_id="101", full_name=ivanov,
                  sent_at=self._msk_at(11, 0), source=AIRequestLog.SOURCE_ARM,
                  mode=AIRequestLog.MODE_BATCH_SOLVE)
        # Вчерашняя запись — не в сегодняшнем дне.
        self._log(user=self.su, external_user_id="101", full_name=ivanov,
                  sent_at=self._msk_at(23, 59, 59, day_offset=-1), topic="Вчера")

        response = self._report(self.su)
        self.assertEqual(response.status_code, 200)
        rows = response.context_data["rows"]
        self.assertEqual([r["display_name"] for r in rows], [ivanov, petrov])

        row_ivanov = rows[0]
        self.assertEqual(row_ivanov["count"], 2)  # без ARM и без вчерашней
        self.assertEqual(row_ivanov["last_sent"], "12:30")
        self.assertEqual(row_ivanov["last_topic"], "Массивы")
        self.assertEqual(row_ivanov["last_prompt"], "Реши задачу")
        self.assertEqual(row_ivanov["last_mode"], "Решить задачу")
        # Развёртка: все записи дня, новые сверху, время без даты.
        self.assertEqual(len(row_ivanov["requests"]), 2)
        self.assertEqual(row_ivanov["requests"][0]["time"], "12:30:00")
        self.assertEqual(row_ivanov["requests"][0]["topic"], "Массивы")
        self.assertEqual(rows[1]["count"], 1)
        self.assertEqual(rows[1]["last_mode"], "Найти ошибку")
        self.assertEqual(response.context_data["total_count"], 3)
        # Клик по запросу разворачивает информацию на месте (lazy-загрузка):
        # id записи — в data-атрибуте, рендер — через detail-json эндпоинт.
        self.assertEqual(row_ivanov["requests"][0]["log_id"], log_1230.id)
        self.assertContains(response, f'data-log-id="{log_1230.id}"')

    def test_daily_report_msk_day_bounds(self):
        """День — по МСК: запись в 00:00:01 МСК включается, в 23:59 вчера —
        нет (границы дня локальные, не UTC)."""
        first_today = self._log(user=self.su, external_user_id="300", full_name="Сидоров",
                                sent_at=self._msk_at(0, 0, 1))
        before = self._log(user=self.su, external_user_id="300", full_name="Сидоров",
                           sent_at=self._msk_at(23, 59, 59, day_offset=-1), topic="Вчера вечером")
        rows = self._report(self.su).context_data["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["count"], 1)  # только сегодняшняя 00:00:01
        self.assertNotEqual(rows[0]["requests"][0]["time"], "23:59:59")

    def test_daily_report_access_and_scope(self):
        """Аноним — 403; prompt_developer видит только свои записи (скоуп)."""
        from django.contrib.auth.models import AnonymousUser

        self._log(user=self.su, external_user_id="101", full_name="Иванов Иван",
                  sent_at=self._msk_at(9, 0))
        response = self._report(AnonymousUser())
        self.assertEqual(response.status_code, 403)

        pd = self.user_model.objects.create_user(
            username="daily-pd", password="***",
        )
        make_prompt_developer(pd)
        self._log(user=pd, external_user_id="400", full_name="Разработчик Промптов",
                  sent_at=self._msk_at(8, 0), prompt="Мой промпт")
        rows = self._report(pd).context_data["rows"]
        self.assertEqual([r["display_name"] for r in rows], ["Разработчик Промптов"])
        self.assertEqual(rows[0]["count"], 1)

    def test_daily_report_group_school_titles_and_filters(self):
        """«Буквенная информация»: группа/вуз-школа приходят из DL
        (education.groupName/schoolName — DL отдаёт их с версии 1.4.2 API);
        если названия нет — сырой ключ + честная пометка «нет названия»
        (без аккаунта → «—»); группа и вуз/школа — СВОБОДНЫЙ ввод: фильтр
        по подстроке названия ИЛИ по сырому ID из DL (регистронезависимо)."""
        from ai.models import ExternalDLAccount

        ivanov = "Иванов Иван"
        petrov = "Петров Пётр"
        student = self.user_model.objects.create_user(username="gstudent", password="***")
        ExternalDLAccount.objects.create(
            user=student, external_user_id="101", external_login="gstudent",
            education_school_id="S-1", education_group_mask_id="G-1",
            education_school_name="ГГУ", education_group_name="ИТП-31",
        )
        petrov_user = self.user_model.objects.create_user(username="gstudent2", password="***")
        ExternalDLAccount.objects.create(
            user=petrov_user, external_user_id="200", external_login="gstudent2",
            education_school_id="S-9",  # названия от DL нет, группы нет
        )

        self._log(user=self.su, external_user_id="101", full_name=ivanov,
                  sent_at=self._msk_at(9, 15))
        self._log(user=self.su, external_user_id="200", full_name=petrov,
                  sent_at=self._msk_at(10, 0))

        report = self._report(self.su)
        rows = report.context_data["rows"]
        self.assertEqual([r["display_name"] for r in rows], [ivanov, petrov])
        self.assertEqual(rows[0]["group_title"], "ИТП-31")
        self.assertEqual(rows[0]["school_title"], "ГГУ")
        self.assertTrue(rows[0]["group_has_title"])
        self.assertTrue(rows[0]["school_has_title"])
        # Названия от DL нет → сырой id; группы нет вообще → «—».
        self.assertEqual(rows[1]["school_title"], "S-9")
        self.assertEqual(rows[1]["group_title"], "—")
        # Названия нет — честный признак (в UI «нет названия»).
        self.assertFalse(rows[1]["school_has_title"])
        self.assertEqual(rows[1]["school_key"], "S-9")
        self.assertFalse(rows[1]["group_has_title"])

        by_group = self._report(self.su, data={"group": "ИТП-31"}).context_data
        self.assertEqual([r["display_name"] for r in by_group["rows"]], [ivanov])
        self.assertEqual(by_group["filters_group"], "ИТП-31")
        by_school = self._report(self.su, data={"school": "ГГУ"}).context_data
        self.assertEqual([r["display_name"] for r in by_school["rows"]], [ivanov])

        # Свободный ввод: часть названия (регистр не важен).
        part = self._report(self.su, data={"group": "тп-3"}).context_data
        self.assertEqual([r["display_name"] for r in part["rows"]], [ivanov])
        # Сырой ID из DL — тоже находится (названия может не быть вовсе).
        by_key = self._report(self.su, data={"group": "G-1"}).context_data
        self.assertEqual([r["display_name"] for r in by_key["rows"]], [ivanov])
        school_key = self._report(self.su, data={"school": "S-9"}).context_data
        self.assertEqual([r["display_name"] for r in school_key["rows"]], [petrov])
        # Не находит — пусто.
        self.assertEqual(self._report(self.su, data={"group": "нет такой"}).context_data["rows"], [])

    def test_daily_report_group_school_titles_from_manual_directory_fallback(self):
        """Фолбэк: названия от DL не пришли — берём из ручного справочника
        DLEducationName (он остаётся запасным источником)."""
        from ai.models import DLEducationName, ExternalDLAccount

        student = self.user_model.objects.create_user(username="gstudent", password="***")
        ExternalDLAccount.objects.create(
            user=student, external_user_id="101", external_login="gstudent",
            education_school_id="S-1", education_group_mask_id="G-1",
        )
        DLEducationName.objects.create(kind="school", key="S-1", title="ГГУ")
        DLEducationName.objects.create(kind="group", key="G-1", title="ИТП-31")

        self._log(user=self.su, external_user_id="101", full_name="Иванов Иван",
                  sent_at=self._msk_at(9, 15))

        rows = self._report(self.su).context_data["rows"]
        self.assertEqual(rows[0]["group_title"], "ИТП-31")
        self.assertEqual(rows[0]["school_title"], "ГГУ")
        self.assertTrue(rows[0]["group_has_title"])
        self.assertTrue(rows[0]["school_has_title"])

    def test_daily_report_student_filter_and_school_from_group(self):
        """Фильтр student — регистронезависимая подстрока ФИО, выбирается до
        фильтрации (суженные строки не урезают варианты селектов); школа ОТ
        ГРУППЫ: школьные поля студента пусты (school_display вернул «—»),
        но его группа привязана к заведению в справочнике — показываем
        заведение группы."""
        from ai.models import DLEducationName, ExternalDLAccount

        ivanov = "Иванов Иван"
        petrov = "Петров Пётр"
        sidorov = "Сидоров Сидор"
        for uid, username in (("101", "sfilt1"), ("200", "sfilt2"), ("300", "sfilt3")):
            user = self.user_model.objects.create_user(username=username, password="***")
            ExternalDLAccount.objects.create(
                user=user, external_user_id=uid, external_login=username,
                # Сидоров (300): только группа — школьных полей нет вообще.
                education_school_id="S-1" if uid == "101" else ("S-9" if uid == "200" else ""),
                education_group_mask_id={"101": "G-1", "200": "", "300": "G-2"}[uid],
            )
        DLEducationName.objects.create(kind="school", key="S-1", title="ГГУ")
        DLEducationName.objects.create(kind="group", key="G-1", title="ИТП-31", school="S-1")
        DLEducationName.objects.create(kind="group", key="G-2", title="КЛ-9А", school="S-1")

        self._log(user=self.su, external_user_id="101", full_name=ivanov, sent_at=self._msk_at(9, 15))
        self._log(user=self.su, external_user_id="200", full_name=petrov, sent_at=self._msk_at(10, 0))
        self._log(user=self.su, external_user_id="300", full_name=sidorov, sent_at=self._msk_at(11, 0))

        # Фолбэк «школа от группы»: школа студента пуста → заведение группы.
        no_filter = self._report(self.su).context_data
        rows = no_filter["rows"]
        self.assertEqual(rows[2]["display_name"], sidorov)  # Сидоров — позже всех
        self.assertEqual(rows[2]["school_title"], "ГГУ")
        self.assertEqual(rows[2]["group_title"], "КЛ-9А")

        # Подстрока ФИО без учёта регистра («ИВАНОВ» и «иванов» равнозначны).
        by_student = self._report(self.su, data={"student": "ИВАНОВ"}).context_data
        self.assertEqual([r["display_name"] for r in by_student["rows"]], [ivanov])
        self.assertEqual(by_student["filters_student"], "ИВАНОВ")
        # Часть ФИО тоже находит.
        by_part = self._report(self.su, data={"student": "тров"}).context_data
        self.assertEqual([r["display_name"] for r in by_part["rows"]], [petrov])
        # Не находит — пусто.
        empty = self._report(self.su, data={"student": "кургузов"}).context_data
        self.assertEqual(empty["rows"], [])

    def test_daily_report_template_filters_and_columns(self):
        """Шаблон отчёта: два GET-фильтра над таблицей, колонки «Группа» и
        «Вуз/Школа» после «Фамилия студента», развёртка colspan=8.
        Группа и вуз/школа — СВОБОДНЫЙ ввод (input[type=text]), без
        select-списков и datalist-подсказок (названия приходят из DL)."""
        from django.template.loader import get_template
        src = get_template("admin/ai/daily_report.html").template.source
        self.assertIn('id="daily-filter-group"', src)
        self.assertIn('id="daily-filter-school"', src)
        # Свободный ввод, а не select: текстовые поля без list=…/datalist.
        self.assertIn('name="group"', src)
        self.assertIn('name="school"', src)
        self.assertNotIn("daily-group-options", src)
        self.assertNotIn("daily-school-options", src)
        self.assertNotIn("group_suggestions", src)
        self.assertNotIn("school_suggestions", src)
        self.assertNotIn("<select", src)
        # Честная пометка «нет названия» для ключей без названия.
        self.assertIn("ai-daily-no-title", src)
        head = src.index("<thead>")
        self.assertLess(head, src.index("<th>Группа</th>"))
        self.assertLess(src.index("<th>Группа</th>"), src.index("<th>Вуз/Школа</th>"))
        name_th = src.index("<th>Фамилия студента</th>")
        self.assertLess(name_th, src.index("<th>Группа</th>"))
        self.assertIn('colspan="8"', src)
        self.assertNotIn('colspan="6"', src)


def _fake_translate_chunk(counter):
    """Мок _translate_chunk: считает вызовы, возвращает «[lang]текст»."""
    def fake(chunk, google_lang):
        counter.append(1)
        return f"[{google_lang}]{chunk}"
    return fake


class DocTranslationTests(SimpleTestCase):
    """Перевод дока (deep-translator): структура markdown сохраняется
    (код/таблицы не переводятся), кэш ключом (slug, lang, fingerprint),
    частичный перевод не кэшируется, фолбэк — русский текст."""

    def setUp(self):
        from django.core.cache import cache
        cache.clear()

    _DOC = (
        "## Инструкция для пользователя\n\n"
        "Привет, мир!\n\n"
        "```python\nprint('| не таблица |')\n```\n\n"
        "| Код | Имя |\n| --- | --- |\n| 1 | а |\n"
    )

    def test_translate_markdown_structure(self):
        from ai.services.doc_translation import translate_markdown
        counter = []
        with patch("ai.services.doc_translation._translate_chunk", _fake_translate_chunk(counter)):
            out, failed = translate_markdown(self._DOC, "en")
        self.assertEqual(failed, 0)
        # Код и таблицы прошли без изменений, текст переведён.
        self.assertIn("```python\nprint('| не таблица |')\n```", out)
        self.assertIn("| Код | Имя |", out)
        self.assertIn("[en]Привет, мир!", out)
        # Заголовок: маркер «## » защищён и восстановлен.
        self.assertIn("[en]## Инструкция", out)
        self.assertNotIn("@@PH", out)

    def test_ru_passes_through_without_translator(self):
        from ai.services.doc_translation import translate_markdown
        with patch("ai.services.doc_translation._translate_chunk") as mock:
            out, failed = translate_markdown(self._DOC, "ru")
        mock.assert_not_called()
        self.assertEqual(out, self._DOC)
        self.assertEqual(failed, 0)

    def test_failure_falls_back_to_original(self):
        from ai.services.doc_translation import translate_markdown

        def boom(chunk, google_lang):
            raise RuntimeError("google down")

        # Ретрай спит 1 с — в тесте не ждём.
        with (
            patch("ai.services.doc_translation._translate_chunk", boom),
            patch("ai.services.doc_translation.time.sleep"),
        ):
            out, failed = translate_markdown(self._DOC, "en")
        self.assertEqual(failed, 1)
        self.assertEqual(out, self._DOC)  # блок остался в оригинале

    def test_render_chapter_html_en_title(self):
        from ai.services import docs as docs_module
        calls = []
        with (
            patch.object(docs_module, "_chapter_markdown", return_value=self._DOC),
            patch.object(docs_module, "chapter_fingerprint", return_value=(1000, 500)),
            patch("ai.services.doc_translation._translate_chunk", _fake_translate_chunk(calls)),
        ):
            rendered = docs_module.render_chapter_html("user", lang="en")
        self.assertEqual(rendered["title"], "User Guide")
        self.assertEqual(rendered["lang"], "en")
        self.assertEqual(rendered["fingerprint"], (1000, 500))
        self.assertIn("[en]Привет, мир!", rendered["html"])

    def test_render_fr_title(self):
        from ai.services import docs as docs_module
        calls = []
        with (
            patch.object(docs_module, "_chapter_markdown", return_value=self._DOC),
            patch.object(docs_module, "chapter_fingerprint", return_value=(1000, 500)),
            patch("ai.services.doc_translation._translate_chunk", _fake_translate_chunk(calls)),
        ):
            rendered = docs_module.render_chapter_html("user", lang="fr")
        self.assertEqual(rendered["title"], "Guide de l'utilisateur")
        self.assertIn("[fr]Привет, мир!", rendered["html"])

    def test_translation_cached_by_fingerprint(self):
        from django.core.cache import cache

        from ai.services import docs as docs_module
        calls = []
        with (
            patch.object(docs_module, "_chapter_markdown", return_value=self._DOC),
            patch.object(docs_module, "chapter_fingerprint", return_value=(1000, 500)),
            patch("ai.services.doc_translation._translate_chunk", _fake_translate_chunk(calls)),
        ):
            first = docs_module.render_chapter_html("user", lang="en")
            # 2 абзаца в текстовом блоке → 2 куска на перевод.
            self.assertEqual(len(calls), 2)
            second = docs_module.render_chapter_html("user", lang="en")
            self.assertEqual(len(calls), 2)  # второй запрос из кэша
        self.assertEqual(first["html"], second["html"])
        self.assertIn("[en]Привет, мир!", second["html"])
        cache.clear()

    def test_translation_cache_broken_by_fingerprint_change(self):
        """Правка файла → новый fingerprint → старый кэш не используется."""
        from ai.services import docs as docs_module
        calls = []
        # Один stat() на рендер; после первого рендера файл «изменился» →
        # другой отпечаток → кэш мимо.
        fp_state = {"n": 0}

        def fake_fingerprint(slug):
            fp_state["n"] += 1
            return (1000, 500) if fp_state["n"] <= 1 else (1001, 600)

        with (
            patch.object(docs_module, "_chapter_markdown", return_value=self._DOC),
            patch.object(docs_module, "chapter_fingerprint", side_effect=fake_fingerprint),
            patch("ai.services.doc_translation._translate_chunk", _fake_translate_chunk(calls)),
        ):
            docs_module.render_chapter_html("user", lang="en")
            docs_module.render_chapter_html("user", lang="en")
        self.assertEqual(len(calls), 4)  # отпечаток изменился → перевод снова (2 абзаца × 2)

    def test_partial_translation_not_cached(self):
        """failed_count > 0 → кэш не пишется; повторный запрос снова переводит."""
        from ai.services import docs as docs_module

        calls = []

        def flaky(chunk, google_lang):
            calls.append(1)
            if len(calls) <= 4:
                raise RuntimeError("google down")
            return f"[{google_lang}]{chunk}"

        # 1 текстовый блок в _DOC; каждый блок ретраится один раз (sleep не ждём):
        # первый рендер — 2 неудачных вызова, второй — ещё 2 (кэша нет).
        with (
            patch.object(docs_module, "_chapter_markdown", return_value=self._DOC),
            patch.object(docs_module, "chapter_fingerprint", return_value=(1000, 500)),
            patch("ai.services.doc_translation._translate_chunk", flaky),
            patch("ai.services.doc_translation.time.sleep"),
        ):
            first = docs_module.render_chapter_html("user", lang="en")
            self.assertEqual(len(calls), 2)
            self.assertEqual(first["lang"], "en")
            self.assertIn("Привет, мир!", first["html"])  # фолбэк на русский
            second = docs_module.render_chapter_html("user", lang="en")
            self.assertEqual(len(calls), 4)  # кэша нет → переводится заново

    def test_render_chapter_html_translated_flag(self):
        """Честный флаг сбоя рантайм-перевода: успешный EN-рендер — True,
        фолбэк на русский (Google недоступен) — False; родной RU — True.
        Плашка на клиенте строится по payload «?» (views chat_user_docs_view)."""
        from ai.services import docs as docs_module
        calls = []
        with (
            patch.object(docs_module, "_chapter_markdown", return_value=self._DOC),
            patch.object(docs_module, "chapter_fingerprint", return_value=(1000, 500)),
            patch("ai.services.doc_translation._translate_chunk", _fake_translate_chunk(calls)),
        ):
            ok = docs_module.render_chapter_html("user", lang="en")
        self.assertTrue(ok["translated"])

        def boom(chunk, google_lang):
            raise RuntimeError("google down")

        # Отпечаток МЕНЯЕМ: иначе «провальный» рендер достал бы из кэша
        # успешный перевод первого рендера (тот же slug/lang/fingerprint).
        with (
            patch.object(docs_module, "_chapter_markdown", return_value=self._DOC),
            patch.object(docs_module, "chapter_fingerprint", return_value=(1001, 600)),
            patch("ai.services.doc_translation._translate_chunk", boom),
            patch("ai.services.doc_translation.time.sleep"),
        ):
            failed = docs_module.render_chapter_html("user", lang="en")
        self.assertFalse(failed["translated"])
        # Родной язык — «перевода не было», плашка не показывается.
        with patch.object(docs_module, "_chapter_markdown", return_value=self._DOC):
            ru = docs_module.render_chapter_html("user", lang="ru")
        self.assertTrue(ru["translated"])

    def test_fingerprint_missing_file_raises(self):
        from pathlib import Path
        from unittest.mock import patch
        from ai.services import docs as docs_module
        with patch.object(docs_module, "_base_dir", return_value=Path("/nonexistent")):
            with self.assertRaises(docs_module.DocUnavailableError):
                docs_module.chapter_fingerprint("user")

    def test_read_chapter_markdown_lang_title(self):
        from ai.services import docs as docs_module
        calls = []
        with (
            patch.object(docs_module, "_chapter_markdown", return_value=self._DOC),
            patch.object(docs_module, "chapter_fingerprint", return_value=(1000, 500)),
            patch("ai.services.doc_translation._translate_chunk", _fake_translate_chunk(calls)),
        ):
            data = docs_module.read_chapter_markdown("user", lang="en")
        self.assertEqual(data["title"], "User Guide")
        self.assertEqual(data["filename"], "User Guide.md")
        self.assertIn("[en]Привет", data["text"])


class DocsContentEndpointTests(_AdminViewRequestMixin, TestCase):
    """JSON-эндпоинт /ai/admin/docs/<slug>/content/ — поллинг изменений главы:
    права как у страницы, payload {success, title, html, fingerprint}."""

    def setUp(self):
        self.factory = RequestFactory()
        self.user_model = get_user_model()

    def _request(self, user):
        from ai.admin import docs as docs_views
        from unittest.mock import patch
        request = self._admin_request(user, path="/ai/admin/docs/developer/content/")
        with patch("ai.admin.site.get_external_user_id_from_request", return_value="12345"):
            return docs_views.admin_docs_content_view(request, slug="developer")

    def test_payload_for_staff(self):
        user = self.user_model.objects.create_user(username="cnt-staff", password="x", is_staff=True)
        response = self._request(user)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertTrue(data["success"])
        self.assertEqual(len(data["fingerprint"]), 2)
        self.assertIn("<h", data["html"])

    def test_forbidden_for_other_role(self):
        user = self.user_model.objects.create_user(username="cnt-pd", password="x")
        make_prompt_developer(user)
        self.assertEqual(self._request(user).status_code, 403)


class ChatUserDocsLangTests(TestCase):
    """?lang= модалки «?»: язык интерфейса → суффикс (ru/en/fr), перевод
    главы, локализованный заголовок и имя скачиваемого файла."""

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.factory = RequestFactory()
        self.user_model = get_user_model()
        self.patcher = patch(
            "ai.views.get_external_user_id_from_request", return_value="12345"
        )
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_en_lang_translates_and_localizes_title(self):
        from ai.services import docs as docs_module
        from unittest.mock import patch
        from ai.views import chat_user_docs_view

        request = self.factory.get("/ai/docs/", {"lang": "English"})
        request.user = self.user_model.objects.create_user(username="lang-en", password="x")
        with (
            patch.object(docs_module, "_chapter_markdown", return_value=DocTranslationTests._DOC),
            patch("ai.services.doc_translation._translate_chunk",
                  _fake_translate_chunk([])),
        ):
            response = chat_user_docs_view(request)
        data = json.loads(response.content)
        self.assertEqual(data["title"], "User Guide")
        self.assertEqual(data["lang"], "en")
        self.assertEqual(data["download_url"], "/ai/docs/download/?lang=en")
        self.assertIn("[en]Привет", data["html"])

    def test_french_download_filename(self):
        from ai.services import docs as docs_module
        from unittest.mock import patch
        from ai.views import chat_user_docs_download_view

        request = self.factory.get("/ai/docs/download/", {"lang": "French"})
        request.user = self.user_model.objects.create_user(username="lang-fr", password="x")
        with (
            patch.object(docs_module, "_chapter_markdown", return_value=DocTranslationTests._DOC),
            patch("ai.services.doc_translation._translate_chunk",
                  _fake_translate_chunk([])),
        ):
            response = chat_user_docs_download_view(request)
        self.assertIn("filename*=UTF-8''Guide%20de%20l", response["Content-Disposition"])
        self.assertIn("[fr]Привет", response.content.decode("utf-8"))

    def test_unknown_lang_falls_back_to_ru(self):
        from ai.views import chat_user_docs_view

        request = self.factory.get("/ai/docs/", {"lang": "Esperanto"})
        request.user = self.user_model.objects.create_user(username="lang-xx", password="x")
        response = chat_user_docs_view(request)
        data = json.loads(response.content)
        self.assertEqual(data["lang"], "ru")


class SendSolutionViewTests(TestCase):
    """send_solution_view / get_solution_result_view — URL-прокси к DL REST
    для кнопки «Тестирование» страницы «Реши задачу» (/ai/api/send-solution/,
    /ai/api/get-solution-result/ в DjangoTest/urls.py). fileExtension можно не
    передавать: расширение резолвится серверно из языка программирования через
    _guess_extension (единый источник соответствия язык→расширение)."""

    def setUp(self):
        self.factory = RequestFactory()
        self.superuser = get_user_model().objects.create_superuser(
            username="ss_admin", password="***", email="ss@t.com",
        )

    def _post_send(self, body, user_info=None):
        from ai.views import send_solution_view
        request = self.factory.post(
            "/ai/api/send-solution/", data=json.dumps(body),
            content_type="application/json",
        )
        request.user = self.superuser
        request.session = {}
        # _has_page_access требует внешний id (userId cookie / user_info).
        request.COOKIES = {"userId": self.superuser.username}
        if user_info is not None:
            request.user_info = user_info
        return send_solution_view(request)

    def _post_result(self, body, user_info=None):
        from ai.views import get_solution_result_view
        request = self.factory.post(
            "/ai/api/get-solution-result/", data=json.dumps(body),
            content_type="application/json",
        )
        request.user = self.superuser
        request.session = {}
        # _has_page_access требует внешний id (userId cookie / user_info).
        request.COOKIES = {"userId": self.superuser.username}
        if user_info is not None:
            request.user_info = user_info
        return get_solution_result_view(request)

    @patch("ai.dl_api_client.send_solution_to_dl")
    def test_extension_resolved_from_language_name(self, mock_send):
        """progLanguageName без fileExtension → расширение через _guess_extension
        («Ассемблер i8086» → «.i86»), payload уходит в DL с ним."""
        mock_send.return_value = {"queueId": 42, "message": "ok"}
        response = self._post_send({
            "sessionId": "SID", "nodeId": 100, "code": "mov ax, 1",
            "courseId": 1450, "progLanguageName": "Ассемблер i8086",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content)["queueId"], 42)
        self.assertEqual(mock_send.call_args.args[3], ".i86")

    @patch("ai.dl_api_client.send_solution_to_dl")
    def test_extension_resolved_from_language_id(self, mock_send):
        ProgrammingLanguage.objects.create(language_name="C-MPA (С-МПА)")
        lang = ProgrammingLanguage.objects.get(language_name="C-MPA (С-МПА)")
        mock_send.return_value = {"queueId": 7, "message": "ok"}
        response = self._post_send({
            "sessionId": "SID", "nodeId": 100, "code": "int main(){}",
            "courseId": 1450, "progLanguageId": lang.id,
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mock_send.call_args.args[3], ".mpc")

    def test_no_extension_and_no_language_returns_400(self):
        response = self._post_send({
            "sessionId": "SID", "nodeId": 100, "code": "x", "courseId": 1450,
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", json.loads(response.content))

    def test_missing_node_id_returns_400(self):
        response = self._post_send({
            "sessionId": "SID", "code": "x", "courseId": 1450,
            "progLanguageName": "Pascal",
        })
        self.assertEqual(response.status_code, 400)

    @patch("ai.dl_api_client.send_solution_to_dl")
    def test_dl_error_mapped_to_json_response(self, mock_send):
        from ai.dl_api_client import DLUnauthorizedError
        mock_send.side_effect = DLUnauthorizedError()
        response = self._post_send({
            "sessionId": "SID", "nodeId": 100, "code": "x", "courseId": 1450,
            "progLanguageName": "Pascal",
        })
        self.assertEqual(response.status_code, 401)

    @patch("ai.dl_api_client.get_solution_result_from_dl")
    def test_result_proxied(self, mock_poll):
        mock_poll.return_value = {"isFinished": True, "comment": "Все тесты успешно пройдены"}
        response = self._post_result({"sessionId": "SID", "queueId": 42})
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertTrue(data["isFinished"])
        self.assertEqual(data["comment"], "Все тесты успешно пройдены")
        self.assertEqual(mock_poll.call_args.args[1], 42)

    def test_result_missing_queue_id_returns_400(self):
        response = self._post_result({"sessionId": "SID"})
        self.assertEqual(response.status_code, 400)

    def test_urls_wired(self):
        """Маршруты /ai/api/send-solution/ и /ai/api/get-solution-result/
        подключены в DjangoTest/urls.py (до этого вьюхи были мертвы)."""
        from django.urls import reverse
        self.assertEqual(reverse("send_solution"), "/ai/api/send-solution/")
        self.assertEqual(reverse("get_solution_result"), "/ai/api/get-solution-result/")

    @patch("ai.dl_api_client.send_solution_to_dl")
    def test_prog_language_id_persisted_with_language_name(self, mock_send):
        """Фронтенд шлёт И progLanguageId, И progLanguageName — ID должен
        записаться в TaskSolution.programming_language_id, иначе
        find_passed_solution(node, lang_id) никогда не найдёт запись и кэш
        решённых задач не сработает (модель вызывается повторно)."""
        lang = ProgrammingLanguage.objects.create(language_name="Pascal")
        mock_send.return_value = {"queueId": 42, "message": "ok"}
        response = self._post_send({
            "sessionId": "SID", "nodeId": 100, "code": "begin end.",
            "courseId": 1450, "progLanguageId": lang.id,
            "progLanguageName": "Pascal",
        })
        self.assertEqual(response.status_code, 200)
        solution = TaskSolution.objects.get(task_node_id=100)
        self.assertEqual(solution.programming_language_id, lang.id)
        self.assertEqual(solution.verdict, TaskSolution.VERDICT_PENDING)
        self.assertEqual(solution.queue_id, 42)

    @patch("ai.dl_api_client.send_solution_to_dl")
    def test_prog_language_id_persisted_with_explicit_file_extension(self, mock_send):
        """ID языка пишется в кэш и при явном fileExtension (парсинг ID
        вынесен из-под if not file_extension)."""
        lang = ProgrammingLanguage.objects.create(language_name="Pascal")
        mock_send.return_value = {"queueId": 43, "message": "ok"}
        response = self._post_send({
            "sessionId": "SID", "nodeId": 101, "code": "begin end.",
            "courseId": 1450, "fileExtension": ".pas", "progLanguageId": lang.id,
        })
        self.assertEqual(response.status_code, 200)
        solution = TaskSolution.objects.get(task_node_id=101)
        self.assertEqual(solution.programming_language_id, lang.id)

    @patch("ai.dl_api_client.fetch_task_info")
    @patch("ai.dl_api_client.send_solution_to_dl")
    def test_tree_path_persisted_from_task_info(self, mock_send, mock_info):
        """Приписка в дереве задач: tree_path пишется best-effort из
        get-task-info (поле path) после записи кэша — приписка появляется
        в списке «Решённые задачи» (шаблон tasksolution_changelist)."""
        mock_send.return_value = {"queueId": 44, "message": "ok"}
        mock_info.return_value = {
            "taskId": 111, "name": "Сумма",
            "path": "Программирование\\Линейные\\Сумма",
        }
        response = self._post_send({
            "sessionId": "SID", "nodeId": 102, "code": "begin end.",
            "courseId": 1450, "fileExtension": ".pas",
        })
        self.assertEqual(response.status_code, 200)
        solution = TaskSolution.objects.get(task_node_id=102)
        self.assertEqual(solution.tree_path, "Программирование\\Линейные\\Сумма")
        kwargs = mock_info.call_args.kwargs
        self.assertEqual(kwargs.get("session_id"), "SID")
        self.assertEqual(kwargs.get("course_id"), 1450)

    @patch("ai.dl_api_client.fetch_task_info")
    @patch("ai.dl_api_client.send_solution_to_dl")
    def test_tree_path_fetch_failure_does_not_break_submission(self, mock_send, mock_info):
        """Сбой get-task-info (путь в дереве) не ломает отправку решения и
        запись кэша — приписка best-effort, повторное решение допишет путь."""
        mock_send.return_value = {"queueId": 45, "message": "ok"}
        mock_info.side_effect = Exception("DL down")
        response = self._post_send({
            "sessionId": "SID", "nodeId": 103, "code": "begin end.",
            "courseId": 1450, "fileExtension": ".pas",
        })
        self.assertEqual(response.status_code, 200)
        solution = TaskSolution.objects.get(task_node_id=103)
        self.assertEqual(solution.tree_path, "")

    @patch("ai.dl_api_client.send_solution_to_dl")
    def test_topic_and_prompt_persisted_from_last_solve_log(self, mock_send):
        """Тема/промпт берутся из журнала последней успешной генерации
        решения (mode=solve); запись выдачи из кэша (CACHE_SERVE_LOG_MESSAGE)
        контекстом не считается и существующие значения не затирает."""
        from ai.services import CACHE_SERVE_LOG_MESSAGE
        lang = ProgrammingLanguage.objects.create(language_name="Pascal")
        topic = Topic.objects.create(topic_name_ru="Циклы")
        AIRequestLog.objects.create(
            source=AIRequestLog.SOURCE_WEBSOCKET, mode=AIRequestLog.MODE_SOLVE,
            status=AIRequestLog.STATUS_SUCCESS, sent_at=timezone.now(),
            model_names=["m1"], task_node_id=100,
            topic_id=topic.id, topic_name="Циклы",
            prompt_id=5, prompt_name="Реши по шагам",
        )
        # Более поздняя запись выдачи из кэша — не должна стать «последней генерацией».
        AIRequestLog.objects.create(
            source=AIRequestLog.SOURCE_WEBSOCKET, mode=AIRequestLog.MODE_SOLVE,
            status=AIRequestLog.STATUS_SUCCESS, sent_at=timezone.now(),
            model_names=["m1"], task_node_id=100,
            message=CACHE_SERVE_LOG_MESSAGE,
        )
        mock_send.return_value = {"queueId": 44, "message": "ok"}
        response = self._post_send({
            "sessionId": "SID", "nodeId": 100, "code": "begin end.",
            "courseId": 1450, "progLanguageId": lang.id, "progLanguageName": "Pascal",
        })
        self.assertEqual(response.status_code, 200)
        solution = TaskSolution.objects.get(task_node_id=100)
        self.assertEqual(solution.topic_id, topic.id)
        self.assertEqual(solution.topic_name, "Циклы")
        self.assertEqual(solution.prompt_id, 5)
        self.assertEqual(solution.prompt_name, "Реши по шагам")

    @patch("ai.dl_api_client.get_solution_result_from_dl")
    def test_result_records_verdict_in_cache(self, mock_poll):
        """Финальный вердикт DL закрывает кэш: passed, queue_id очищен,
        комментарий записан — только после этого сработает выдача из кэша."""
        TaskSolution.objects.create(
            task_node_id=100, programming_language_id=7, code="begin end.",
            queue_id=42, verdict=TaskSolution.VERDICT_PENDING,
        )
        mock_poll.return_value = {"isFinished": True, "comment": "Все тесты успешно пройдены"}
        response = self._post_result({"sessionId": "SID", "queueId": 42})
        self.assertEqual(response.status_code, 200)
        solution = TaskSolution.objects.get(task_node_id=100)
        self.assertEqual(solution.verdict, TaskSolution.VERDICT_PASSED)
        self.assertIsNone(solution.queue_id)
        self.assertEqual(solution.dl_comment, "Все тесты успешно пройдены")


class SolutionPollRateLimitTests(SimpleTestCase):
    """Поллинг результата DL-тестирования (POST /ai/api/get-solution-result/,
    ~1 раз в 3 с) идёт в отдельный poll-лимитер, а не в action-бюджет
    пользователя; сама отправка (send-solution) остаётся обычным действием."""

    def test_get_solution_result_is_poll(self):
        from ai.throttling import _is_poll_request
        request = RequestFactory().post("/ai/api/get-solution-result/")
        self.assertTrue(_is_poll_request(request))

    def test_send_solution_is_not_poll(self):
        from ai.throttling import _is_poll_request
        request = RequestFactory().post("/ai/api/send-solution/")
        self.assertFalse(_is_poll_request(request))

    def test_get_poll_still_requires_get_method(self):
        from ai.throttling import _is_poll_request
        # POST по GET-пути из _POLL_PATHS — не poll (регрессия на новую ветку).
        request = RequestFactory().post("/ai/admin/arm/models/state/")
        self.assertFalse(_is_poll_request(request))


class PromptOwnProgrammingLanguageTests(TestCase):
    """Собственный FK-язык промпта (миграция 0048): выбор языка в админке —
    настоящий селект, значение СОХРАНЯЕТСЯ (раньше поле было виртуальным и
    язык существовал только через тему), промпт без темы работает «на весь
    язык» и попадает в фронтовые сериализаторы."""

    def setUp(self):
        self.python_language = ProgrammingLanguage.objects.create(language_name="Python")
        self.c_language = ProgrammingLanguage.objects.create(language_name="C")
        self.python_topic = Topic.objects.create(
            topic_name_ru="Loops", programming_language=self.python_language,
        )

    def _form_data(self, **overrides):
        data = {
            "prompt_name_ru": "Prompt",
            "prompt_text_ru": "Body",
            "mode": Prompt.MODE_SOLVE,
            "programming_language": str(self.python_language.id),
 "topic": "",
        }
        data.update(overrides)
        return data

    def test_save_prompt_with_language_and_no_topic(self):
        from ai.admin.forms import PromptForm
        form = PromptForm(data=self._form_data())
        self.assertTrue(form.is_valid(), form.errors)
        prompt = form.save()
        self.assertEqual(prompt.programming_language_id, self.python_language.id)
        self.assertIsNone(prompt.topic_id)

    def test_edit_language_persists(self):
        from ai.admin.forms import PromptForm
        prompt = Prompt.objects.create(
            prompt_name_ru="Prompt", prompt_text_ru="Body",
            programming_language=self.python_language, mode=Prompt.MODE_SOLVE,
        )
        form = PromptForm(data=self._form_data(programming_language=str(self.c_language.id)), instance=prompt)
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        saved.refresh_from_db()
        self.assertEqual(saved.programming_language_id, self.c_language.id)

    def test_form_initial_uses_own_language_when_no_topic(self):
        from ai.admin.forms import PromptForm
        prompt = Prompt.objects.create(
            prompt_name_ru="Prompt", prompt_text_ru="Body",
            programming_language=self.c_language, mode=Prompt.MODE_SOLVE,
        )
        form = PromptForm(instance=prompt)
        self.assertEqual(form.fields["programming_language"].initial, self.c_language.id)

    def test_serializer_exposes_own_language(self):
        from ai.views import serialize_prompt
        prompt = Prompt.objects.create(
            prompt_name_ru="Prompt", prompt_text_ru="Body",
            programming_language=self.python_language, mode=Prompt.MODE_SOLVE,
        )
        data = serialize_prompt(prompt, "Русский")
        self.assertEqual(data["programming_language_id"], self.python_language.id)


class PinnedBatchRunsTests(TestCase):
    """«Закреплённые пакетные решения»: ★ из журнала/настроек, страница
    /ai/admin/pinned-runs/, права как у журнала, только завершённые прогоны."""

    def setUp(self):
        from ai.models import AIModelTestRun
        from ai.admin.pinned import (
            PINNED_RUNS_URL,
            admin_pinned_run_toggle_view,
            admin_pinned_runs_view,
        )
        self.pin_url_prefix = PINNED_RUNS_URL
        self.toggle_view = admin_pinned_run_toggle_view
        self.page_view = admin_pinned_runs_view
        self.run_id = "a" * 32
        self.operator = get_user_model().objects.create_superuser(
            username="pin_admin", password="x", email="p@t.com",
        )
        self.other = get_user_model().objects.create_superuser(
            username="pin_other", password="x", email="o@t.com",
        )
        self.log = AIRequestLog.objects.create(
            user=self.operator, source="arm", mode="batch_solve",
            message=f"Batch solve run {self.run_id}",
            status=AIRequestLog.STATUS_SUCCESS, sent_at=timezone.now(),
        )
        self.plain_log = AIRequestLog.objects.create(
            user=self.operator, source="websocket", mode="chat",
            message="hello", status=AIRequestLog.STATUS_SUCCESS, sent_at=timezone.now(),
        )
        self.test_run = AIModelTestRun.objects.create(
            run_id=self.run_id, run_type=AIModelTestRun.RUN_TYPE_BATCH,
            status=AIModelTestRun.STATUS_COMPLETED, course_id=1450,
        )
        AIModelTestRun  # noqa: keep import visible

    def _post(self, url, user):
        request = RequestFactory().post(url)
        request.user = user
        # each_context → guest_mode читает request.session.
        SessionMiddleware(lambda req: None).process_request(request)
        return request

    def test_toggle_pin_and_unpin(self):
        resp = self.toggle_view(self._post(f"/x/{self.log.id}/pin/", self.operator), self.log.id)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(json.loads(resp.content)["pinned"])
        self.assertEqual(AIPinnedBatchRun.objects.filter(user=self.operator, log=self.log).count(), 1)

        resp = self.toggle_view(self._post(f"/x/{self.log.id}/pin/", self.operator), self.log.id)
        self.assertFalse(json.loads(resp.content)["pinned"])
        self.assertFalse(AIPinnedBatchRun.objects.filter(user=self.operator, log=self.log).exists())

    def test_toggle_rejects_non_batch_log(self):
        resp = self.toggle_view(self._post(f"/x/{self.plain_log.id}/pin/", self.operator), self.plain_log.id)
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(AIPinnedBatchRun.objects.exists())

    def test_toggle_rejects_running_run(self):
        from ai.models import AIModelTestRun
        AIModelTestRun.objects.filter(run_id=self.run_id).update(status=AIModelTestRun.STATUS_RUNNING)
        resp = self.toggle_view(self._post(f"/x/{self.log.id}/pin/", self.operator), self.log.id)
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(AIPinnedBatchRun.objects.exists())

    def test_toggle_rejects_missing_run(self):
        ghost = AIRequestLog.objects.create(
            user=self.operator, source="arm", mode="batch_solve",
            message=f"Batch solve run {'b' * 32}",
            status=AIRequestLog.STATUS_SUCCESS, sent_at=timezone.now(),
        )
        resp = self.toggle_view(self._post(f"/x/{ghost.id}/pin/", self.operator), ghost.id)
        self.assertEqual(resp.status_code, 400)

    def test_page_shows_pins_in_pin_order(self):
        from ai.models import AIModelTestRun
        run2 = "c" * 32
        log2 = AIRequestLog.objects.create(
            user=self.other, source="arm", mode="batch_solve",
            message=f"Batch solve run {run2}",
            status=AIRequestLog.STATUS_SUCCESS, sent_at=timezone.now(),
        )
        AIModelTestRun.objects.create(
            run_id=run2, run_type=AIModelTestRun.RUN_TYPE_BATCH,
            status=AIModelTestRun.STATUS_COMPLETED, course_id=1451,
        )
        self.toggle_view(self._post(f"/x/{self.log.id}/pin/", self.operator), self.log.id)
        self.toggle_view(self._post(f"/x/{log2.id}/pin/", self.operator), log2.id)

        request = RequestFactory().get(self.pin_url_prefix)
        request.user = self.operator
        SessionMiddleware(lambda req: None).process_request(request)
        resp = self.page_view(request)
        self.assertEqual(resp.status_code, 200)
        rows = resp.context_data["rows"]
        self.assertEqual([r["id"] for r in rows], [log2.id, self.log.id])
        self.assertTrue(all(r["pinned"] for r in rows))

    def test_page_lists_other_users_pins_with_authors(self):
        """Общий вид: закрепления чужих пользователю видны, автор показан,
        is_own различает своё/чужое; открепить можно только своё."""
        run2 = "d" * 32
        log2 = AIRequestLog.objects.create(
            user=self.operator, source="arm", mode="batch_solve",
            message=f"Batch solve run {run2}",
            status=AIRequestLog.STATUS_SUCCESS, sent_at=timezone.now(),
        )
        from ai.models import AIModelTestRun
        AIModelTestRun.objects.create(
            run_id=run2, run_type=AIModelTestRun.RUN_TYPE_BATCH,
            status=AIModelTestRun.STATUS_COMPLETED, course_id=1452,
        )
        # Чужое закрепление (other закрепляет лог, который ведёт оператор)…
        self.toggle_view(self._post(f"/x/{self.log.id}/pin/", self.other), self.log.id)
        # …и своё.
        self.toggle_view(self._post(f"/x/{log2.id}/pin/", self.operator), log2.id)

        request = RequestFactory().get(self.pin_url_prefix)
        request.user = self.operator
        SessionMiddleware(lambda req: None).process_request(request)
        resp = self.page_view(request)
        rows_by_id = {r["id"]: r for r in resp.context_data["rows"]}
        self.assertIn(self.log.id, rows_by_id)
        self.assertIn(log2.id, rows_by_id)
        foreign = rows_by_id[self.log.id]
        own = rows_by_id[log2.id]
        self.assertFalse(foreign["is_own"])
        self.assertTrue(own["is_own"])
        self.assertIn("pin_other", foreign["pin_authors"])
        # Суперюзер: скупа «только свои» нет — чужой прогон раскрывается.
        self.assertTrue(foreign["snapshot_url"])

    def test_page_for_prompt_developer_keeps_foreign_snapshot(self):
        """Общий список виден и разработчику промтов, и смотреть результаты
        ЧУЖИХ закреплённых прогонов он может (ТЗ 2026-10-19: закреплённые
        решения — общая витрина): snapshot_url остаётся; открепить чужое
        он всё равно не может (is_own=false; toggle — только свои записи)."""
        pd_user = get_user_model().objects.create_user(
            username="pin_pd", password="x",
        )
        make_prompt_developer(pd_user)
        self.toggle_view(self._post(f"/x/{self.log.id}/pin/", self.operator), self.log.id)

        request = RequestFactory().get(self.pin_url_prefix)
        request.user = pd_user
        SessionMiddleware(lambda req: None).process_request(request)
        resp = self.page_view(request)
        self.assertEqual(resp.status_code, 200)
        rows = resp.context_data["rows"]
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["is_own"])
        self.assertIn("pin_admin", rows[0]["pin_authors"])
        self.assertEqual(
            rows[0]["snapshot_url"],
            f"/ai/admin/ai/request_logs/{self.log.id}/batch-snapshot/",
        )

    def test_snapshot_serves_foreign_pinned_log_to_prompt_dev(self):
        """Снапшот-эндпоинт: чужой ЗАКРЕПЛЁННЫЙ прогон промпт-разработчику
        отдаётся (200 + results), незакреплённый чужой — 404 (скуп «только
        свои» журнала на него больше не распространяется только при закреплении)."""
        from ai.admin.request_logs import admin_batch_snapshot_view
        from ai.models import AIModelTestResult, AIModelTestRun

        run2 = "b" * 32
        log2 = AIRequestLog.objects.create(
            user=self.operator, source="arm", mode="batch_solve",
            message=f"Batch solve run {run2}",
            status=AIRequestLog.STATUS_SUCCESS, sent_at=timezone.now(),
        )
        AIModelTestRun.objects.create(
            run_id=run2, run_type=AIModelTestRun.RUN_TYPE_BATCH,
            status=AIModelTestRun.STATUS_COMPLETED, course_id=1451,
        )
        AIModelTestResult.objects.create(
            run=AIModelTestRun.objects.get(run_id=run2),
            model_key="FakeModel", model_title="FakeModel",
            status=AIModelTestResult.STATUS_OK, verdict="solved",
            raw_response="jmp begin", code="jmp begin",
        )

        pd_user = get_user_model().objects.create_user(
            username="snap_pd", password="x",
        )
        make_prompt_developer(pd_user)

        # Не закреплённый чужой прогон — скуп прячет → 404.
        request = RequestFactory().get(f"/ai/admin/ai/request_logs/{log2.id}/batch-snapshot/")
        request.user = pd_user
        SessionMiddleware(lambda req: None).process_request(request)
        resp = admin_batch_snapshot_view(request, log_id=log2.id)
        self.assertEqual(resp.status_code, 404)

        # Закрепили (оператор) — теперь витрина: 200 с полным снапшотом.
        self.toggle_view(self._post(f"/x/{log2.id}/pin/", self.operator), log2.id)
        request = RequestFactory().get(f"/ai/admin/ai/request_logs/{log2.id}/batch-snapshot/")
        request.user = pd_user
        SessionMiddleware(lambda req: None).process_request(request)
        resp = admin_batch_snapshot_view(request, log_id=log2.id)
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertEqual(data["run_id"], run2)
        self.assertEqual(len(data["results"]), 1)

    def test_snapshot_404_for_pinned_non_batch_log(self):
        """Закрепление гарантирует только видимость снапшота чужому промпт-
        разработчику; не-батч запись журнала (чат) — 404 даже закреплённой."""
        from ai.admin.request_logs import admin_batch_snapshot_view

        AIPinnedBatchRun.objects.create(user=self.operator, log=self.plain_log)
        pd_user = get_user_model().objects.create_user(
            username="snap_pd2", password="x",
        )
        make_prompt_developer(pd_user)
        request = RequestFactory().get(f"/ai/admin/ai/request_logs/{self.plain_log.id}/batch-snapshot/")
        request.user = pd_user
        SessionMiddleware(lambda req: None).process_request(request)
        resp = admin_batch_snapshot_view(request, log_id=self.plain_log.id)
        self.assertEqual(resp.status_code, 404)

    def test_page_requires_logs_access(self):
        outsider = get_user_model().objects.create_user(username="pin_nobody", password="x")
        request = RequestFactory().get(self.pin_url_prefix)
        request.user = outsider
        SessionMiddleware(lambda req: None).process_request(request)
        resp = self.page_view(request)
        self.assertEqual(resp.status_code, 403)

    def test_journal_row_contexts_carry_pinned_flag(self):
        from ai.admin.request_logs import _batch_log_row_contexts
        self.toggle_view(self._post(f"/x/{self.log.id}/pin/", self.operator), self.log.id)
        contexts = _batch_log_row_contexts([self.log], user=self.operator)
        self.assertTrue(contexts[self.log.pk]["pinned"])
        contexts = _batch_log_row_contexts([self.log], user=self.other)
        self.assertFalse(contexts[self.log.pk]["pinned"])

    def test_pinned_foreign_log_detail_and_xlsx_open_to_prompt_dev(self):
        """Закреплённый чужой лог — общая витрина не только снапшотом:
        «Детали →» (детальная страница) и XLSX по log_id отдаются
        промпт-разработчику, а не 403 «Доступны только собственные запросы»
        (ТЗ 2026-10-19). Незакреплённый чужой лог по-прежнему 403."""
        from ai.admin.request_logs import (
            admin_request_log_detail_view,
            admin_request_log_xlsx_view,
        )

        pd_user = get_user_model().objects.create_user(username="det_pd", password="x")
        make_prompt_developer(pd_user)

        def _get(view, log_id):
            request = RequestFactory().get(f"/x/{log_id}/")
            request.user = pd_user
            SessionMiddleware(lambda req: None).process_request(request)
            return view(request, log_id=log_id)

        # Чужой незакреплённый лог — по-прежнему 403.
        self.assertEqual(_get(admin_request_log_detail_view, self.log.id).status_code, 403)

        self.toggle_view(self._post(f"/x/{self.log.id}/pin/", self.operator), self.log.id)
        self.assertEqual(_get(admin_request_log_detail_view, self.log.id).status_code, 200)
        # XLSX: у setUp-прогона нет пар — 404 «В прогоне нет результатов»;
        # важно, что это НЕ 403 «Доступны только собственные запросы».
        resp = _get(admin_request_log_xlsx_view, self.log.id)
        self.assertNotEqual(resp.status_code, 403)
        self.assertNotIn("только собственные", resp.content.decode(errors="ignore"))

    def test_pinned_foreign_log_not_resendable_by_prompt_dev(self):
        """Закрепление открывает только ЧТЕНИЕ чужого прогона: resend
        (мутация, перезапуск модели) на чужой закреплённый лог — 403,
        как и раньше (allow_pinned=False в resend/rerun)."""
        from ai.admin.request_logs import resend_request_view, rerun_arm_batch_view

        pd_user = get_user_model().objects.create_user(username="pd_mute", password="x")
        make_prompt_developer(pd_user)
        self.toggle_view(self._post(f"/x/{self.log.id}/pin/", self.operator), self.log.id)

        request = RequestFactory().post(f"/x/{self.log.id}/resend/")
        request.user = pd_user
        SessionMiddleware(lambda req: None).process_request(request)
        resp = resend_request_view(request, log_id=self.log.id)
        self.assertEqual(resp.status_code, 403)

        request = RequestFactory().post(f"/x/{self.log.id}/rerun-arm/")
        request.user = pd_user
        SessionMiddleware(lambda req: None).process_request(request)
        resp = rerun_arm_batch_view(request, log_id=self.log.id)
        self.assertEqual(resp.status_code, 403)


class AIDevTaskBoardTests(_AdminViewRequestMixin, TestCase):
    """Доска «Таски» (/ai/admin/tasks/, ai/admin/tasks.py).

    Матрица (система групп, сид 0060 + миграция 0063): доска видна с
    «Разработчика» и у «Разработчика промптов» (page.tasks выдан группой);
    взять может любой с доступом к доске; создать/закрыть — суперюзер
    (action-ключи выдаются группами); переставить строки фронта и хотелов —
    суперюзер/action.task_reorder (выдан «Разработчику»). Номер приоритета
    выдаёт СЕРВЕР (max+1 внутри вида; у фронта и хотелов нумерации
    раздельны, оба с №1); вкладки-фильтры «Не взятые» (свободный фронт) и
    «Поиск» (все таски + фильтр «Кто взял»); закрытие — архив вместо
    удаления (вкладка «Закрытые», возврат reopen'ом); в эмуляции
    «Посмотреть как…» права честные по ключам группы."""

    def setUp(self):
        from ai.admin.tasks import (
            admin_task_claim_view,
            admin_task_close_view,
            admin_task_create_view,
            admin_task_edit_view,
            admin_task_reopen_view,
            admin_task_reorder_view,
            admin_tasks_view,
            TASKS_URL,
        )
        self.page_view = admin_tasks_view
        self.create_view = admin_task_create_view
        self.reorder_view = admin_task_reorder_view
        self.claim_view = admin_task_claim_view
        self.close_view = admin_task_close_view
        self.reopen_view = admin_task_reopen_view
        self.edit_view = admin_task_edit_view
        self.tasks_url = TASKS_URL
        self.superuser = get_user_model().objects.create_superuser(
            username="task_admin", password="x", email="t@a.com",
        )
        self.dev = get_user_model().objects.create_user(username="task_dev", password="x")
        make_developer(self.dev)
        self.other_dev = get_user_model().objects.create_user(username="task_dev2", password="x")
        make_developer(self.other_dev)
        self.pd = get_user_model().objects.create_user(username="task_pd", password="x")
        make_prompt_developer(self.pd)
        self.outsider = get_user_model().objects.create_user(username="task_nobody", password="x")

    def _request(self, user, method="get", path="/x/", data=None, body=None):
        if body is not None:
            request = RequestFactory().post(
                path, data=body, content_type="application/json"
            )
        else:
            request = getattr(RequestFactory(), method)(path, data=data or {})
        request.user = user
        # each_context → guest_mode читает request.session.
        SessionMiddleware(lambda req: None).process_request(request)
        return request

    # --- Доступ --- -------------------------------------------------------

    def test_page_denies_plain_user(self):
        """Без допуска в админку (page.admin) baseline не выдаётся — 403."""
        resp = self.page_view(self._request(self.outsider, path=self.tasks_url))
        self.assertEqual(resp.status_code, 403)

    def test_page_allows_prompt_developer_via_baseline(self):
        """Доска видна «Разработчику промптов» — у группы есть page.tasks
        (сид 0060 его не содержал; миграция 0063 выдала, когда ключ вышел
        из базового набора) — брать таск тоже можно."""
        resp = self.page_view(self._request(self.pd, path=self.tasks_url))
        self.assertEqual(resp.status_code, 200)
        task = AIDevTask.objects.create(title="Т-baseline", created_by=self.superuser)
        resp = self.claim_view(self._request(self.pd, "post", f"/x/{task.id}/claim/"), task.id)
        self.assertEqual(resp.status_code, 200)
        task.refresh_from_db()
        self.assertEqual(task.taken_by, self.pd)

    def test_page_allows_developer_and_superuser(self):
        resp = self.page_view(self._request(self.dev, path=self.tasks_url))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.context_data["can_create"])
        self.assertFalse(resp.context_data["can_close"])
        self.assertTrue(resp.context_data["can_reorder"])

        resp = self.page_view(self._request(self.superuser, path=self.tasks_url))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context_data["can_create"])
        self.assertTrue(resp.context_data["can_close"])
        self.assertTrue(resp.context_data["can_reorder"])

    def test_rows_are_draggable_on_front_and_wish_tabs(self):
        """Перетаскивание: HTML5 DnD требует draggable на строках (регрессия
        2026-10 — атрибут потеряли, dragstart не стартовал вовсе). Ставится
        при праве can_reorder на вкладках реальных видов (front + wish,
        паритет хотелов 2026-10-20); на фильтрах unclaimed/search/mine нет."""
        AIDevTask.objects.create(title="Т-фронт", kind="front", priority_no=1)
        AIDevTask.objects.create(title="Т-хотелка", kind="wish", priority_no=1)

        for kind in ("front", "wish"):
            resp = self.page_view(self._request(self.superuser, path=f"{self.tasks_url}?kind={kind}"))
            self.assertTrue(resp.context_data["can_reorder"], kind)
            resp.render()
            content = resp.content.decode()
            self.assertIn('draggable="true"', content)
            self.assertIn(f'data-kind="{kind}"', content)

        # На вкладках-фильтрах перетаскивания нет (смешанные/усечённые наборы).
        for kind in ("unclaimed", "search", "mine"):
            resp = self.page_view(self._request(self.superuser, path=f"{self.tasks_url}?kind={kind}"))
            self.assertFalse(resp.context_data["can_reorder"], kind)

        # Эмуляция «Посмотреть как…» без action.task_reorder у группы:
        # can_reorder False → draggable не ставится.
        req = self._request(self.superuser, path=f"{self.tasks_url}?kind=front")
        self._enter_view_as(req, self._view_as_group("Эмуля-drag", ["page.admin", "page.tasks"]))
        resp = self.page_view(req)
        self.assertFalse(resp.context_data["can_reorder"])
        resp.render()
        self.assertNotIn('draggable="true"', resp.content.decode())

    # --- Создание (superuser; номер выдаёт сервер) -------------------------

    def test_create_requires_superuser(self):
        resp = self.create_view(self._request(self.dev, "post", path="/x/tasks/create/", data={
            "title": "Мой таск", "kind": "front",
        }))
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(AIDevTask.objects.exists())

        resp = self.create_view(self._request(self.superuser, "post", path="/x/tasks/create/", data={
            "title": "Поправить промпт №3",
            "description": "См. тему …",
            "estimate": "До 4 дней",
            "kind": "front",
        }))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(json.loads(resp.content)["ok"])
        task = AIDevTask.objects.get()
        self.assertEqual(task.title, "Поправить промпт №3")
        self.assertEqual(task.estimate, "До 4 дней")
        self.assertEqual(task.kind, "front")
        # Номер сервер выдан сам (max+1 → первый таск — №1).
        self.assertEqual(task.priority_no, 1)
        self.assertEqual(task.created_by, self.superuser)

    def test_create_front_numbers_are_max_plus_one(self):
        AIDevTask.objects.create(title="Уже есть", kind="front", priority_no=7)
        wish = AIDevTask.objects.create(title="Хотелка", kind="wish")
        self.create_view(self._request(self.superuser, "post", path="/x/tasks/create/", data={
            "title": "Новый фронт", "kind": "front",
        }))
        new_task = AIDevTask.objects.exclude(pk=wish.pk).get(title="Новый фронт")
        from django.db.models import Max
        self.assertEqual(new_task.priority_no, AIDevTask.objects.aggregate(
            no=Max("priority_no")
        )["no"])
        self.assertEqual(new_task.priority_no, 8)

    def test_create_wish_numbering_is_per_kind_max_plus_one(self):
        """Хотелки нумеруются СВОЕЙ последовательностью с №1: номер — max+1
        внутри вида, независимо от максимума фронта (паритет 2026-10-20)."""
        AIDevTask.objects.create(title="Фронт с номером", kind="front", priority_no=3)
        resp = self.create_view(self._request(self.superuser, "post", path="/x/tasks/create/", data={
            "title": "Хочу фильтры", "kind": "wish",
        }))
        self.assertEqual(resp.status_code, 200)
        wish = AIDevTask.objects.get(title="Хочу фильтры")
        self.assertEqual(wish.kind, "wish")
        self.assertEqual(wish.priority_no, 1)

        # Вторая хотелка продолжает хотелочную последовательность, а не общую.
        self.create_view(self._request(self.superuser, "post", path="/x/tasks/create/", data={
            "title": "Хочу фильтры 2", "kind": "wish",
        }))
        self.assertEqual(
            AIDevTask.objects.get(title="Хочу фильтры 2").priority_no, 2
        )
        # Новый таск фронта — по фронту (№4), хотелочные не мешают.
        self.create_view(self._request(self.superuser, "post", path="/x/tasks/create/", data={
            "title": "Новый фронт", "kind": "front",
        }))
        self.assertEqual(
            AIDevTask.objects.get(title="Новый фронт").priority_no, 4
        )

    def test_create_validates_title_and_kind(self):
        resp = self.create_view(self._request(self.superuser, "post", path="/x/tasks/create/", data={}))
        self.assertEqual(resp.status_code, 400)
        resp = self.create_view(self._request(self.superuser, "post", path="/x/tasks/create/", data={
            "title": "x", "kind": "ultra",
        }))
        self.assertEqual(resp.status_code, 400)

    # --- Взять таск (любой с доступом к доске) -----------------------------

    def test_claim_sets_taken_fields_and_visible_on_page(self):
        task = AIDevTask.objects.create(
            title="Т1", estimate="До 2 недель", created_by=self.superuser,
        )
        resp = self.claim_view(self._request(self.dev, "post", f"/x/{task.id}/claim/"), task.id)
        self.assertEqual(resp.status_code, 200)
        task.refresh_from_db()
        self.assertEqual(task.taken_by, self.dev)
        self.assertIsNotNone(task.taken_at)

        resp = self.page_view(self._request(self.superuser, path=self.tasks_url))
        row = resp.context_data["rows"][0]
        self.assertTrue(row["taken"])
        self.assertIn("task_dev", row["claimant"])

    def test_claim_wish_task_also_works(self):
        task = AIDevTask.objects.create(title="Хотелка-Т", kind="wish", created_by=self.superuser)
        resp = self.claim_view(self._request(self.dev, "post", f"/x/{task.id}/claim/"), task.id)
        self.assertEqual(resp.status_code, 200)
        task.refresh_from_db()
        self.assertEqual(task.taken_by, self.dev)

    def test_claim_unknown_task_is_404(self):
        resp = self.claim_view(self._request(self.dev, "post", "/x/99999/claim/"), 99999)
        self.assertEqual(resp.status_code, 404)

    def test_claim_second_getter_gets_409(self):
        """Атомарность: взятый таск не перезатаривается — условный UPDATE
        возвращает 0 строк и 409, taken_by остаётся у первого."""
        task = AIDevTask.objects.create(title="Т2", created_by=self.superuser)
        self.assertEqual(
            self.claim_view(self._request(self.dev, "post", f"/x/{task.id}/claim/"), task.id).status_code,
            200,
        )
        resp = self.claim_view(
            self._request(self.other_dev, "post", f"/x/{task.id}/claim/"), task.id
        )
        self.assertEqual(resp.status_code, 409)
        task.refresh_from_db()
        self.assertEqual(task.taken_by, self.dev)
        # Повторный claim тем же — тоже нельзя.
        resp = self.claim_view(self._request(self.dev, "post", f"/x/{task.id}/claim/"), task.id)
        self.assertEqual(resp.status_code, 409)

    # --- Закрытие (superuser) ----------------------------------------------

    def test_close_requires_superuser(self):
        """Закрытие уводит таск с доски, но НЕ удаляет строку: closed_at/
        closed_by ставятся, приоритетный номер сбрасывается, taken-поля
        остаются (в архиве видно «кто выполнил»); второй close — 409."""
        task = AIDevTask.objects.create(
            title="Т3", created_by=self.superuser, taken_by=self.dev,
            priority_no=5,
        )
        resp = self.close_view(self._request(self.dev, "post", f"/x/{task.id}/close/"), task.id)
        self.assertEqual(resp.status_code, 403)
        task.refresh_from_db()
        self.assertIsNone(task.closed_at)

        self.assertEqual(
            self.close_view(self._request(self.superuser, "post", f"/x/{task.id}/close/"), task.id).status_code,
            200,
        )
        task.refresh_from_db()
        # Строка жива: архив, а не удаление.
        self.assertIsNotNone(task.closed_at)
        self.assertEqual(task.closed_by, self.superuser)
        self.assertIsNone(task.priority_no)
        self.assertEqual(task.taken_by, self.dev)

        # Повторное закрытие закрытого — 409.
        resp = self.close_view(self._request(self.superuser, "post", f"/x/{task.id}/close/"), task.id)
        self.assertEqual(resp.status_code, 409)

    def test_close_unknown_task_is_404(self):
        resp = self.close_view(self._request(self.superuser, "post", "/x/99999/close/"), 99999)
        self.assertEqual(resp.status_code, 404)

    # --- Архив «Закрытые» и возврат (reopen) --------------------------------

    def test_closed_tab_details_and_visibility(self):
        """Архив «Закрытые»: тип, «кто дал», «кто выполнил», даты; закрытое
        пропадает из рабочих вкладок (front/unclaimed/search/mine)."""
        task = AIDevTask.objects.create(
            title="Готовый", kind="wish", priority_no=2, created_by=self.superuser,
        )
        self.claim_view(
            self._request(self.dev, "post", f"/x/{task.id}/claim/"), task.id
        )
        self.close_view(
            self._request(self.superuser, "post", f"/x/{task.id}/close/"), task.id
        )

        resp = self.page_view(self._request(self.dev, path=f"{self.tasks_url}?kind=closed"))
        self.assertEqual(resp.context_data["active_kind"], "closed")
        rows = resp.context_data["rows"]
        self.assertEqual([r["title"] for r in rows], ["Готовый"])
        self.assertEqual(rows[0]["kind_label"], "Хотелки")
        self.assertIn("task_admin", rows[0]["creator"])  # «кто дал»
        self.assertIn("task_dev", rows[0]["claimant"])  # «кто выполнил»
        self.assertIsNotNone(rows[0]["closed_at"])
        # Шаблон архива рисуется (колонки + двухшажный «Вернуть»: спрашивает
        # оставить исполнителя или освободить; у разработчика без действия
        # кнопок нет).
        resp = self.page_view(self._request(self.superuser, path=f"{self.tasks_url}?kind=closed"))
        resp.render()
        html = resp.content.decode()
        self.assertIn("Кто выполнил", html)
        self.assertIn("data-reopen-ask", html)
        self.assertIn("data-reopen-url", html)
        self.assertIn("С исполнителем", html)
        self.assertIn("Освободить", html)
        resp = self.page_view(self._request(self.dev, path=f"{self.tasks_url}?kind=closed"))
        resp.render()
        self.assertNotIn("data-reopen-url", resp.content.decode())

        # Рабочие вкладки закрытое больше не показывают.
        for kind_tab in ("front", "unclaimed", "search"):
            resp = self.page_view(self._request(self.dev, path=f"{self.tasks_url}?kind={kind_tab}"))
            self.assertEqual([r["title"] for r in resp.context_data["rows"]], [], kind_tab)
        resp = self.page_view(self._request(self.dev, path=self.tasks_url))
        self.assertEqual(resp.context_data["rows"], [])
        # Фильтр «Кто взял» на «Поиске» тоже только по активным.
        self.assertNotEqual(resp.context_data["active_kind"], "closed")

    def test_closed_tab_date_filter(self):
        """Разграничение по датам закрытия: обе границы включительно («по»
        захватывает весь день; границы — московские, не UTC)."""
        self.dev  # dev допущен к доске (baseline)
        first = AIDevTask.objects.create(title="Окт", created_by=self.superuser)
        second = AIDevTask.objects.create(title="Ноя", created_by=self.superuser)
        # Закрытие в 23:50 по МСК (+3): UTC-границы отсекли бы его соседним днём.
        AIDevTask.objects.filter(pk=first.pk).update(closed_at=timezone.make_aware(
            datetime(2026, 10, 5, 23, 50), timezone.get_fixed_timezone(180)
        ))
        AIDevTask.objects.filter(pk=second.pk).update(closed_at=timezone.make_aware(
            datetime(2026, 11, 10, 23, 50), timezone.get_fixed_timezone(180)
        ))

        def titles(query):
            resp = self.page_view(
                self._request(self.dev, path=f"{self.tasks_url}?kind={query}")
            )
            return [r["title"] for r in resp.context_data["rows"]]

        self.assertEqual(titles("closed&closed_from=2026-10-05&closed_to=2026-10-05"), ["Окт"])
        self.assertEqual(titles("closed&closed_from=2026-11-01"), ["Ноя"])
        self.assertEqual(titles("closed&closed_to=2026-10-31"), ["Окт"])
        self.assertEqual(titles("closed"), ["Ноя", "Окт"])  # без фильтра — все
        # Мусорные даты игнорируются (фильтр не применяется, страница жива).
        self.assertEqual(titles("closed&closed_from=бак&closed_to=zz"), ["Ноя", "Окт"])

        self.assertEqual(titles("closed"), ["Ноя", "Окт"])  # без фильтра — все
        # Мусорные даты игнорируются (фильтр не применяется, страница жива).
        self.assertEqual(titles("closed&closed_from=бак&closed_to=zz"), ["Ноя", "Окт"])

    def test_closed_tab_kind_and_executor_filters(self):
        """Архив «Закрытые»: фильтры по типу таска (?task_kind=) и по
        исполнителю (?done_by= — «кто выполнил»); мусорные значения и
        неизвестный тип молча не применяются. Селект «Кто выполнил» перечисляет
        исполнителей ЗАКРЫТЫХ тасков (не активных)."""
        front = AIDevTask.objects.create(title="Фр", kind="front", created_by=self.superuser)
        wish = AIDevTask.objects.create(title="Хо", kind="wish", created_by=self.superuser)
        self.claim_view(self._request(self.dev, "post", f"/x/{front.id}/claim/"), front.id)
        self.claim_view(self._request(self.other_dev, "post", f"/x/{wish.id}/claim/"), wish.id)
        self.close_view(self._request(self.superuser, "post", f"/x/{front.id}/close/"), front.id)
        self.close_view(self._request(self.superuser, "post", f"/x/{wish.id}/close/"), wish.id)

        def titles(query):
            resp = self.page_view(
                self._request(self.dev, path=f"{self.tasks_url}?kind={query}")
            )
            return [r["title"] for r in resp.context_data["rows"]]

        self.assertEqual(titles("closed"), ["Хо", "Фр"])  # свежие закрытия первыми
        self.assertEqual(titles("closed&task_kind=front"), ["Фр"])
        self.assertEqual(titles("closed&task_kind=wish"), ["Хо"])
        self.assertEqual(titles("closed&task_kind=garbage"), ["Хо", "Фр"])
        self.assertEqual(titles(f"closed&done_by={self.dev.id}"), ["Фр"])
        self.assertEqual(titles(f"closed&done_by={self.other_dev.id}"), ["Хо"])
        self.assertEqual(titles("closed&done_by=abc"), ["Хо", "Фр"])

        resp = self.page_view(self._request(self.dev, path=f"{self.tasks_url}?kind=closed"))
        options = resp.context_data["closed_done_options"]
        self.assertEqual(
            {opt["id"] for opt in options}, {self.dev.id, self.other_dev.id}
        )
        # Селекты фильтров рисуются (autosubmit-движок тот же, что у «Поиска»).
        resp.render()
        html = resp.content.decode()
        self.assertIn('name="task_kind"', html)
        self.assertIn('name="done_by"', html)
        self.assertIn("data-autosubmit", html)

    def test_reopen_returns_task_to_board(self):
        """«Вернуть»: закрытые поля в NULL, новый номер max+1 внутри вида,
        taken_by сохраняется (переделывать тому, кто взял); право — как у
        закрытия; повторный reopen/закрыто-открытое — 409, чужой id — 404."""
        task = AIDevTask.objects.create(
            title="Верну", kind="front", priority_no=1, created_by=self.superuser,
        )
        self.claim_view(self._request(self.dev, "post", f"/x/{task.id}/claim/"), task.id)
        self.close_view(self._request(self.superuser, "post", f"/x/{task.id}/close/"), task.id)
        AIDevTask.objects.create(title="Ещё", kind="front", priority_no=2)

        # Брать таски можно всем, а возвращать — как закрывать.
        resp = self.reopen_view(self._request(self.dev, "post", f"/x/{task.id}/reopen/"), task.id)
        self.assertEqual(resp.status_code, 403)
        resp = self.reopen_view(
            self._request(self.superuser, "post", "/x/99999/reopen/", body={}), 99999
        )
        self.assertEqual(resp.status_code, 404)

        self.assertEqual(
            self.reopen_view(
                self._request(self.superuser, "post", f"/x/{task.id}/reopen/", body={}), task.id
            ).status_code,
            200,
        )
        task.refresh_from_db()
        self.assertIsNone(task.closed_at)
        self.assertEqual(task.priority_no, 3)  # новый номер max+1, старый не восстановлен
        self.assertEqual(task.taken_by, self.dev)
        resp = self.page_view(self._request(self.dev, path=self.tasks_url))
        self.assertEqual([r["title"] for r in resp.context_data["rows"]], ["Ещё", "Верну"])

        # Открытый повторно reopen'ится нельзя.
        resp = self.reopen_view(
            self._request(self.superuser, "post", f"/x/{task.id}/reopen/", body={}), task.id
        )
        self.assertEqual(resp.status_code, 409)

        # Непарсящийся непустой JSON — 400. Пустое тело (реальный POST UI
        # «С исполнителем» шлёт fetch без body) валидно и трактуется как
        # отсутствие флага — проверяется кодовой веткой с raw.strip().
        resp = self.reopen_view(
            self._request(self.superuser, "post", "/x/99999/reopen/", body=b"not json"), 99999
        )
        self.assertEqual(resp.status_code, 400)

    def test_reopen_with_free_executor_releases_task(self):
        """«Освободить» ({"free_executor": true}): taken_by/taken_at
        сбрасываются — таск возвращается в «Не взятые», его можно взять
        заново; без флага исполнитель сохраняется (см. предыдущий тест)."""
        task = AIDevTask.objects.create(
            title="Освобожду", kind="front", priority_no=1, created_by=self.superuser,
        )
        self.claim_view(self._request(self.dev, "post", f"/x/{task.id}/claim/"), task.id)
        self.close_view(self._request(self.superuser, "post", f"/x/{task.id}/close/"), task.id)
        resp = self.reopen_view(
            self._request(self.superuser, "post", f"/x/{task.id}/reopen/", body={
                "free_executor": True,
            }),
            task.id,
        )
        self.assertEqual(resp.status_code, 200)
        task.refresh_from_db()
        self.assertIsNone(task.closed_at)
        self.assertIsNone(task.taken_by)
        self.assertIsNone(task.taken_at)
        self.assertEqual(task.priority_no, 1)  # был единственным активным
        # Таск в «Не взятые» и его можно взять заново.
        resp = self.page_view(self._request(self.dev, path=f"{self.tasks_url}?kind=unclaimed"))
        self.assertEqual([r["title"] for r in resp.context_data["rows"]], ["Освобожду"])
        self.assertEqual(
            self.claim_view(self._request(self.dev, "post", f"/x/{task.id}/claim/"), task.id).status_code,
            200,
        )

    def test_create_after_close_reuses_numbers_and_reorder_skips_closed(self):
        """Закрытый таск номер не держит: max+1 считается по активным
        (после закрытия №2 новый таск снова №2 — нумерация остаётся
        плотной); reorder считает закрытые отсутствующими — присланное
        множество только активных не даёт 409, закрытые номера не трогает."""
        first = AIDevTask.objects.create(title="А", kind="front", priority_no=1)
        second = AIDevTask.objects.create(title="Б", kind="front", priority_no=2)
        self.close_view(self._request(self.superuser, "post", f"/x/{second.id}/close/"), second.id)

        resp = self.create_view(self._request(self.superuser, "post", path="/x/tasks/create/", data={
            "title": "Новый", "kind": "front",
        }))
        self.assertEqual(json.loads(resp.content)["ok"], True)
        new_task = AIDevTask.objects.get(title="Новый")
        self.assertEqual(new_task.priority_no, 2)

        # В тело перестановки входят только АКТИВНЫЕ строки вида;
        # закрытый second не нужен (иначе был бы 409).
        resp = self.reorder_view(
            self._request(self.superuser, "post", "/x/tasks/reorder/",
                          body=self._reorder_body([new_task, first])),
        )
        self.assertEqual(resp.status_code, 200)
        first.refresh_from_db()
        new_task.refresh_from_db()
        self.assertEqual(new_task.priority_no, 1)
        self.assertEqual(first.priority_no, 2)
        second.refresh_from_db()
        self.assertIsNone(second.priority_no)

    # --- Вкладки и сортировка ----------------------------------------------

    def test_rows_sorted_by_priority_no_within_front_tab(self):
        """Меньший номер ВЫШЕ: сортировка front — по priority_no ASC."""
        AIDevTask.objects.create(title="выс", kind="front", priority_no=1)
        AIDevTask.objects.create(title="сред", kind="front", priority_no=2)
        AIDevTask.objects.create(title="низ", kind="front", priority_no=3)
        AIDevTask.objects.create(title="хотелка", kind="wish")
        resp = self.page_view(self._request(self.dev, path=self.tasks_url))
        titles = [r["title"] for r in resp.context_data["rows"]]
        self.assertEqual(titles, ["выс", "сред", "низ"])
        self.assertEqual(resp.context_data["active_kind"], "front")

        resp = self.page_view(self._request(self.dev, path=f"{self.tasks_url}?kind=wish"))
        titles = [r["title"] for r in resp.context_data["rows"]]
        self.assertEqual(titles, ["хотелка"])
        self.assertEqual(resp.context_data["active_kind"], "wish")

    def test_wish_tab_shows_numbers_starting_at_one(self):
        """Вкладки share the URL (?kind=front|wish); у хотелов СВОЯ
        последовательность с №1 — №-колонка не «—» уже после первой хотолки."""
        AIDevTask.objects.create(title="Х-первая", kind="wish", priority_no=1)
        AIDevTask.objects.create(title="Х-вторая", kind="wish", priority_no=2)
        resp = self.page_view(self._request(self.dev, path=f"{self.tasks_url}?kind=wish"))
        rows = resp.context_data["rows"]
        self.assertEqual([r["title"] for r in rows], ["Х-первая", "Х-вторая"])
        self.assertEqual([r["priority_no"] for r in rows], [1, 2])

    def test_unknown_kind_falls_back_to_front(self):
        AIDevTask.objects.create(title="фронт", kind="front", priority_no=4)
        resp = self.page_view(self._request(self.dev, path=f"{self.tasks_url}?kind=bogus"))
        self.assertEqual(resp.context_data["active_kind"], "front")

    # --- Вкладка «Не взятые» (свободный фронт) ------------------------------

    def test_unclaimed_tab_shows_free_front_tasks_only(self):
        """«Не взятые»: ТОЛЬКО незанятый фронт (взятое и хотелки не видны),
        «Взять» работает прямо оттуда (claim-вью один на все вкладки)."""
        free = AIDevTask.objects.create(title="Свободный", kind="front", priority_no=1)
        AIDevTask.objects.create(
            title="Занятый", kind="front", priority_no=2, taken_by=self.superuser,
        )
        AIDevTask.objects.create(title="Хотелка свободная", kind="wish", priority_no=1)

        resp = self.page_view(self._request(self.dev, path=f"{self.tasks_url}?kind=unclaimed"))
        self.assertEqual(resp.context_data["active_kind"], "unclaimed")
        rows = resp.context_data["rows"]
        self.assertEqual([r["title"] for r in rows], ["Свободный"])
        self.assertFalse(rows[0]["taken"])

        # Взять прямо со вкладки.
        resp = self.claim_view(self._request(self.dev, "post", f"/x/{free.id}/claim/"), free.id)
        self.assertEqual(resp.status_code, 200)
        resp = self.page_view(self._request(self.dev, path=f"{self.tasks_url}?kind=unclaimed"))
        self.assertEqual(resp.context_data["rows"], [])

    # --- Вкладка «Поиск» (все таски + фильтр «Кто взял») --------------------

    def test_search_tab_shows_all_front_then_wish_rows(self):
        """«Поиск»: ВСЕ таски, сначала фронт (по номеру), затем хотелки;
        фильтр пуст — список полный."""
        AIDevTask.objects.create(title="ф-2", kind="front", priority_no=2)
        AIDevTask.objects.create(title="ф-1", kind="front", priority_no=1)
        AIDevTask.objects.create(title="х-1", kind="wish", priority_no=1)
        resp = self.page_view(self._request(self.dev, path=f"{self.tasks_url}?kind=search"))
        self.assertEqual(resp.context_data["active_kind"], "search")
        titles = [r["title"] for r in resp.context_data["rows"]]
        self.assertEqual(titles, ["ф-1", "ф-2", "х-1"])
        self.assertEqual(resp.context_data["taken_options"], [])
        # Шаблон фильтра «Кто взял» реально рисуется на вкладке.
        resp.render()
        html = resp.content.decode()
        self.assertIn('name="taken"', html)
        self.assertIn("Кто взял", html)

    def test_search_tab_taker_filter(self):
        """Фильтр «Кто взял»: выбран человек — только его взятые таски;
        пустое значение — все (GET ?kind=search&taken=)."""
        his = AIDevTask.objects.create(title="его таск", created_by=self.superuser)
        AIDevTask.objects.create(
            title="чужой таск", created_by=self.superuser, taken_by=self.superuser,
        )
        self.claim_view(self._request(self.dev, "post", f"/x/{his.id}/claim/"), his.id)

        resp = self.page_view(self._request(
            self.dev, path=f"{self.tasks_url}?kind=search&taken={self.dev.pk}",
        ))
        titles = [r["title"] for r in resp.context_data["rows"]]
        self.assertEqual(titles, ["его таск"])
        self.assertEqual(resp.context_data["active_taken"], str(self.dev.pk))
        # Опции — все, кто хоть что-то взял (dev и superuser).
        option_ids = {o["id"] for o in resp.context_data["taken_options"]}
        self.assertEqual(option_ids, {self.dev.pk, self.superuser.pk})

    def test_search_tab_taker_filter_bad_value_shows_all(self):
        """Мусорное значение фильтра — просто все; числовой, но несуществующий
        pk — честно пустой список."""
        AIDevTask.objects.create(title="ф", created_by=self.superuser)
        base = f"{self.tasks_url}?kind=search"
        resp = self.page_view(self._request(self.dev, path=f"{base}&taken=abc"))
        self.assertEqual(len(resp.context_data["rows"]), 1)
        self.assertEqual(resp.context_data["active_taken"], "")
        resp = self.page_view(self._request(self.dev, path=f"{base}&taken=999999"))
        self.assertEqual(resp.context_data["rows"], [])

    def test_mine_tab_shows_only_own_taken_tasks(self):
        """«Мои таски» (kind=mine) — только таски, ВЗЯТЫЕ пользователем
        (созданные им, но не взятые никем, не показываются; чужие не видны);
        закрытый (=удалённый с доски) таск исчезает. Хотелка попадает тоже —
        фильтр не по kind; порядок по номеру фронта, NULL-номера в конец."""
        claimed = AIDevTask.objects.create(
            title="Мой взятый", kind="front", priority_no=2, created_by=self.superuser,
        )
        # taken_by ставится только claim'ом (никогда не вручную) — берём как UI.
        self.claim_view(
            self._request(self.dev, "post", f"/x/{claimed.id}/claim/"), claimed.id
        )
        claimed_wish = AIDevTask.objects.create(
            title="Моя взятая хоталка", kind="wish", created_by=self.superuser,
        )
        self.claim_view(
            self._request(self.dev, "post", f"/x/{claimed_wish.id}/claim/"), claimed_wish.id
        )
        # созданный мной, никем не взятый — в mine НЕ попадает
        AIDevTask.objects.create(
            title="Мой созданный", kind="front", priority_no=1, created_by=self.dev,
        )
        AIDevTask.objects.create(
            title="Чужой взятый", kind="front", priority_no=3,
            created_by=self.superuser, taken_by=self.superuser,
        )

        resp = self.page_view(self._request(self.dev, path=f"{self.tasks_url}?kind=mine"))
        self.assertEqual(resp.context_data["active_kind"], "mine")
        titles = [r["title"] for r in resp.context_data["rows"]]
        self.assertEqual(titles, ["Мой взятый", "Моя взятая хоталка"])

        # Закрытый таск уходит с доски в архив → из mine исчезает.
        self.close_view(
            self._request(self.superuser, "post", f"/x/{claimed.id}/close/"), claimed.id
        )
        resp = self.page_view(self._request(self.dev, path=f"{self.tasks_url}?kind=mine"))
        titles = [r["title"] for r in resp.context_data["rows"]]
        self.assertEqual(titles, ["Моя взятая хоталка"])

    # --- Перестановка фронта (reorder) -------------------------------------

    def _reorder_body(self, tasks, kind="front"):
        return json.dumps({
            "kind": kind,
            "ids": [t.id if isinstance(t, AIDevTask) else t for t in tasks],
        })

    def test_reorder_persists_new_order_and_renumbers_from_one(self):
        first = AIDevTask.objects.create(title="А", kind="front", priority_no=1)
        second = AIDevTask.objects.create(title="Б", kind="front", priority_no=2)
        third = AIDevTask.objects.create(title="В", kind="front", priority_no=3, created_by=self.superuser)

        resp = self.reorder_view(
            self._request(self.superuser, "post", "/x/tasks/reorder/", body=self._reorder_body([second, third, first])),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            [t.priority_no for t in AIDevTask.objects.filter(kind="front").order_by("priority_no")],
            [1, 2, 3],
        )
        self.assertEqual(AIDevTask.objects.get(pk=second.id).title, "Б")
        self.assertEqual(AIDevTask.objects.get(pk=first.id).priority_no, 3)
        # Хотелки фронт-тело не трогает: их СВОЯ нумерация сохраняется.
        wish = AIDevTask.objects.create(title="Х", kind="wish", priority_no=1)
        self.assertEqual(AIDevTask.objects.get(pk=wish.id).priority_no, 1)

    def test_reorder_wish_persists_new_order_and_renumbers_from_one(self):
        """Хотелки переезжают тем же механизмом (kind=wish): 1..N в пределах
        вида; фронту и его номера не трогаются."""
        first = AIDevTask.objects.create(title="Х-А", kind="wish", priority_no=1)
        second = AIDevTask.objects.create(title="Х-Б", kind="wish", priority_no=2)
        front = AIDevTask.objects.create(title="Ф", kind="front", priority_no=1)
        resp = self.reorder_view(
            self._request(self.superuser, "post", "/x/tasks/reorder/",
                          body=self._reorder_body([second, first], kind="wish")),
        )
        self.assertEqual(resp.status_code, 200)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(second.priority_no, 1)
        self.assertEqual(first.priority_no, 2)
        front.refresh_from_db()
        self.assertEqual(front.priority_no, 1)
        # Фронт в тело wish не попадает → 409, ничего не перепутано.
        resp = self.reorder_view(
            self._request(self.superuser, "post", "/x/tasks/reorder/",
                          body=self._reorder_body([first, second, front], kind="wish")),
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(AIDevTask.objects.get(pk=first.id).priority_no, 2)

    def test_reorder_unknown_kind_is_400(self):
        resp = self.reorder_view(
            self._request(self.superuser, "post", "/x/tasks/reorder/",
                          body=json.dumps({"kind": "ultra", "ids": []})),
        )
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(AIDevTask.objects.exists())

    def test_reorder_allows_developer_with_reorder_key(self):
        """action.task_reorder выдан «Разработчику» — порядок менять может,
        superuser-ключ не нужен."""
        first = AIDevTask.objects.create(title="А", kind="front", priority_no=1)
        second = AIDevTask.objects.create(title="Б", kind="front", priority_no=2)
        resp = self.reorder_view(
            self._request(self.dev, "post", "/x/tasks/reorder/", body=self._reorder_body([second, first])),
        )
        self.assertEqual(resp.status_code, 200)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.priority_no, 2)
        self.assertEqual(second.priority_no, 1)

    def test_reorder_denies_plain_prompt_developer(self):
        first = AIDevTask.objects.create(title="А", kind="front", priority_no=1)
        second = AIDevTask.objects.create(title="Б", kind="front", priority_no=2)
        body = self._reorder_body([second, first])

        resp = self.reorder_view(
            self._request(self.pd, "post", "/x/tasks/reorder/", body=body),
        )
        self.assertEqual(resp.status_code, 403)

    def test_reorder_honest_in_view_as(self):
        """Эмуляция честная: без action.task_reorder у группы — 403,
        с ключом — 200 (супер-обход отключён)."""
        first = AIDevTask.objects.create(title="А", kind="front", priority_no=1)
        second = AIDevTask.objects.create(title="Б", kind="front", priority_no=2)
        body = self._reorder_body([second, first])

        no_key_group = self._view_as_group("Эмуля-безрорядка", ["page.admin"])
        request = self._request(self.superuser, "post", "/x/tasks/reorder/", body=body)
        self._enter_view_as(request, no_key_group)
        self.assertEqual(self.reorder_view(request).status_code, 403)

        key_group = self._view_as_group("Эмуля-порядок", ["action.task_reorder"])
        request = self._request(self.superuser, "post", "/x/tasks/reorder/", body=body)
        self._enter_view_as(request, key_group)
        self.assertEqual(self.reorder_view(request).status_code, 200)

    def test_reorder_rejects_inexact_id_set(self):
        """Множество id должно ТОЧНО совпасть с текущим фронтом: лишний/не
        переданный/чужой id → 409, доска на сервере не трогалась."""
        first = AIDevTask.objects.create(title="А", kind="front", priority_no=1)
        second = AIDevTask.objects.create(title="Б", kind="front", priority_no=2)
        wish = AIDevTask.objects.create(title="Х", kind="wish")

        # Забыли один фронт-таск.
        resp = self.reorder_view(
            self._request(self.superuser, "post", "/x/tasks/reorder/", body=self._reorder_body([second])),
        )
        self.assertEqual(resp.status_code, 409)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.priority_no, 1)
        self.assertEqual(second.priority_no, 2)

        # Лишний id (несуществующий).
        resp = self.reorder_view(
            self._request(self.superuser, "post", "/x/tasks/reorder/",
                          body=self._reorder_body([first, second, 999999])),
        )
        self.assertEqual(resp.status_code, 409)

        # Хотелку в порядок включить нельзя.
        resp = self.reorder_view(
            self._request(self.superuser, "post", "/x/tasks/reorder/",
                          body=self._reorder_body([first, second, wish])),
        )
        self.assertEqual(resp.status_code, 409)

        # Тело не JSON / не список int'ов.
        resp = self.reorder_view(
            self._request(self.superuser, "post", "/x/tasks/reorder/", body="not json"),
        )
        self.assertEqual(resp.status_code, 400)

    def test_reorder_renumbers_gaps_from_closed_tasks(self):
        """Заодно уплотняются дырки после закрытий: 1..N непрерывно."""
        first = AIDevTask.objects.create(title="А", kind="front", priority_no=1)
        second = AIDevTask.objects.create(title="Б", kind="front", priority_no=5)
        third = AIDevTask.objects.create(title="В", kind="front", priority_no=9)
        resp = self.reorder_view(
            self._request(self.superuser, "post", "/x/tasks/reorder/", body=self._reorder_body([third, first, second])),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            [t.priority_no for t in AIDevTask.objects.filter(kind="front").order_by("priority_no")],
            [1, 2, 3],
        )
        self.assertEqual(AIDevTask.objects.get(pk=third.id).title, "В")

    # --- Отображение взявшего ----------------------------------------------

    def test_claimant_display_uses_role_fallback(self):
        """Без DL-аккаунта/справочника у взятого таска — ролевой фолбэк
        «ФИО (Разработчик промптов)» у pd-юзера, формат как у «Обновлений».

        NB: член «Разработчика» (не pd/staff/super) сейчас показывается как
        «(Пользователь)» — _user_role_label не знает про новую роль; правка
        в ai/admin/site.py добавит отдельную ветку."""
        from ai.admin.tasks import _claimant_display
        label = _claimant_display(self.pd)
        self.assertIn("(Разработчик промптов)", label)

    def test_claimant_display_shows_dl_group(self):
        """DL-разработчик: «ФИО (учебная группа)» — группа из справочника
        по education_group_mask_id его ExternalDLAccount."""
        from ai.admin.tasks import _claimant_display
        from ai.models import DLEducationName
        dev = get_user_model().objects.create_user(
            username="user_4242", password="x", first_name="Иван", last_name="Петров",
        )
        ExternalDLAccount.objects.create(
            user=dev, external_user_id="4242", external_login="dev_dl",
            education_group_mask_id="M-01",
        )
        DLEducationName.objects.create(kind=DLEducationName.KIND_GROUP, key="M-01",
                                       title="ФК-101")
        self.assertIn("Иван Петров (ФК-101)", _claimant_display(dev))

    # --- Правка таска (ТЗ 2026-10-20) ---------------------------------------

    def _edit_body(self, **kwargs):
        return json.dumps(kwargs)

    def test_edit_allowed_only_for_author(self):
        """Править таск может ТОЛЬКО тот, кто его создал (ТЗ владельца)."""
        task = AIDevTask.objects.create(
            title="Мой таск", description="Описание", estimate="До 2 недель",
            kind="front", priority_no=1, created_by=self.dev,
        )
        # Автор — правит.
        resp = self.edit_view(
            self._request(self.dev, "post", f"/x/{task.id}/edit/",
                          body=self._edit_body(title="Правленый", description="Новое",
                                               estimate="До 4 дней")),
            task.id,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(json.loads(resp.content)["ok"])
        task.refresh_from_db()
        self.assertEqual(task.title, "Правленый")
        self.assertEqual(task.description, "Новое")
        self.assertEqual(task.estimate, "До 4 дней")

        # Чужой (в т.ч. суперюзер) — 403, таск не тронут.
        for other in (self.other_dev, self.superuser, self.pd):
            resp = self.edit_view(
                self._request(other, "post", f"/x/{task.id}/edit/",
                              body=self._edit_body(title="Взломано")),
                task.id,
            )
            self.assertEqual(resp.status_code, 403, other.username)
        task.refresh_from_db()
        self.assertEqual(task.title, "Правленый")

    def test_edit_keeps_kind_and_priority_no(self):
        """Вид таска и его номер — не меняются: нумерация вида сквозная."""
        task = AIDevTask.objects.create(
            title="Хотелка", kind="wish", priority_no=3, created_by=self.dev,
        )
        resp = self.edit_view(
            self._request(self.dev, "post", f"/x/{task.id}/edit/",
                          body=self._edit_body(title="Хотелка-2", kind="front",
                                               priority_no=99)),
            task.id,
        )
        self.assertEqual(resp.status_code, 200)
        task.refresh_from_db()
        self.assertEqual(task.kind, "wish")
        self.assertEqual(task.priority_no, 3)

    def test_edit_rejects_empty_title_and_unknown_task(self):
        task = AIDevTask.objects.create(
            title="Т", kind="front", created_by=self.dev,
        )
        resp = self.edit_view(
            self._request(self.dev, "post", f"/x/{task.id}/edit/",
                          body=self._edit_body(title="   ")),
            task.id,
        )
        self.assertEqual(resp.status_code, 400)
        task.refresh_from_db()
        self.assertEqual(task.title, "Т")

        resp = self.edit_view(
            self._request(self.dev, "post", "/x/999999/edit/",
                          body=self._edit_body(title="Х")),
            999999,
        )
        self.assertEqual(resp.status_code, 404)

    def test_edit_rejects_non_json_body(self):
        task = AIDevTask.objects.create(
            title="Т", kind="front", created_by=self.dev,
        )
        resp = self.edit_view(
            self._request(self.dev, "post", f"/x/{task.id}/edit/", body="не json"),
            task.id,
        )
        self.assertEqual(resp.status_code, 400)

    def test_edit_closed_task_is_409(self):
        """Закрытый (архивный) таск не редактируется."""
        task = AIDevTask.objects.create(
            title="Т", kind="front", created_by=self.dev,
            closed_at=timezone.now(),
        )
        resp = self.edit_view(
            self._request(self.dev, "post", f"/x/{task.id}/edit/",
                          body=self._edit_body(title="Правка")),
            task.id,
        )
        self.assertEqual(resp.status_code, 409)
        task.refresh_from_db()
        self.assertEqual(task.title, "Т")

    def test_edit_button_shown_only_to_author_on_front_and_wish(self):
        """Кнопка «Изменить» — только у автора и только на рабочих видах
        (фронт/хотелки); в архиве «Закрытые» её нет."""
        mine = AIDevTask.objects.create(
            title="Мой", kind="front", priority_no=1, created_by=self.dev,
        )
        theirs = AIDevTask.objects.create(
            title="Чужой", kind="front", priority_no=2, created_by=self.other_dev,
        )
        wish = AIDevTask.objects.create(
            title="Моя хотелка", kind="wish", priority_no=1, created_by=self.dev,
        )

        resp = self.page_view(self._request(self.dev, path=f"{self.tasks_url}?kind=front"))
        rows = {r["id"]: r for r in resp.context_data["rows"]}
        self.assertTrue(rows[mine.id]["can_edit"])
        self.assertFalse(rows[theirs.id]["can_edit"])
        resp.render()
        content = resp.content.decode()
        self.assertIn("ai-task-btn-edit", content)
        self.assertIn(f'data-edit-url="{self.tasks_url}{mine.id}/edit/"', content)

        # Хотелки — тоже можно править (своя нумерация, но правило то же).
        resp = self.page_view(self._request(self.dev, path=f"{self.tasks_url}?kind=wish"))
        rows = {r["id"]: r for r in resp.context_data["rows"]}
        self.assertTrue(rows[wish.id]["can_edit"])

        # Архив «Закрытые» — кнопки правки нет.
        closed = AIDevTask.objects.create(
            title="Закрытый", kind="front", created_by=self.dev,
            closed_at=timezone.now(),
        )
        resp = self.page_view(self._request(self.dev, path=f"{self.tasks_url}?kind=closed"))
        rows = {r["id"]: r for r in resp.context_data["rows"]}
        self.assertFalse(rows[closed.id]["can_edit"])
        resp.render()
        self.assertNotIn(f'data-edit-url="{self.tasks_url}{closed.id}/edit/"',
                         resp.content.decode())

    def test_edit_works_on_pseudo_tabs_of_front(self):
        """Псевдо-вкладки («Не взятые», «Поиск», «Мои таски») показывают те же
        строки фронта/хотелок — правка там доступна автору."""
        task = AIDevTask.objects.create(
            title="Мой", kind="front", priority_no=1, created_by=self.dev,
        )
        for kind in ("unclaimed", "search", "mine"):
            resp = self.page_view(
                self._request(self.dev, path=f"{self.tasks_url}?kind={kind}")
            )
            rows = {r["id"]: r for r in resp.context_data["rows"]}
            if task.id in rows:
                self.assertTrue(rows[task.id]["can_edit"], kind)

    def test_edit_honest_in_view_as(self):
        """Эмуляция «Посмотреть как…»: авторство проверяется по РЕАЛЬНОМу
        пользователю сессии — в эмуляции правки нет ни у кого (superuser-
        обхода нет)."""
        task = AIDevTask.objects.create(
            title="Т", kind="front", created_by=self.dev,
        )
        request = self._request(self.superuser, "post", f"/x/{task.id}/edit/",
                                body=self._edit_body(title="Правка"))
        self._enter_view_as(
            request, self._view_as_group("Эмуля-правка", ["page.admin", "page.tasks"])
        )
        self.assertEqual(self.edit_view(request, task.id).status_code, 403)
        task.refresh_from_db()
        self.assertEqual(task.title, "Т")

    # --- Эмуляция «Посмотреть как…» ----------------------------------------

    def test_view_as_group_honest_page_and_mutations(self):
        """Эмуляция честная: доступ по ключам группы. Без action-ключей —
        только просмотр (can_* False, мутации 403); с ключами — можно."""
        task = AIDevTask.objects.create(title="Т5", created_by=self.superuser)

        group = self._view_as_group(
            "Эмуля-доска", ["page.admin", "page.tasks"],
        )
        request = self._request(self.superuser, path=self.tasks_url)
        self._enter_view_as(request, group)
        resp = self.page_view(request)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.context_data["can_create"])
        self.assertFalse(resp.context_data["can_reorder"])
        self.assertFalse(resp.context_data["can_close"])

        request = self._request(self.superuser, "post", path="/x/tasks/create/")
        self._enter_view_as(
            request, self._view_as_group("Эмуля-доска2", ["page.admin"]),
        )
        self.assertEqual(self.create_view(request).status_code, 403)
        request = self._request(self.superuser, "post", f"/x/{task.id}/close/")
        self._enter_view_as(request, group)
        self.assertEqual(self.close_view(request, task.id).status_code, 403)
        request = self._request(self.superuser, "post", f"/x/{task.id}/claim/")
        self._enter_view_as(request, group)
        # Взять таск может любой с доступом к доске (page.tasks у группы):
        # в честной эмуляции это 200, unlike гостя «только просмотр».
        self.assertEqual(self.claim_view(request, task.id).status_code, 200)
        task.refresh_from_db()
        self.assertEqual(task.taken_by, self.superuser)
        request = self._request(self.superuser, "post", "/x/tasks/reorder/",
                                body=self._reorder_body([task]))
        self._enter_view_as(request, group)
        self.assertEqual(self.reorder_view(request).status_code, 403)
        self.assertTrue(AIDevTask.objects.filter(pk=task.pk).exists(),
                        "таск закрыть не дали — доска не тронута")

        action_group = self._view_as_group(
            "Эмуля-действия",
            ["page.admin", "page.tasks", "action.task_create",
             "action.task_close", "action.task_reorder"],
        )
        request = self._request(self.superuser, path=self.tasks_url)
        self._enter_view_as(request, action_group)
        resp = self.page_view(request)
        self.assertTrue(resp.context_data["can_create"])
        self.assertTrue(resp.context_data["can_reorder"])
        request = self._request(self.superuser, "post", path="/x/tasks/create/",
                                data={"title": "Т6-view-as", "kind": "front"})
        self._enter_view_as(request, action_group)
        self.assertEqual(self.create_view(request).status_code, 200)
        self.assertTrue(AIDevTask.objects.filter(title="Т6-view-as").exists())


class AIUserAccessGroupsAdminTests(_AdminViewRequestMixin, TestCase):
    """Выдача «Групп доступа» со страницы пользователя и DL-аккаунта
    (ТЗ 2026-10-20): поле ai_access_groups у RestrictedUserAdmin и
    user_ai_access_groups у ExternalDLAccountAdmin.

    Гейтинг — только get_form/get_fieldsets/save_model: держатель ключа
    page.mgmt_users/page.mgmt_accounts уже имеет полный change (миксин),
    поэтому редактировать группы там может СТРОГО суперюзер либо держатель
    page.groups (can_edit_access_groups); «Посмотреть как…» — readOnly.
    Стоковые Django-«Группы» из формы убраны всегда: система прав их не
    читает. Тесты зовут get_form/get_fieldsets напрямую (как в проде —
    ровно те же хуки)."""

    def setUp(self):
        self.factory = RequestFactory()
        self.user_model = get_user_model()
        self.superuser = self.user_model.objects.create_user(
            username="ag-super", password="x", is_superuser=True, is_staff=True,
        )
        self.target = self.user_model.objects.create_user(
            username="ag-target", password="x",
        )
        self.account = ExternalDLAccount.objects.create(
            user=self.target, external_user_id="4242", external_login="target_dl",
        )

    # --- Хелперы ------------------------------------------------------------

    def _admin(self, model):
        from ai.admin import models as admin_models
        from django.contrib.admin.sites import AdminSite
        admin_cls = (
            admin_models.RestrictedUserAdmin if model is self.user_model
            else admin_models.ExternalDLAccountAdmin
        )
        return admin_cls(model, AdminSite())

    def _grant(self, user, keys):
        group = self._view_as_group(f"Г-{user.username}-{len(keys)}", keys)
        group.users.add(user)
        return group

    def _get_form(self, model, user, obj=None):
        request = self._admin_request(user, method="get")
        return self._admin(model).get_form(request, obj=obj)()

    def _fieldsets_field_names(self, model, user, obj=None):
        request = self._admin_request(user, method="get")
        return {
            field
            for _, opts in self._admin(model).get_fieldsets(request, obj=obj)
            for field in (opts.get("fields") or ())
        }

    # --- Страница пользователя ----------------------------------------------

    def test_superuser_sees_ai_groups_and_no_stock_groups(self):
        form = self._get_form(self.user_model, self.superuser, obj=self.target)
        self.assertIn("ai_access_groups", form.fields)
        self.assertNotIn("groups", form.fields)
        names = self._fieldsets_field_names(self.user_model, self.superuser, obj=self.target)
        self.assertIn("ai_access_groups", names)
        self.assertNotIn("groups", names)

    def test_page_groups_holder_can_edit(self):
        holder = self.user_model.objects.create_user(
            username="ag-holder", password="x", is_staff=True,
        )
        self._grant(holder, ["page.admin", "page.groups"])
        form = self._get_form(self.user_model, holder, obj=self.target)
        self.assertIn("ai_access_groups", form.fields)

    def test_mgmt_users_holder_readonly_display(self):
        """Держатель page.mgmt_users (без page.groups) править группы НЕ
        может: поля нет, а в филдсетах — readonly-строка состава; стоковых
        «Групп» нет и у него."""
        holder = self.user_model.objects.create_user(username="ag-mgmt", password="x")
        self._grant(holder, ["page.admin", "page.mgmt_users"])
        form = self._get_form(self.user_model, holder, obj=self.target)
        self.assertNotIn("ai_access_groups", form.fields)
        self.assertNotIn("groups", form.fields)
        names = self._fieldsets_field_names(self.user_model, holder, obj=self.target)
        self.assertIn("ai_access_groups_display", names)
        self.assertNotIn("ai_access_groups", names)

    def test_view_as_is_readonly(self):
        """Эмуляция честная: супер-обход отключён, page.groups у группы
        эмуляции НЕ даёт правки (can_edit False, как на самой странице
        «Группы доступа»). Включать эмуляцию может только суперюзер —
        иначе is_viewing_as_guest всегда False."""
        group = self._view_as_group("Эмуля-группы", ["page.admin", "page.groups"])
        request = self._admin_request(self.superuser, method="get")
        self._enter_view_as(request, group)
        form = self._admin(self.user_model).get_form(request, obj=self.target)()
        self.assertNotIn("ai_access_groups", form.fields)
        names = {
            field
            for _, opts in self._admin(self.user_model).get_fieldsets(request, obj=self.target)
            for field in (opts.get("fields") or ())
        }
        self.assertIn("ai_access_groups_display", names)

    def test_change_post_sets_groups_and_keys_apply_at_once(self):
        """POST со страницы юзера: состав AIAccessGroup меняется, права
        (user_has_key) действуют сразу — без повторного входа."""
        grant = self._view_as_group("Г-выдача", ["page.tasks"])
        from ai.admin.models import RestrictedUserAdmin
        from django.contrib.admin.sites import AdminSite
        model_admin = RestrictedUserAdmin(self.user_model, AdminSite())
        request = self._admin_request(self.superuser, method="post", path="/x/", data={
            "username": self.target.username,
            "is_active": "on",
            "date_joined_0": "2026-10-20",
            "date_joined_1": "12:30",
            "ai_access_groups": [grant.pk],
        })
        form = model_admin.get_form(request, obj=self.target)(
            data=request.POST, instance=self.target,
        )
        self.assertTrue(form.is_valid(), form.errors)
        model_admin.save_model(request, self.target, form, change=True)
        self.assertIn(self.target, grant.users.all())
        from ai.admin.permissions import user_has_key
        self.assertTrue(user_has_key(self.target, "page.tasks"))

    def test_save_model_sets_groups_but_not_for_hidden_field(self):
        """Защита от подделки POST: держателю page.mgmt_users поля нет —
        прошенный в body ai_access_groups игнорируется, состав живой."""
        holder = self.user_model.objects.create_user(username="ag-mgmt2", password="x")
        self._grant(holder, ["page.admin", "page.mgmt_users"])
        grant = self._view_as_group("Г-подделка", ["page.tasks"])
        from ai.admin.models import RestrictedUserAdmin
        from django.contrib.admin.sites import AdminSite
        model_admin = RestrictedUserAdmin(self.user_model, AdminSite())
        request = self._admin_request(holder, method="post", path="/x/", data={
            "username": self.target.username,
            "is_active": "on",
            "date_joined_0": "2026-10-20",
            "date_joined_1": "12:30",
            "ai_access_groups": [grant.pk],
        })
        form = model_admin.get_form(request, obj=self.target)(
            data=request.POST, instance=self.target,
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertNotIn("ai_access_groups", form.fields)
        model_admin.save_model(request, self.target, form, change=True)
        self.assertNotIn(self.target, grant.users.all())

    # --- Страница DL-аккаунта ------------------------------------------------

    def test_account_superuser_sees_and_sets_groups(self):
        form = self._get_form(ExternalDLAccount, self.superuser, obj=self.account)
        self.assertIn("user_ai_access_groups", form.fields)

        grant = self._view_as_group("Г-аккаунт", ["page.tasks"])
        from ai.admin.models import ExternalDLAccountAdmin
        from django.contrib.admin.sites import AdminSite
        model_admin = ExternalDLAccountAdmin(ExternalDLAccount, AdminSite())
        request = self._admin_request(self.superuser, method="post", path="/x/", data={
            "user": self.target.pk,
            "external_user_id": self.account.external_user_id,
            "external_login": self.account.external_login,
            "user_ai_access_groups": [grant.pk],
        })
        form = model_admin.get_form(request, obj=self.account)(
            data=request.POST, instance=self.account,
        )
        self.assertTrue(form.is_valid(), form.errors)
        model_admin.save_model(request, self.account, form, change=True)
        self.assertIn(self.target, grant.users.all())
        from ai.admin.permissions import user_has_key
        self.assertTrue(user_has_key(self.target, "page.tasks"))

    def test_account_mgmt_holder_sees_no_field(self):
        """page.mgmt_accounts без page.groups: блока групп нет вовсе."""
        holder = self.user_model.objects.create_user(
            username="ag-acct", password="x", is_staff=True,
        )
        self._grant(holder, ["page.admin", "page.mgmt_accounts"])
        form = self._get_form(ExternalDLAccount, holder, obj=self.account)
        self.assertNotIn("user_ai_access_groups", form.fields)

    def test_account_add_page_keeps_field_for_editor(self):
        """На add-странице (obj=None) у редактора поле есть: raw_id юзера
        заполняется при создании, save_model выдаст группы уже после
        save (в БД аккаунт без user невозможен — OneToOne NOT NULL)."""
        form = self._get_form(ExternalDLAccount, self.superuser)
        self.assertIn("user_ai_access_groups", form.fields)

    def test_account_form_initial_reflects_current_groups(self):
        grant = self._view_as_group("Г-initial", ["page.tasks"])
        grant.users.add(self.target)
        request = self._admin_request(self.superuser, method="get")
        form_cls = self._admin(ExternalDLAccount).get_form(request, obj=self.account)
        # initial ставится в __init__ по instance.user — инстанцируем с ним.
        form = form_cls(instance=self.account)
        self.assertEqual(
            list(form.fields["user_ai_access_groups"].initial), [grant]
        )


class AIAccessGroupsAdminTests(_AdminViewRequestMixin, TestCase):
    """Страница «Группы доступа» (/ai/admin/ai-groups/): только суперюзер
    (в эмуляции «Посмотреть как…» — тоже 403); CRUD групп, права чекбоксами (валидация ключей
    против БД), встроенные группы без переименования/удаления, построчный
    toggle состава. Сиды миграции 0060: ровно две встроенные группы с
    ЭТАЛОННЫМ набором ключей (set-равенство — состав правит суперюзер)."""

    def setUp(self):
        from django.contrib.sessions.middleware import SessionMiddleware

        self.factory = RequestFactory()
        self.superuser = get_user_model().objects.create_superuser(
            username="groups_admin", password="x", email="t@a.com",
        )
        self.plain = get_user_model().objects.create_user(username="groups_plain", password="x")
        self.staff = get_user_model().objects.create_user(
            username="groups_staff", password="x", is_staff=True,
        )
        self._middleware = SessionMiddleware(lambda req: None)

    def _request(self, user, method="get", path="/x/", data=None):
        request = getattr(self.factory, method)(path, data=data or {})
        request.user = user
        self._middleware.process_request(request)
        return request

    def _view(self, request):
        from ai.admin.access_groups import admin_access_groups_view
        return admin_access_groups_view(request)

    def _save(self, user, group_id, data):
        from ai.admin.access_groups import admin_access_group_save_view
        request = self._request(user, "post", f"/x/{group_id}/save/", data=data)
        return admin_access_group_save_view(request, group_id)

    def _delete(self, user, group_id):
        from ai.admin.access_groups import admin_access_group_delete_view
        request = self._request(user, "post", f"/x/{group_id}/delete/")
        return admin_access_group_delete_view(request, group_id)

    def _toggle(self, user, group_id, target_user, action="add"):
        from ai.admin.access_groups import admin_access_group_member_toggle_view
        request = self._request(user, "post", f"/x/{group_id}/members/toggle/", data={
            "user_id": target_user.pk, "action": action,
        })
        return admin_access_group_member_toggle_view(request, group_id)

    # --- Доступ --------------------------------------------------------

    def test_page_requires_superuser(self):
        self.assertEqual(self._view(self._request(self.plain)).status_code, 403)
        self.assertEqual(self._view(self._request(self.staff)).status_code, 403)

        request = self._request(self.superuser)
        self._enter_view_as(
            request, self._view_as_group("Эмуля-безгрупп", ["page.admin"]),
        )
        self.assertEqual(self._view(request).status_code, 403)
        self._leave_view_as(self.superuser)

        resp = self._view(self._request(self.superuser))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Группы доступа")

    def test_two_column_actions_card(self):
        """Две колонки прав (ТЗ 2026-10-20): слева — страницы без
        аккордеонов; справа — «Действия» АКТИВНЫХ страниц группы (в базе
        или в галочках), их вложенные чекбоксы сразу; неактивные страницы
        в правой колонке скрыты."""
        from ai.models import AIAccessGroup
        group = AIAccessGroup.objects.get(name="Разработчик промптов")
        request = self._request(self.superuser, path="/x/?group=%d" % group.id)
        resp = self._view(request)
        self.assertEqual(resp.status_code, 200)
        html = resp.render().content.decode()

        # В левой колонке нет аккордеона-кнопки «Действия (n)».
        self.assertNotIn("data-ai-perm-toggle", html)
        # Правая колонка: заголовок секции и блоки активных страниц.
        self.assertIn("data-ai-actions-for", html)
        for key in ("page.prompt_admin", "page.logs", "page.tasks"):
            self.assertIn(f'data-ai-actions-for="{key}"', html)
        # Страница Журнал/Промпт — база (checked disabled), тоже с блоками.
        self.assertIn("ai-perm-baseline", html)

    def test_save_delete_toggle_require_superuser(self):
        from ai.models import AIAccessGroup

        group = AIAccessGroup.objects.get(name="Разработчик")
        resp = self._save(self.plain, group.id, {"name": "Разработчик"})
        self.assertEqual(resp.status_code, 403)
        resp = self._delete(self.staff, group.id)
        self.assertEqual(resp.status_code, 403)
        resp = self._toggle(self.plain, group.id, self.plain)
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(AIAccessGroup.objects.filter(pk=group.pk).exists())

    # --- Сиды builtin-групп (миграция 0060) -----------------------------

    def test_builtin_groups_seeded_with_exact_key_sets(self):
        from ai.constants import BUILTIN_GROUP_DEVELOPER, BUILTIN_GROUP_PROMPT_DEV
        from ai.models import AIAccessGroup

        pd = AIAccessGroup.objects.get(name=BUILTIN_GROUP_PROMPT_DEV)
        dev = AIAccessGroup.objects.get(name=BUILTIN_GROUP_DEVELOPER)
        self.assertTrue(pd.is_builtin)
        self.assertTrue(dev.is_builtin)
        self.assertEqual(
            {p.key for p in pd.permissions.all()},
            # миграция 0063: Таски и Состояние моделей выведены из
            # baseline — выдаются сидом самой группе.
            # 0067: «Мои AI-модели» — страница про СВОИ модели, доступна
            # всей базовой аудитории админки (как «Мой промпт»).
            {"page.admin", "page.arm_solve", "page.prompt_admin",
             "page.prompt_defaults", "page.logs_own",
             "page.model_status", "page.tasks", "page.user_models"},
        )
        self.assertEqual(
            {p.key for p in dev.permissions.all()},
            # 0067: «Разработчик» дополнительно получает «Мои AI-модели»,
            # «Контроль токенов» и «Запросы по DL API».
            {"page.admin", "page.arm_solve", "page.model_status",
             "page.prompt_admin", "page.test_console",
             "page.tasks", "page.logs_own", "page.updates",
             "page.user_models", "page.tokens", "page.dl_api_requests",
             "docs.developer", "action.task_reorder"},
        )
        # Базовые права (BASELINE_ACCESS_KEYS): seeds групп могли и не менять
        # — baseline выдаётся В КОДЕ любому с page.admin, а не сидами.
        from ai.admin.permissions import user_has_key, BASELINE_ACCESS_KEYS
        self.assertFalse(user_has_key(self.plain, "page.tasks"))  # нет page.admin — нет и baseline
        make_prompt_developer(pd_user := self.plain)
        self.assertTrue(user_has_key(pd_user, "page.admin"))
        # весь «Раздел ИИ» приходит baseline'ом, не сидами:
        # (с 2026-10-19 в baseline и «Видеть все промпты»)
        self.assertIn("page.prompt_admin_all", BASELINE_ACCESS_KEYS)
        # (2026-10-20: Таски и Состояние моделей из базы выведены —
        # PD получает их сидом встроенной группы, миграция 0063.)
        self.assertEqual(set(BASELINE_ACCESS_KEYS) & {"page.tasks"}, set())
        self.assertEqual(set(BASELINE_ACCESS_KEYS) & {"page.model_status"}, set())
        self.assertTrue(user_has_key(pd_user, "page.tasks"))
        self.assertTrue(user_has_key(pd_user, "page.model_status"))
        self.assertTrue(user_has_key(pd_user, "page.logs"))
        # действия по-прежнему НЕ baseline:
        self.assertFalse(user_has_key(pd_user, "action.task_create"))
        make_developer(dev_user := get_user_model().objects.create_user(
            username="groups_dev", password="x",
        ))
        self.assertTrue(user_has_key(dev_user, "page.tasks"))
        self.assertTrue(user_has_key(dev_user, "action.task_reorder"))
        self.assertFalse(user_has_key(dev_user, "action.task_create"))
        self.assertFalse(user_has_key(dev_user, "action.task_close"))

    def test_builtin_key_seed_powers_page_and_model_status(self):
        """Ключи действительно открывают can_access_*: «Разработчику» виден
        статус моделей, промпт-девелоперу — «Промпты по умолчанию»."""
        user = get_user_model().objects.create_user(username="keys_dev", password="x")
        make_developer(user)
        from ai.admin.permissions import user_has_key
        self.assertTrue(user_has_key(user, "page.model_status"))

        pd_user = get_user_model().objects.create_user(username="keys_pd", password="x")
        make_prompt_developer(pd_user)
        from ai.admin.permissions import can_access_prompt_defaults
        self.assertTrue(can_access_prompt_defaults(type("R", (), {"user": pd_user})()))

    def test_edit_others_key_opens_foreign_prompts(self):
        """page.prompt_admin.edit_others — «Перезаписывать чужие промпты»
        (вложенное действие «Моего промпта», ТЗ 2026-10-19): не-staff'у видны
        все промпты (queryset) и чужая запись открывается на правку; без
        ключа — только свои/редактируемые."""
        from ai.admin.models import PromptAdmin
        from ai.admin.permissions import user_has_key
        from ai.admin.site import ai_admin_site
        from ai.models import AIAccessGroup, Prompt

        resp = self._save(self.superuser, 0, {
            "name": "Редакторы чужого",
            "permissions": [
                "page.admin", "page.prompt_admin", "page.prompt_admin.edit_others",
            ],
        })
        self.assertEqual(resp.status_code, 200)
        group = AIAccessGroup.objects.get(name="Редакторы чужого")
        member = get_user_model().objects.create_user(
            username="prompt_editor", password="x",
        )
        group.users.add(member)
        self.assertTrue(user_has_key(member, "page.prompt_admin.edit_others"))

        foreign = Prompt.objects.create(
            prompt_name_ru="Чужой промпт",
            prompt_text_ru="Текст чужого промпта",
            owner=self.superuser,
        )
        # Ключ открывает ЛЮБЫЕ промпты и на просмотр, и на правку.
        # (Просмотр чужого в ModelAdmin-листинге открыт и БЕЗ ключа —
        # «Видеть все промпты» в baseline 2026-10-19.)
        request = RequestFactory().get("/")
        request.user = member
        admin = ai_admin_site._registry[Prompt]
        self.assertTrue(admin.has_change_permission(request, obj=foreign))

        # Без ключа (у «Разработчика промптов» только baseline) — чужое закрыто.
        pd_user = get_user_model().objects.create_user(
            username="prompt_editor2", password="x",
        )
        make_prompt_developer(pd_user)
        self.assertFalse(user_has_key(pd_user, "page.prompt_admin.edit_others"))
        # читает чужое (baseline «Видеть все промпты»), но править не может
        self.assertTrue(admin.has_view_permission(request, obj=foreign))
        request.user = pd_user
        self.assertFalse(admin.has_change_permission(request, obj=foreign))

    # --- CRUD -----------------------------------------------------------

    def test_create_group_and_assign_permissions(self):
        resp = self._save(self.superuser, 0, {
            "name": "Операторы ARM",
            "description": "Только пакеты",
            "permissions": ["page.arm_solve", "page.model_status", "page.admin"],
        })
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(json.loads(resp.content)["ok"])

        from ai.models import AIAccessGroup
        group = AIAccessGroup.objects.get(name="Операторы ARM")
        self.assertEqual(group.description, "Только пакеты")
        self.assertFalse(group.is_builtin)
        self.assertEqual(
            {p.key for p in group.permissions.all()},
            {"page.arm_solve", "page.model_status", "page.admin"},
        )
        from ai.admin.permissions import user_has_key
        # Ключ реально открывает доступ: кастомная группа — рабочий механизм.
        member = get_user_model().objects.create_user(username="arm_op", password="x")
        group.users.add(member)
        self.assertTrue(user_has_key(member, "page.arm_solve"))
        # baseline даёт page.prompt_defaults любому с page.admin (он в выдаче),
        # поэтому «нет доступа» проверяем на не-baseline ключе:
        self.assertFalse(user_has_key(member, "page.test_console"))
        self.assertTrue(user_has_key(member, "page.prompt_defaults"))

    def test_create_requires_name(self):
        resp = self._save(self.superuser, 0, {"name": "", "permissions": []})
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(json.loads(resp.content)["ok"])

    def test_create_duplicate_name_rejected(self):
        resp = self._save(self.superuser, 0, {"name": "Разработчик", "permissions": []})
        self.assertEqual(resp.status_code, 400)

    def test_save_validates_permission_keys_against_db(self):
        resp = self._save(self.superuser, 0, {
            "name": "Кривые ключи", "permissions": ["page.admin", "nope.nope"],
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn("nope.nope", json.loads(resp.content)["message"])

        from ai.models import AIAccessGroup
        group = AIAccessGroup.objects.get(name="Кривые ключи")
        self.assertEqual(group.permissions.count(), 0)

    def test_save_group_replaces_permissions(self):
        from ai.models import AIAccessGroup
        group = AIAccessGroup.objects.get(name="Разработчик")
        # Встроенной можно менять ПРАВА (не имя) — сузили до одной страницы.
        resp = self._save(self.superuser, group.id, {
            "name": "Разработчик", "description": "",
            "permissions": ["page.admin"],
        })
        self.assertEqual(resp.status_code, 200)
        group.refresh_from_db()
        self.assertEqual([p.key for p in group.permissions.all()], ["page.admin"])

    # --- Builtin-защита --------------------------------------------------

    def test_builtin_cannot_be_renamed(self):
        from ai.constants import BUILTIN_GROUP_DEVELOPER
        from ai.models import AIAccessGroup
        group = AIAccessGroup.objects.get(name=BUILTIN_GROUP_DEVELOPER)
        resp = self._save(self.superuser, group.id, {
            "name": "Разрабы", "description": "", "permissions": ["page.admin"],
        })
        self.assertEqual(resp.status_code, 403)
        group.refresh_from_db()
        self.assertEqual(group.name, BUILTIN_GROUP_DEVELOPER)

    def test_builtin_can_be_deleted_and_recreates_empty(self):
        """Удаление встроенной разрешено (2026-10, решение владельца): права
        теряются, а при следующем добавлении участника get_or_create по имени
        пересоздаёт группу ПУСТОЙ."""
        from ai.constants import BUILTIN_GROUP_PROMPT_DEV
        from ai.models import AIAccessGroup

        group = AIAccessGroup.objects.get(name=BUILTIN_GROUP_PROMPT_DEV)
        resp = self._delete(self.superuser, group.id)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(AIAccessGroup.objects.filter(pk=group.pk).exists())

        # Пересоздание пустой: у нового юзера ключей не прибавится.
        from ai.admin.permissions import add_user_to_builtin_ai_group, user_has_key
        recreated = add_user_to_builtin_ai_group(self.plain)
        self.assertEqual(recreated.name, BUILTIN_GROUP_PROMPT_DEV)
        self.assertTrue(recreated.is_builtin)
        self.assertEqual(recreated.permissions.count(), 0)
        self.assertFalse(user_has_key(self.plain, "page.arm_solve"))

    def test_regular_group_delete(self):
        from ai.models import AIAccessGroup
        group = AIAccessGroup.objects.create(name="Одноразовая")
        resp = self._delete(self.superuser, group.id)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(AIAccessGroup.objects.filter(pk=group.pk).exists())

    def test_regular_group_rename_persists(self):
        """У созданной группы меняется имя (2026-10: раньше имя обновлялось
        только при создании — ренейм был мёртвым кодом); права сохраняются."""
        from ai.models import AIAccessGroup

        group = AIAccessGroup.objects.create(name="Старое имя")
        from ai.models import AIAdminGroupPermission
        group.permissions.add(
            *AIAdminGroupPermission.objects.filter(key="page.arm_solve")
        )
        resp = self._save(self.superuser, group.id, {
            "name": "Новое имя", "description": "переименована",
            "permissions": ["page.arm_solve"],
        })
        self.assertEqual(resp.status_code, 200)
        group.refresh_from_db()
        self.assertEqual(group.name, "Новое имя")
        self.assertEqual(group.description, "переименована")
        self.assertEqual([p.key for p in group.permissions.all()], ["page.arm_solve"])

    def test_regular_group_rename_to_duplicate_is_400(self):
        from ai.models import AIAccessGroup
        AIAccessGroup.objects.create(name="А", description="")
        target = AIAccessGroup.objects.create(name="Б", description="")
        resp = self._save(self.superuser, target.id, {"name": "А"})
        self.assertEqual(resp.status_code, 400)
        target.refresh_from_db()
        self.assertEqual(target.name, "Б")

    # --- Динамические секции (страницы/доки из нава) ---------------------

    def test_pages_section_follows_nav_registry(self):
        """«Страницы» — динамически из _ai_tool_defs: строка ключа, потерянная
        в БД, восстанавливается при рендере; page.admin (без URL) остаётся;
        устаревшая БД-строка не показывается. Вложенные действия страниц —
        аккордеоном."""
        from ai.models import AIAdminGroupPermission

        AIAdminGroupPermission.objects.filter(key="page.arm_solve").delete()
        AIAdminGroupPermission.objects.create(
            key="page.ghost", label="Призрак",
            section=AIAdminGroupPermission.SECTION_PAGE,
        )
        resp = self._view(self._request(self.superuser))
        resp.render()
        html = resp.content.decode()
        # Потерянная страница восстановлена из нава и видна (подпись нава).
        self.assertTrue(
            AIAdminGroupPermission.objects.filter(key="page.arm_solve").exists()
        )
        self.assertIn("Пакетное решение", html)
        # docs — тоже из нава (свежая подпись нава перетирает миграционную).
        self.assertIn("Документация для разработчика", html)
        # page.admin — гейт входа, страницы не имеет: остаётся.
        self.assertIn("page.admin", html)
        # Вложенные действия «Тасок» и «Моего промпта» — аккордеоном
        # (value-чекбоксы есть, страницы без действий кнопку не рисуют).
        self.assertIn('value="action.task_create"', html)
        self.assertIn('value="page.prompt_admin.edit_others"', html)
        self.assertIn('value="page.logs_all"', html)
        self.assertIn("Перезаписывать чужие промпты", html)
        # Ключи-страницы без ключа в коде (гейты без URL) в списке нет.
        self.assertNotIn("page.ghost", html)
        # Новые ключи-страницы и действия доступны в UI групп.
        self.assertIn('value="page.pinned_runs"', html)
        self.assertIn('value="page.test_data"', html)
        self.assertIn('value="page.groups"', html)
        self.assertIn('value="docs.prompt_developer"', html)
        self.assertIn('value="page.prompt_admin_all"', html)
        self.assertIn('value="action.test_console_run"', html)
        self.assertIn('value="action.updates_publish"', html)
        self.assertIn('value="page.mgmt_users"', html)
        self.assertIn('value="page.mgmt_shared_prompt"', html)
        self.assertIn("Видеть все промпты", html)
        self.assertIn("Запускать прогоны", html)

    def test_member_search_endpoint(self):
        from ai.admin.access_groups import admin_access_group_member_search_view
        from ai.models import AIAccessGroup

        group = AIAccessGroup.objects.get(name="Разработчик")
        request = self._request(
            self.superuser, "get", f"/x/{group.id}/members/search/", data={"q": "plain"}
        )
        resp = admin_access_group_member_search_view(request, group.id)
        self.assertEqual(resp.status_code, 200)
        names = [u["name"] for u in json.loads(resp.content)["users"]]
        self.assertIn("groups_plain", names)

        # Пустой q → пустой список; чужой доступ → 403; не группа → 404.
        request = self._request(
            self.superuser, "get", f"/x/{group.id}/members/search/", data={"q": ""}
        )
        self.assertEqual(admin_access_group_member_search_view(request, group.id).status_code, 200)
        request = self._request(
            self.plain, "get", f"/x/{group.id}/members/search/", data={"q": "x"}
        )
        self.assertEqual(admin_access_group_member_search_view(request, group.id).status_code, 403)
        request = self._request(
            self.superuser, "get", "/x/999999/members/search/", data={"q": "x"}
        )
        self.assertEqual(admin_access_group_member_search_view(request, 999999).status_code, 404)

    def test_delete_unknown_group_is_404(self):
        resp = self._delete(self.superuser, 999999)
        self.assertEqual(resp.status_code, 404)

    # --- Состав (member toggle) ------------------------------------------

    def test_member_toggle_add_then_remove(self):
        from ai.constants import BUILTIN_GROUP_DEVELOPER
        from ai.models import AIAccessGroup
        group = AIAccessGroup.objects.get(name=BUILTIN_GROUP_DEVELOPER)

        resp = self._toggle(self.superuser, group.id, self.plain, action="add")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(group.users.filter(pk=self.plain.pk).exists())
        from ai.admin.permissions import user_has_key
        # И ключ пришел сразу: выдача действует без перелогина.
        self.assertTrue(user_has_key(self.plain, "page.tasks"))

        resp = self._toggle(self.superuser, group.id, self.plain, action="remove")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(group.users.filter(pk=self.plain.pk).exists())
        self.assertFalse(user_has_key(self.plain, "page.tasks"))

    def test_member_toggle_unknown_user_or_group_is_404(self):
        from ai.admin.access_groups import admin_access_group_member_toggle_view
        from ai.models import AIAccessGroup

        group = AIAccessGroup.objects.get(name="Разработчик промптов")
        # Несуществующая группа.
        resp = self._toggle(self.superuser, 999999, self.plain)
        self.assertEqual(resp.status_code, 404)

        # Несуществующий юзер (в POST идёт сырой user_id).
        request = self._request(self.superuser, "post", f"/x/{group.id}/members/toggle/", data={
            "user_id": 999999, "action": "add",
        })
        resp = admin_access_group_member_toggle_view(request, group.id)
        self.assertEqual(resp.status_code, 404)


class ArmCourseTreeDbCacheTests(TestCase):
    """Дерево курса выживает отказ DL: бессрочная Redis-копия + БД
    (AICourseTreeCache). 403 DL у пользователей без активного курса больше не
    ломает /arm/solve/ — после одной успешной загрузки дерево доступно всем."""

    def setUp(self):
        from ai.admin.arm import admin_arm_solve_load_tree_view
        self.view = admin_arm_solve_load_tree_view
        self.admin = get_user_model().objects.create_superuser(
            username="tree_admin", password="***", email="t@t.com",
        )
        # Redis переживает тесты — чистим ключи дерева курса (изоляция).
        from django.core.cache import cache
        from ai.constants import AI_CACHE_KEY_PREFIX
        cache.delete(f"{AI_CACHE_KEY_PREFIX}:dl_tree:1450")
        cache.delete(f"{AI_CACHE_KEY_PREFIX}:dl_tree_stale:1450")

    def _post(self):
        from django.contrib.sessions.middleware import SessionMiddleware
        request = RequestFactory().post("/ai/admin/arm/solve/load-tree/", {"course_id": "1450"})
        request.user = self.admin
        SessionMiddleware(lambda req: None).process_request(request)
        # DLSID нужен вью только на промахе кэша (дальше DL мокается).
        request.session["external_session_id"] = "{TEST-DLSID-GUID}"
        return request

    def test_success_persists_tree_to_db(self):
        from django.core.cache import cache
        from ai.constants import AI_CACHE_KEY_PREFIX
        from ai.models import AICourseTreeCache
        tree = [{"nodeId": 1, "name": "Папка", "isFolder": True,
                 "children": [{"nodeId": 2, "name": "Задача", "isFolder": False}]}]
        with patch("ai.dl_api_client.fetch_course_nodes",
                   return_value={"tasksRootId": 100}), patch(
            "ai.dl_api_client.fetch_node_tree",
            return_value={"tree": tree},
        ):
            resp = self.view(self._post())
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertTrue(data["ok"])
        self.assertEqual(data["task_count"], 1)
        row = AICourseTreeCache.objects.get(course_id=1450)
        self.assertEqual(row.tree, data["tree"])
        # Redis-копии тоже на месте
        self.assertIsNotNone(cache.get(f"{AI_CACHE_KEY_PREFIX}:dl_tree_stale:1450"))

    def test_dl_failure_falls_back_to_db_tree(self):
        from django.core.cache import cache
        from ai.constants import AI_CACHE_KEY_PREFIX
        from ai.dl_api_client import DLForbiddenError
        from ai.models import AICourseTreeCache
        # Redis пуст (очистка), БД хранит последнее удачное дерево.
        AICourseTreeCache.objects.create(
            course_id=1450,
            tree=[{"nodeId": 9, "name": "Задача", "isFolder": False}],
            task_count=1,
        )
        with patch("ai.dl_api_client.fetch_course_nodes",
                   side_effect=DLForbiddenError()), patch(
            "ai.dl_api_client.fetch_node_tree", side_effect=DLForbiddenError(),
        ):
            resp = self.view(self._post())
        data = json.loads(resp.content)
        self.assertTrue(data["ok"])
        self.assertTrue(data["stale"])
        self.assertEqual(data["tree"], [{"nodeId": 9, "name": "Задача", "isFolder": False}])
        # Redis-копия реанимирована
        self.assertIsNotNone(cache.get(f"{AI_CACHE_KEY_PREFIX}:dl_tree_stale:1450"))

    def test_dl_failure_without_any_cache_gives_hint(self):
        from ai.dl_api_client import DLForbiddenError
        with patch("ai.dl_api_client.fetch_course_nodes",
                   side_effect=DLForbiddenError()), patch(
            "ai.dl_api_client.fetch_node_tree", side_effect=DLForbiddenError(),
        ):
            resp = self.view(self._post())
        self.assertEqual(resp.status_code, 400)
        data = json.loads(resp.content)
        self.assertFalse(data["ok"])
        self.assertIn("dl.gsu.by", data["message"])


class RequestLogDetailJsonTests(TestCase):
    """Ленивая развёртка подробностей записи журнала на «Настройке ИИ»: JSON
    GET /ai/admin/ai/airequestlog/<id>/detail-json/ с правами журнала."""

    def setUp(self):
        from ai.admin.request_logs import admin_request_log_detail_json_view
        from django.contrib.sessions.middleware import SessionMiddleware
        self.view = admin_request_log_detail_json_view
        self._middleware = SessionMiddleware(lambda req: None)
        self.admin = get_user_model().objects.create_superuser(
            username="log_admin", password="***", email="l@t.com",
        )
        self.pd = get_user_model().objects.create_user(username="log_pd", password="***")
        make_prompt_developer(self.pd)
        self.log = AIRequestLog.objects.create(
            user=self.admin, source="websocket", mode="solve",
            message="текст запроса", response_text="ответ модели",
            error_message="",
            status=AIRequestLog.STATUS_SUCCESS, sent_at=timezone.now(),
            model_names=["ModelOne"], task_node_id=2606749, task_name="Байт + слово",
        )

    def _get(self, user, log_id=None):
        request = RequestFactory().get(f"/x/{log_id or self.log.id}/detail-json/")
        request.user = user
        self._middleware.process_request(request)
        return self.view(request, log_id or self.log.id)

    def test_returns_detail_fields(self):
        resp = self._get(self.admin)
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertTrue(data["ok"])
        self.assertEqual(data["log"]["message"], "текст запроса")
        self.assertEqual(data["log"]["response_text"], "ответ модели")
        self.assertEqual(data["log"]["task_node_id"], 2606749)
        self.assertIn("detail_url", data["log"])

    def test_batch_log_finish_meta(self):
        """Batch-запись: JSON несёт время окончания и общее время прогона."""
        from datetime import timedelta

        from ai.constants import MOSCOW_TZ
        from ai.models import AIModelTestRun
        run_id = "e" * 32
        AIModelTestRun.objects.create(
            run_id=run_id, run_type=AIModelTestRun.RUN_TYPE_BATCH,
            status=AIModelTestRun.STATUS_COMPLETED, course_id=1450,
            started_at=timezone.now() - timedelta(seconds=90),
            finished_at=timezone.now(),
        )
        batch_log = AIRequestLog.objects.create(
            user=self.admin, source="arm", mode="batch_solve",
            message=f"Batch solve run {run_id}",
            status=AIRequestLog.STATUS_SUCCESS, sent_at=timezone.now(),
        )
        data = json.loads(self._get(self.admin, batch_log.id).content)
        self.assertTrue(data["ok"])
        self.assertTrue(data["log"]["is_batch"])
        self.assertRegex(data["log"]["run_finished_at"], r"^\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}:\d{2}$")
        self.assertEqual(data["log"]["run_duration_display"], "1 мин 30 с")

    def test_batch_log_running_no_finish_meta(self):
        """Идущий (finished_at пуст) прогон → пустые строки (JS покажет «—»)."""
        from ai.models import AIModelTestRun
        run_id = "f" * 32
        AIModelTestRun.objects.create(
            run_id=run_id, run_type=AIModelTestRun.RUN_TYPE_BATCH,
            status=AIModelTestRun.STATUS_RUNNING, course_id=1450,
        )
        batch_log = AIRequestLog.objects.create(
            user=self.admin, source="arm", mode="batch_solve",
            message=f"Batch solve run {run_id}",
            status=AIRequestLog.STATUS_SUCCESS, sent_at=timezone.now(),
        )
        data = json.loads(self._get(self.admin, batch_log.id).content)
        self.assertEqual(data["log"]["run_finished_at"], "")
        self.assertEqual(data["log"]["run_duration_display"], "")

    def test_prompt_developer_foreign_log_forbidden(self):
        resp = self._get(self.pd)
        self.assertEqual(resp.status_code, 403)

    def test_missing_log_404(self):
        resp = self._get(self.admin, log_id=999999)
        self.assertEqual(resp.status_code, 404)


class AssetViewAccessTests(TestCase):
    """asset_view (/ai/assets/): prod требует внешний id (user_info / userId-cookie);
    dev-bypass (DEBUG + AI_DEV_AUTH_BYPASS) — достаточно валидной Django-сессии.

    Регрессия: /ai/assets/ — skip-путь ExternalAuthMiddleware, request.user_info
    на нём не заполняется, поэтому локально (вне dl.gsu.by, кук userId/DLID нет)
    каждый статический файл отдавал 403 — «страница работает, но нет стилей».
    """

    def setUp(self):
        self.factory = RequestFactory()

    def _get(self, cookies=None, user=None):
        from ai.views import asset_view

        request = self.factory.get("/ai/assets/admin/css/ai.css")
        if cookies:
            request.COOKIES = cookies
        if user is not None:
            request.user = user
        return asset_view(request, "admin/css/ai.css")

    def test_anonymous_is_403(self):
        self.assertEqual(self._get().status_code, 403)

    def test_authenticated_without_external_id_is_403_in_prod_mode(self):
        user = get_user_model().objects.create_user(username="plain", password="x")
        self.assertEqual(self._get(user=user).status_code, 403)

    def test_authenticated_with_user_id_cookie_is_200(self):
        user = get_user_model().objects.create_user(username="plain", password="x")
        response = self._get(cookies={"userId": "plain"}, user=user)
        self.assertEqual(response.status_code, 200)

    @override_settings(DEBUG=True)
    def test_authenticated_dev_bypass_is_200_without_external_id(self):
        user = get_user_model().objects.create_user(username="dev", password="x")
        with patch.dict(os.environ, {"AI_DEV_AUTH_BYPASS": "1"}):
            response = self._get(user=user)
        self.assertEqual(response.status_code, 200)


class AssetViewETagTests(TestCase):
    """Условные запросы asset_view: ETag = md5 КОНТЕНТА (mtime при деплое мог
    не сдвинуться → 304 отдавал старый файл, «стили падали»). If-None-Match
    (строгий/слабый/списком) → 304 с ETag + Cache-Control; непрошедший ETag →
    200 с полным телом; If-Modified-Since — fallback, если ETag не прислали.
    """

    def setUp(self):
        self.factory = RequestFactory()
        self.user = get_user_model().objects.create_user(username="etag_user", password="x")

    def _get(self, headers=None):
        from ai.views import asset_view

        request = self.factory.get("/ai/assets/admin/css/ai.css")
        request.COOKIES = {"userId": self.user.username}
        request.user = self.user
        for key, value in (headers or {}).items():
            request.META[key] = value
        response = asset_view(request, "admin/css/ai.css")
        return response

    def test_200_carries_etag_and_no_cache(self):
        """Кавыченный md5 (content + соль старта процесса): соль не даёт
        «отравленному» кэшу прошлого деплоя (пустое тело при живом old-ETag)
        навсегда отвечать 304 — новый процесс гарантированно перекачивает
        свежие байты; сам честный cached-байт продолжает ловить 304."""
        import hashlib
        from ai.views import _ASSET_BOOT_SALT
        response = self._get()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "no-cache")
        self.assertIn("ETag", response)
        etag = response["ETag"]
        self.assertTrue(etag.startswith('"') and etag.endswith('"'))
        from django.contrib.staticfiles import finders
        path = finders.find("admin/css/ai.css")
        with open(path, "rb") as fh:
            self.assertEqual(
                etag.strip('"'),
                hashlib.md5(_ASSET_BOOT_SALT + fh.read()).hexdigest(),
            )
        # Соль реально участвует: голый md5 контента больше НЕ валидатор.
        self.assertNotEqual(
            etag.strip('"'),
            hashlib.md5(open(path, "rb").read()).hexdigest(),
        )

    def test_matching_etag_gives_304_with_etag_and_cache_control(self):
        first = self._get()
        self.assertEqual(first.status_code, 200)
        not_modified = self._get({"HTTP_IF_NONE_MATCH": first["ETag"]})
        self.assertEqual(not_modified.status_code, 304)
        self.assertEqual(not_modified["ETag"], first["ETag"])
        self.assertEqual(not_modified["Cache-Control"], "no-cache")

    def test_weak_and_listed_etags_match(self):
        first = self._get()
        bare = first["ETag"].strip('"')
        for header in (f'W/{first["ETag"]}', f'"opaque-etag", {first["ETag"]}'):
            response = self._get({"HTTP_IF_NONE_MATCH": header})
            self.assertEqual(response.status_code, 304, header)
            self.assertEqual(response["ETag"].strip('"'), bare)

    def test_mismatched_etag_serves_full_body(self):
        first = self._get()
        response = self._get({"HTTP_IF_NONE_MATCH": '"no-such-etag"'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), b"".join(first.streaming_content))

    def test_if_modified_since_fallback_when_no_etag(self):
        # Кэш без ETag: дата «в будущем» → не модифицирован → 304.
        response = self._get({"HTTP_IF_MODIFIED_SINCE": "Mon, 01 Jan 2399 00:00:00 GMT"})
        self.assertEqual(response.status_code, 304)
        self.assertEqual(response["Cache-Control"], "no-cache")
        self.assertIn("ETag", response)
        # Дата в прошлом → модифицирован → 200 с телом.
        response = self._get({"HTTP_IF_MODIFIED_SINCE": "Thu, 01 Jan 1990 00:00:00 GMT"})
        self.assertEqual(response.status_code, 200)


class OnboardingServiceTests(TestCase):
    """ai/services/onboarding.py: контракт флага «wizard просмотрено»."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(username="wz-user", password="x")

    def test_scope_validation(self):
        from ai.services.onboarding import scope_name_valid
        self.assertTrue(scope_name_valid("chat"))
        self.assertTrue(scope_name_valid("admin_arm_solve"))
        # Тур главного экрана админки разделён по правам: свои scope'ы на
        # каждый уровень, объединённого «admin» больше нет.
        self.assertTrue(scope_name_valid("admin_pd"))
        self.assertTrue(scope_name_valid("admin_staff"))
        self.assertTrue(scope_name_valid("admin_super"))
        # Главные страницы админки — свой тур у каждой (контент страницы).
        for page_scope in (
            "admin_my_prompt", "admin_prompt_defaults", "admin_logs",
            "admin_pinned_runs", "admin_aiappsettings", "admin_updates",
            "admin_test_console", "admin_tasksolution",
            "admin_test_data",
        ):
            self.assertTrue(scope_name_valid(page_scope))
        self.assertFalse(scope_name_valid("admin"))
        self.assertFalse(scope_name_valid("nope"))
        self.assertFalse(scope_name_valid(None))

    def test_no_row_means_show(self):
        from ai.services.onboarding import should_show_wizard
        self.assertTrue(should_show_wizard(self.user, "chat"))

    def test_record_then_nag_disappears(self):
        from ai.services.onboarding import record_wizard_seen, should_show_wizard, WIZARD_VERSION
        record_wizard_seen(self.user, "chat")
        self.assertFalse(should_show_wizard(self.user, "chat"))
        row = AIWizardSeen.objects.get(user=self.user, scope="chat")
        self.assertEqual(row.version, WIZARD_VERSION)

    def test_record_is_idempotent_single_row(self):
        from ai.services.onboarding import record_wizard_seen
        record_wizard_seen(self.user, "chat")
        record_wizard_seen(self.user, "chat")
        self.assertEqual(AIWizardSeen.objects.filter(user=self.user).count(), 1)

    def test_deleting_mark_restarts_the_tour(self):
        """Удаление строки в AIWizardSeenAdmin — способ перезапустить тур
        конкретному пользователю на конкретной странице."""
        from ai.services.onboarding import record_wizard_seen, should_show_wizard
        record_wizard_seen(self.user, "chat")
        self.assertFalse(should_show_wizard(self.user, "chat"))
        AIWizardSeen.objects.filter(user=self.user, scope="chat").delete()
        self.assertTrue(should_show_wizard(self.user, "chat"))

    def test_unknown_scope_is_not_recorded(self):
        from ai.services.onboarding import record_wizard_seen
        record_wizard_seen(self.user, "nope")
        record_wizard_seen(self.user, None)
        self.assertEqual(AIWizardSeen.objects.count(), 0)

    def test_version_bump_resurrects_the_tour(self):
        from ai.services import onboarding as onboarding_service
        record_wizard_seen = onboarding_service.record_wizard_seen
        should_show_wizard = onboarding_service.should_show_wizard
        record_wizard_seen(self.user, "chat")  # пишет текущий WIZARD_VERSION
        self.assertFalse(should_show_wizard(self.user, "chat"))
        with patch(
            "ai.services.onboarding.WIZARD_VERSION",
            onboarding_service.WIZARD_VERSION + 1,
        ):
            # Содержимое тура обновилось → все видят его заново один раз.
            self.assertTrue(should_show_wizard(self.user, "chat"))
            record_wizard_seen(self.user, "chat")
            self.assertFalse(should_show_wizard(self.user, "chat"))
        # Записанная версия выше актуальной (откат константы) — не показываем.
        self.assertFalse(should_show_wizard(self.user, "chat"))

    def test_anonymous_user_is_never_shown_and_never_recorded(self):
        from ai.services.onboarding import record_wizard_seen, should_show_wizard
        anonymous = SimpleNamespace(is_authenticated=False)
        self.assertFalse(should_show_wizard(anonymous, "chat"))
        record_wizard_seen(anonymous, "chat")
        self.assertEqual(AIWizardSeen.objects.count(), 0)

    def test_user_basics_seen_other_user_scope(self):
        """Шаги-«общие» закрываются отметкой ЛЮБОЙ другой юзер-страницы."""
        from ai.services.onboarding import record_wizard_seen, user_basics_seen
        self.assertFalse(user_basics_seen(self.user, "solve"))
        record_wizard_seen(self.user, "chat")
        self.assertTrue(user_basics_seen(self.user, "solve"))
        self.assertTrue(user_basics_seen(self.user, "find_error"))

    def test_user_basics_seen_same_scope_does_not_count(self):
        """Свой scope «общих» не закрывает — тур текущей страницы ещё не пройден."""
        from ai.services.onboarding import record_wizard_seen, user_basics_seen
        record_wizard_seen(self.user, "solve")
        self.assertFalse(user_basics_seen(self.user, "solve"))

    def test_user_basics_seen_ignores_admin_scopes_and_excludes_wrong_scope(self):
        """Админские отметки и не-юзерский exclude — не «общие»."""
        from ai.services.onboarding import record_wizard_seen, user_basics_seen
        record_wizard_seen(self.user, "admin_pd")
        self.assertFalse(user_basics_seen(self.user, "solve"))
        self.assertFalse(user_basics_seen(self.user, "admin_pd"))
        self.assertFalse(user_basics_seen(None, "solve"))

    def test_user_basics_seen_requires_current_version(self):
        """Закрыть «общие» может отметка ТОЛЬКО текущей версии: после бампа
        туры обновились, и их общая часть ещё не показывалась (строка старой
        версии не выпадала бы общие шаги молча)."""
        from ai.services import onboarding as onboarding_service
        from ai.services.onboarding import user_basics_seen

        row = AIWizardSeen.objects.create(user=self.user, scope="chat", version=1)
        self.assertGreater(onboarding_service.WIZARD_VERSION, 1)
        self.assertFalse(user_basics_seen(self.user, "solve"))
        row.version = onboarding_service.WIZARD_VERSION
        row.save(update_fields=["version"])
        self.assertTrue(user_basics_seen(self.user, "solve"))

class WizardSeenUserEndpointTests(TestCase):
    """POST /ai/api/wizard-seen/ (пользовательские страницы, ai-wizard.js)."""

    def setUp(self):
        self.factory = RequestFactory()
        self.user = get_user_model().objects.create_user(username="wz-http-user", password="x")

    def _post(self, body, user=None):
        from ai.views import wizard_seen_view

        request = self.factory.post(
            "/ai/api/wizard-seen/", data=body, content_type="application/json",
        )
        request.user = user if user is not None else self.user
        request.session = {}
        request.user_info = {"userId": "wz-http-user"}
        request.COOKIES = {"userId": "wz-http-user"}
        return wizard_seen_view(request)

    def test_requires_auth(self):
        stranger = SimpleNamespace(is_authenticated=False, is_active=True)
        response = self._post(json.dumps({"scope": "chat"}), user=stranger)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(AIWizardSeen.objects.count(), 0)

    def test_get_is_405(self):
        from ai.views import wizard_seen_view

        request = self.factory.get("/ai/api/wizard-seen/")
        request.user = self.user
        request.session = {}
        request.user_info = {"userId": "wz-http-user"}
        request.COOKIES = {"userId": "wz-http-user"}
        self.assertEqual(wizard_seen_view(request).status_code, 405)

    def test_unknown_scope_is_400(self):
        # Валидатор scope общий для обоих эндпоинтов (ai/services/onboarding.py);
        # «admin» с v2 им больше не валидный (разделён на ролевые), поэтому тест
        # на явно неизвестное имя.
        response = self._post(json.dumps({"scope": "nope"}))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(AIWizardSeen.objects.count(), 0)

    def test_broken_json_is_400(self):
        response = self._post(b"{not json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(AIWizardSeen.objects.count(), 0)

    def test_valid_post_marks_seen_once(self):
        response = self._post(json.dumps({"scope": "chat"}))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content), {"ok": True})
        self.assertEqual(
            AIWizardSeen.objects.get(user=self.user, scope="chat").scope, "chat",
        )
        # Повторный POST не плодит строк.
        self._post(json.dumps({"scope": "chat"}))
        self.assertEqual(
            AIWizardSeen.objects.filter(user=self.user, scope="chat").count(), 1,
        )


class WizardSeenAdminEndpointTests(_AdminViewRequestMixin, TestCase):
    """POST /ai/admin/wizard/seen/ (админка; admin_view-гейты)."""

    def setUp(self):
        self.factory = RequestFactory()
        self.user_model = get_user_model()

    def _pd_user(self):
        user = self.user_model.objects.create_user(username="wz-pd", password="x")
        make_prompt_developer(user)
        return user

    def _post(self, user, body, method="post", path="/ai/admin/wizard/seen/"):
        from unittest.mock import patch as _patch
        from ai.admin.onboarding import admin_wizard_seen_view

        request = self._admin_request(
            user, method=method, path=path, data=body,
            content_type="application/json" if body else None,
        )
        with _patch("ai.admin.site.get_external_user_id_from_request", return_value="12345"):
            return admin_wizard_seen_view(request)

    def test_anonymous_is_denied(self):
        # «admin» больше не валидный scope (разделён по правам), поэтому — «chat».
        response = self._post(AnonymousUser(), '{"scope": "chat"}')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(AIWizardSeen.objects.count(), 0)

    def test_get_is_405(self):
        response = self._post(self._pd_user(), "", method="get")
        self.assertEqual(response.status_code, 405)

    def test_unknown_scope_is_400(self):
        response = self._post(self._pd_user(), '{"scope": "nope"}')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(AIWizardSeen.objects.count(), 0)

    def test_valid_post_marks_seen(self):
        pd_user = self._pd_user()
        response = self._post(pd_user, '{"scope": "admin_arm_solve"}')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content), {"ok": True})
        self.assertTrue(
            AIWizardSeen.objects.filter(user=pd_user, scope="admin_arm_solve").exists(),
        )

    def test_plain_user_without_roles_is_403(self):
        stranger = self.user_model.objects.create_user(username="wz-nobody", password="x")
        response = self._post(stranger, '{"scope": "admin_pd"}')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(AIWizardSeen.objects.count(), 0)

    def test_view_as_mode_post_is_ok_but_not_recorded(self):
        from ai.admin.onboarding import admin_wizard_seen_view

        superuser = self.user_model.objects.create_user(
            username="wz-super", password="x", is_superuser=True,
        )
        request = self._admin_request(
            superuser, method="post", path="/ai/admin/wizard/seen/",
            data='{"scope": "admin_super"}', content_type="application/json",
        )
        self._enter_view_as(
            request, self._view_as_group("Эмуля-wz", ["page.admin"]),
        )
        response = admin_wizard_seen_view(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content), {"ok": True})
        self.assertEqual(AIWizardSeen.objects.count(), 0)


class WizardUserPageContextTests(TestCase):
    """Рендер страниц юзеров: конфиг тура в json_script + линки wizard-ассетов."""

    def _render(self, view, url, user=None):
        request = self.factory.get(url)
        request.user = user if user is not None else self.user
        request.session = {}
        request.user_info = {"userId": "wz-context-user"}
        request.COOKIES = {"userId": "wz-context-user"}
        # CSRF_COOKIE не задаём: get_token(request) сам сгенерирует свежий
        # токен (свой алфавит маскировки — произвольная строка не годится).
        with patch("ai.views.AIAppSettings.get_solo", return_value=SimpleNamespace(is_enabled=True)), \
             patch("ai.views.get_available_model_options", return_value=[]):
            response = view(request)
        return response.content.decode("utf-8")

    def setUp(self):
        self.factory = RequestFactory()
        self.user_model = get_user_model()
        self.user = self.user_model.objects.create_user(
            username="wz-context-user", password="x",
        )
        self.plain_user = self.user_model.objects.create_user(
            username="wz-context-plain", password="x",
        )
        self.staff_user = self.user_model.objects.create_user(
            username="wz-context-staff", password="x", is_staff=True,
        )
        self.super_user = self.user_model.objects.create_user(
            username="wz-context-super", password="x", is_superuser=True,
        )

    def test_chat_page_contains_wizard_config_and_assets(self):
        html = self._render(chat_view, "/ai/chat/")
        self.assertIn('id="ai-wizard-config"', html)
        self.assertIn('"scope": "chat"', html)
        self.assertIn("/ai/assets/admin/js/ai-wizard.js", html)
        self.assertIn("/ai/assets/admin/css/ai-wizard.css", html)
        self.assertIn("aiDocsTourBtn", html)

    def test_solve_page_wizard_scope(self):
        from ai.views import decide_task_view

        html = self._render(decide_task_view, "/ai/solve-problem/")
        self.assertIn('"scope": "solve"', html)

    def test_find_error_page_wizard_scope(self):
        from ai.views import find_error_view

        html = self._render(find_error_view, "/ai/find-error/")
        self.assertIn('"scope": "find_error"', html)

    def test_seen_tour_renders_show_false(self):
        from ai.services.onboarding import record_wizard_seen

        record_wizard_seen(self.user, "chat")
        html = self._render(chat_view, "/ai/chat/", user=self.user)
        self.assertIn('"show": false', html)

    def test_payload_carries_role_for_engine_steps(self):
        """Роль в конфиге: ролевые шаги движка (side-меню чата — super/staff)."""
        from ai.views import decide_task_view, find_error_view

        html = self._render(chat_view, "/ai/chat/", user=self.plain_user)
        self.assertIn('"role": ""', html)
        html = self._render(chat_view, "/ai/chat/", user=self.staff_user)
        self.assertIn('"role": "staff"', html)
        html = self._render(chat_view, "/ai/chat/", user=self.super_user)
        self.assertIn('"role": "super"', html)
        html = self._render(decide_task_view, "/ai/solve-problem/", user=self.plain_user)
        self.assertIn('"role": ""', html)
        html = self._render(find_error_view, "/ai/find-error/", user=self.staff_user)
        self.assertIn('"role": "staff"', html)

    def test_payload_carries_basics_seen(self):
        """basics_seen в payload: тур другой юзер-страницы закрыл «общие» шаги
        (семантика — в OnboardingServiceTests.user_basics_seen)."""
        from ai.services.onboarding import record_wizard_seen

        html = self._render(chat_view, "/ai/chat/", user=self.plain_user)
        self.assertIn('"basics_seen": false', html)
        record_wizard_seen(self.plain_user, "solve")
        html = self._render(chat_view, "/ai/chat/", user=self.plain_user)
        self.assertIn('"basics_seen": true', html)
        # Админские отметки юзер-«общие» не закрывают.
        plain2 = self.user_model.objects.create_user(
            username="wz-context-plain2", password="***",
        )
        record_wizard_seen(plain2, "admin_pd")
        html = self._render(chat_view, "/ai/chat/", user=plain2)
        self.assertIn('"basics_seen": false', html)

    def test_task_prefill_node_id_on_all_three_pages(self):
        """Префилл условия задачи со страницы DL: ?nid=… прокидывается в шаблон
        на всех трёх страницах (data-node-id), а window.AI_TASK_NODE_ID ставит
        общий загрузчик в ai-common.js (initAiTaskNodeId).

        Раньше «Реши задачу» держало nid, а «Чат с DLAI» и «В чём ошибка?»
        игнорировали его вовсе — текст задачи никогда не подставлялся.

        Инлайн-скрипт с window.AI_TASK_NODE_ID в шаблонах больше НЕ живёт
        (правило «JS — в файлах»): значение приходит из data-node-id поля
        страницы, которое заполняет initAiTaskNodeId на DOMContentLoaded.
        """
        from ai.views import decide_task_view, find_error_view

        for view, url in (
            (chat_view, "/ai/chat/"),
            (decide_task_view, "/ai/solve-problem/"),
            (find_error_view, "/ai/find-error/"),
        ):
            with self.subTest(url=url):
                html = self._render(view, f"{url}?nid=2607154")
                self.assertIn('data-node-id="2607154"', html)
                # Страница подключает общий ai-common.js — там и ставится nid.
                self.assertIn("/ai/assets/admin/js/ai-common.js", html)
        # Без nid — пустое значение, страница живёт как раньше.
        html = self._render(chat_view, "/ai/chat/")
        self.assertIn('data-node-id=""', html)

    def test_task_prefill_static_loader_markers(self):
        """Общий лоадер условия в ai-common и его вызов на find-error/chat:
        маркеры кода для регресс-безопасности (версии статики в шаблонах
        иногда откатываются мержами — маркеры ловят откат)."""
        import ai
        from pathlib import Path
        base = Path(ai.__file__).parent.parent / "static/admin/js"
        common = (base / "ai-common.js").read_text(encoding="utf-8")
        self.assertIn("async function aiLoadTaskFromUrl", common)
        fe = (base / "find_error.js").read_text(encoding="utf-8")
        self.assertIn("aiLoadTaskFromUrl(document.getElementById('taskText')", fe)
        ct = (base / "chat_template.js").read_text(encoding="utf-8")
        self.assertIn("aiLoadTaskFromUrl(document.getElementById('messageText')", ct)


class AIWizardSeenAdminPermissionTests(_AdminViewRequestMixin, TestCase):
    """Листинг «кто видел wizard» (AIWizardSeenAdmin): просмотр — только
    суперюзер; удаление строки — способ перезапустить тур пользователю."""

    def setUp(self):
        self.factory = RequestFactory()
        self.user_model = get_user_model()

    def _admin_instance(self):
        from ai.admin.models import AIWizardSeenAdmin
        from ai.admin.site import ai_admin_site
        from ai.models import AIWizardSeen as Model

        return AIWizardSeenAdmin(Model, ai_admin_site)

    def test_superuser_permissions_granted(self):
        superuser = self.user_model.objects.create_user(
            username="wz-list-super", password="x", is_superuser=True,
        )
        request = self._admin_request(superuser)
        model_admin = self._admin_instance()
        self.assertTrue(model_admin.has_module_permission(request))
        self.assertTrue(model_admin.has_view_permission(request))
        self.assertTrue(model_admin.has_delete_permission(request))
        # add/change по смыслу бессмысленны (строки создаются движком), но
        # права не зажаты сильнее общего супер-гейта.
        self.assertTrue(model_admin.has_change_permission(request))

    def test_prompt_developer_permissions_denied(self):
        pd_user = self.user_model.objects.create_user(username="wz-list-pd", password="x")
        make_prompt_developer(pd_user)
        request = self._admin_request(pd_user)
        model_admin = self._admin_instance()
        self.assertFalse(model_admin.has_module_permission(request))
        self.assertFalse(model_admin.has_view_permission(request))
        self.assertFalse(model_admin.has_delete_permission(request))

    def test_staff_permissions_denied(self):
        staff_user = self.user_model.objects.create_user(
            username="wz-list-staff", password="x", is_staff=True,
        )
        request = self._admin_request(staff_user)
        model_admin = self._admin_instance()
        self.assertFalse(model_admin.has_view_permission(request))


class WizardAdminContextTests(_AdminViewRequestMixin, TestCase):
    """each_context: ai_wizard — scope по пути, роль, гостевой режим, отметка."""

    def setUp(self):
        self.factory = RequestFactory()
        self.user_model = get_user_model()

    def _context(self, user, path="/ai/admin/", view_as_group=None):
        from ai.admin.site import ai_admin_site

        request = self._admin_request(user, path=path)
        if view_as_group is not None:
            self._enter_view_as(request, view_as_group)
        return ai_admin_site.each_context(request)["ai_wizard"]

    def test_dashboard_scope_by_role(self):
        """Тур ГЛАВНОГО ЭКРАНА разделён по правам: свой scope на каждый уровень."""
        superuser = self.user_model.objects.create_user(
            username="wz-ctx-super", password="x", is_superuser=True,
        )
        ctx = self._context(superuser)
        self.assertEqual(ctx["scope"], "admin_super")
        self.assertEqual(ctx["role"], "super")
        self.assertTrue(ctx["show"])

        staff_user = self.user_model.objects.create_user(
            username="wz-ctx-staff", password="x", is_staff=True,
        )
        ctx = self._context(staff_user)
        self.assertEqual(ctx["scope"], "admin_staff")
        self.assertEqual(ctx["role"], "staff")
        self.assertTrue(ctx["show"])

        pd_user = self.user_model.objects.create_user(username="wz-ctx-pd", password="x")
        make_prompt_developer(pd_user)
        ctx = self._context(pd_user)
        self.assertEqual(ctx["scope"], "admin_pd")
        self.assertEqual(ctx["role"], "pd")
        self.assertTrue(ctx["show"])

    def test_arm_solve_and_model_status_scopes(self):
        """Большие страницы — общие туры, без деления по правам."""
        user = self.user_model.objects.create_user(username="wz-ctx-super", password="x", is_superuser=True)
        self.assertEqual(self._context(user, path="/ai/admin/arm/solve/")["scope"], "admin_arm_solve")
        self.assertEqual(self._context(user, path="/ai/admin/arm/models/")["scope"], "admin_model_status")

    def test_test_data_scope(self):
        """У генератора тестовых записей свой тур (контент страницы)."""
        user = self.user_model.objects.create_user(username="wz-ctx-td", password="x", is_superuser=True)
        ctx = self._context(user, path="/ai/admin/test_data/")
        self.assertEqual(ctx["scope"], "admin_test_data")
        self.assertTrue(ctx["show"])

    def test_pd_role_and_staff_role(self):
        pd_user = self.user_model.objects.create_user(username="wz-ctx-pd2", password="x")
        make_prompt_developer(pd_user)
        staff_user = self.user_model.objects.create_user(
            username="wz-ctx-staff2", password="x", is_staff=True,
        )
        self.assertEqual(self._context(pd_user)["role"], "pd")
        self.assertEqual(self._context(staff_user)["role"], "staff")

    def test_guest_mode_suppresses_show(self):
        superuser = self.user_model.objects.create_user(
            username="wz-ctx-guest", password="x", is_superuser=True,
        )
        viewas = self._view_as_group("Эмуля-wzctx", ["page.admin"])
        superuser2 = self.user_model.objects.create_user(
            username="wz-ctx-viewas", password="x", is_superuser=True, is_staff=True,
        )
        ctx = self._context(superuser2, view_as_group=viewas)
        self.assertFalse(ctx["show"])

    def test_seen_scope_is_suppressed(self):
        """Закрытый ролевой тур не повторяется; «большие страницы» — свой тур."""
        from ai.services.onboarding import record_wizard_seen

        user = self.user_model.objects.create_user(username="wz-ctx-seen", password="x", is_superuser=True)
        record_wizard_seen(user, "admin_super")
        self.assertFalse(self._context(user)["show"])
        self.assertTrue(self._context(user, path="/ai/admin/arm/solve/")["show"])

    def test_role_upgrade_runs_unseen_role_tour(self):
        """Повышение роли: pd-тур пройден → staff-тур показывается; после него
        — и super-тур (ролевые scope'ы независимы)."""
        from ai.services.onboarding import record_wizard_seen, should_show_wizard

        user = self.user_model.objects.create_user(
            username="wz-ctx-promo", password="x", is_staff=True,
        )
        record_wizard_seen(user, "admin_pd")
        self.assertFalse(should_show_wizard(user, "admin_pd"))
        self.assertTrue(should_show_wizard(user, "admin_staff"))
        record_wizard_seen(user, "admin_staff")
        self.assertTrue(should_show_wizard(user, "admin_super"))

    def test_role_downgrade_does_not_replay_seen_tour(self):
        """Понижение роли: пройденные младшие туры не переигрываются заново."""
        from ai.services.onboarding import record_wizard_seen

        user = self.user_model.objects.create_user(
            username="wz-ctx-demoted", password="x", is_staff=True,
        )
        record_wizard_seen(user, "admin_pd")
        record_wizard_seen(user, "admin_staff")
        user.is_staff = False
        user.save(update_fields=["is_staff"])
        ctx = self._context(user)  # теперь «pd»
        self.assertEqual(ctx["scope"], "admin_pd")
        self.assertFalse(ctx["show"])

    def test_page_scopes_by_path(self):
        """Главные страницы админки — свой тур (контент страницы, без хрома:
        бар и меню расписывает главный экран)."""
        user = self.user_model.objects.create_user(
            username="wz-ctx-pages", password="x", is_superuser=True,
        )
        expectations = (
            ("/ai/admin/ai/request_logs/", "admin_logs"),
            ("/ai/admin/ai/request_logs/123/", "admin_logs"),
            ("/ai/admin/pinned-runs/", "admin_pinned_runs"),
            ("/ai/admin/ai/tasksolution/", "admin_tasksolution"),
            ("/ai/admin/ai/aiappsettings/", "admin_aiappsettings"),
            ("/ai/admin/ai/aiappsettings/1/change/", "admin_aiappsettings"),
            ("/ai/admin/updates/", "admin_updates"),
            ("/ai/admin/prompt-defaults/", "admin_prompt_defaults"),
            ("/ai/admin/test-console/", "admin_test_console"),
            ("/ai/admin/prompts/my/", "admin_my_prompt"),
        )
        for path, expected_scope in expectations:
            self.assertEqual(
                self._context(user, path=path)["scope"], expected_scope,
                msg=path,
            )

    def test_stock_changelist_paths_have_no_autostart(self):
        """Стоковые таблицы/доки: автостарта нет — главный экран уже расписал
        бар и все разделы; повторять его на каждой таблице нельзя. Ролевой
        scope в payload остаётся (data-фоллбэк для ручного запуска)."""
        user = self.user_model.objects.create_user(
            username="wz-ctx-stock", password="x", is_superuser=True,
        )
        for path in ("/ai/admin/ai/topic/", "/ai/admin/docs/developer/", "/ai/admin/login/"):
            ctx = self._context(user, path=path)
            self.assertFalse(ctx["show"], msg=path)
            self.assertEqual(ctx["scope"], "admin_super", msg=path)


class WizardAssetAccessTests(TestCase):
    """asset_view отдаёт новый JS/CSS wizard'а (форма AssetViewAccessTests)."""

    def setUp(self):
        self.factory = RequestFactory()

    def _get(self, asset_path, cookies=None, user=None):
        from ai.views import asset_view

        request = self.factory.get(f"/ai/assets/{asset_path}")
        if cookies:
            request.COOKIES = cookies
        if user is not None:
            request.user = user
        return asset_view(request, asset_path)

    def test_wizard_js_served_with_user_id_cookie(self):
        user = get_user_model().objects.create_user(username="wz-asset", password="x")
        response = self._get(
            "admin/js/ai-wizard.js",
            cookies={"userId": "wz-asset"},
            user=user,
        )
        self.assertEqual(response.status_code, 200)

    def test_wizard_css_served_with_user_id_cookie(self):
        user = get_user_model().objects.create_user(username="wz-asset", password="x")
        response = self._get(
            "admin/css/ai-wizard.css",
            cookies={"userId": "wz-asset"},
            user=user,
        )
        self.assertEqual(response.status_code, 200)

    def test_assets_require_external_id(self):
        user = get_user_model().objects.create_user(username="wz-asset", password="x")
        self.assertEqual(
            self._get("admin/js/ai-wizard.js", user=user).status_code, 403,
        )


class DashboardWizardRestartLinkTests(TestCase):
    """Кнопка повторного показа тура на дашборде админки."""

    def test_dashboard_contains_restart_button(self):
        from django.template.loader import render_to_string

        html = render_to_string(
            "admin/ai/index.html",
            {
                "user_dl_id": "42",
                "user_display_name": "Иван Иванов",
                "user_role_label": "Суперпользователь",
                "user": SimpleNamespace(),
                "available_apps": [],
            },
        )
        self.assertIn("data-ai-wizard-restart", html)


# === Триязычная клиентская часть: словари строк, WS-статусы, локализация ошибок ===

class ClientStringsTests(SimpleTestCase):
    """client_strings(): словарь первого рендера для трёх языков."""

    def test_all_languages_have_same_keys(self):
        from ai.i18n import _CLIENT_STRINGS
        key_sets = [set(table) for table in _CLIENT_STRINGS.values()]
        self.assertEqual(key_sets[0], key_sets[1], "ключи client_strings расходятся ru/en")
        self.assertEqual(key_sets[1], key_sets[2], "ключи client_strings расходятся en/fr")

    def test_language_values(self):
        from ai.i18n import client_strings
        self.assertEqual(client_strings("Русский")["send"], "Отправить")
        self.assertEqual(client_strings("English")["send"], "Send")
        self.assertEqual(client_strings("Français")["send"], "Envoyer")

    def test_unknown_language_falls_back_to_russian(self):
        from ai.i18n import client_strings
        self.assertEqual(client_strings(""), client_strings("Русский"))
        self.assertEqual(client_strings("Klingon"), client_strings("Русский"))


class WsTextTests(SimpleTestCase):
    """ws_text(): локализованные серверные тексты WS-протокола."""

    def test_langs_have_same_keys(self):
        from ai.i18n import _WS_STRINGS
        key_sets = [set(table) for table in _WS_STRINGS.values()]
        self.assertEqual(key_sets[0], key_sets[1], "ключи ws_text расходятся ru/en")
        self.assertEqual(key_sets[1], key_sets[2], "ключи ws_text расходятся en/fr")

    def test_placeholder_formatting(self):
        from ai.i18n import ws_text
        self.assertEqual(
            ws_text("English", "model_not_found", model_key="Web_DeepSeek"),
            "Model Web_DeepSeek not found. Please use the available models.",
        )
        self.assertEqual(
            ws_text("Français", "processing", message="Bonjour"),
            "Traitement de votre requête. Vous : Bonjour",
        )

    def test_russian_default_and_cleared(self):
        from ai.i18n import ws_text
        # Пустой язык → русский; формулировки RU сниффятся фронтендом.
        self.assertEqual(ws_text("", "cleared"), "Контекст очищен")
        self.assertIn("Запрос успешно обработан", ws_text("Русский", "success_head"))

    def test_unknown_key_returns_key(self):
        from ai.i18n import ws_text
        self.assertEqual(ws_text("English", "no_such_key"), "no_such_key")


class HumanizeErrorLanguageTests(SimpleTestCase):
    """humanize_model_error(language=...): локализация friendly-текстов."""

    def test_pattern_matched_localized(self):
        from ai.model_clients.exceptions import humanize_model_error
        ru, _ = humanize_model_error("Код 429: превышен лимит", language="Русский")
        en, _ = humanize_model_error("Код 429: превышен лимит", language="English")
        fr, _ = humanize_model_error("Код 429: превышен лимит", language="Français")
        self.assertIn("429", ru)
        self.assertIn("429", en)
        self.assertIn("429", fr)
        self.assertNotEqual(en, ru)
        self.assertNotEqual(fr, ru)

    def test_detail_label_localized(self):
        from ai.model_clients.exceptions import humanize_model_error
        _, detailed_en = humanize_model_error(
            "Error 401: unauthorized", include_detail=True, language="English")
        self.assertIn("Technical detail", detailed_en)
        _, detailed_ru = humanize_model_error(
            "Error 401: unauthorized", include_detail=True, language="Русский")
        self.assertIn("Техническая деталь", detailed_ru)

    def test_default_language_is_russian(self):
        from ai.model_clients.exceptions import humanize_model_error
        no_lang, _ = humanize_model_error("Код 429: превышен лимит")
        ru, _ = humanize_model_error("Код 429: превышен лимит")
        self.assertEqual(no_lang, ru)

    def test_long_text_passthrough_untouched(self):
        from ai.model_clients.exceptions import humanize_model_error
        raw = "x" * 400
        text, _ = humanize_model_error(raw)
        self.assertEqual(text, raw)


class WsStatusMarkerTests(SimpleTestCase):
    """Структурированный статус терминальных WS-сообщений (протокол фронтенда)."""

    def test_with_status_appends_marker(self):
        from ai.consumers import _with_status
        payload = _with_status("Контекст очищен", "cleared")
        self.assertTrue(payload.endswith("\n[[ai-status:cleared]]"))
        self.assertTrue(payload.startswith("Контекст очищен"))

    def test_format_success_carries_success_and_language(self):
        from ai.consumers import ResponseFormatter
        payload = ResponseFormatter().format_success(
            "12:00:00", "Model A", "1.000 сек", "answer", 5, "English")
        self.assertTrue(payload.endswith("[[ai-status:success]]"))
        self.assertIn("Model:", payload)
        self.assertIn("Tokens spent: 5", payload)

    def test_processing_marker(self):
        from ai.consumers import ResponseFormatter
        payload = ResponseFormatter().format_user_processing("12:00:00", "Hi", "English")
        self.assertTrue(payload.endswith("[[ai-status:processing]]"))
        self.assertIn("Processing your request. You: Hi", payload)

    def test_error_status_on_simple_success(self):
        from ai.consumers import ResponseFormatter
        payload = ResponseFormatter().format_simple_success(
            "12:00:00", "boom", "English", status="error")
        self.assertTrue(payload.endswith("[[ai-status:error]]"))

    def test_format_duration_localized(self):
        from ai.consumers import ResponseFormatter
        f = ResponseFormatter()
        self.assertIn("s", f.format_duration(30.5, "English"))
        self.assertIn("сек", f.format_duration(30.5, "Русский"))
        self.assertIn("мин", f.format_duration(120.0, "Русский"))
        self.assertIn("min", f.format_duration(120.0, "English"))


class BaseChatTemplateLocalizationTests(SimpleTestCase):
    """base_chat.html рендерится во всех трёх языках из t = client_strings()."""

    def _render(self, lang):
        import datetime
        from django.template.loader import render_to_string
        from ai.i18n import client_strings
        request = self.factory.get("/ai/chat/")
        context = {
            "request": request,
            "t": client_strings(lang),
            "available_models": [
                {"key": "X_1", "title": "X 1"},
                {"key": "OR_X", "title": "X Open"},
            ],
            "other_models": [{"key": "X_1", "title": "X 1"}],
            "favorite_models": [],
            "experimental_models": [{"key": "OR_X", "title": "X Open"}],
            "default_model_key": "X_1",
            "saved_lang": lang,
            "last_update_date": timezone.make_aware(datetime.datetime(2026, 10, 13)),
            "ai_wizard": None,
        }
        return render_to_string("ai/base_chat.html", context)

    def setUp(self):
        self.factory = RequestFactory()

    def test_default_russian(self):
        html = self._render("Русский")
        self.assertIn('label="Все модели"', html)
        self.assertIn("Отправить", html)
        self.assertIn("Админ-Панель", html)

    def test_english_render(self):
        html = self._render("English")
        self.assertIn('label="All models"', html)
        self.assertIn(">Send<", html)
        self.assertIn("Admin Panel", html)
        self.assertIn('data-ai-group="experimental"', html)

    def test_french_render(self):
        html = self._render("Français")
        self.assertIn('label="Tous les modèles"', html)
        self.assertIn("Envoyer", html)
        self.assertIn("Panneau Admin", html)


class BatchRunStatusPausedDisplayTests(SimpleTestCase):
    """Пауза batch-прогона — «Пауза» в журнале и «Настройке ИИ» (2026-10)."""

    def test_status_mapping(self):
        from ai.admin.request_logs import batch_run_status_display
        from ai.models import AIModelTestRun
        self.assertEqual(batch_run_status_display(AIModelTestRun.STATUS_PAUSED), "Пауза")


class DurationFormatTests(SimpleTestCase):
    """format_human_duration: нули старших разрядов опускаются, None/0 → ""."""

    def test_boundaries(self):
        from ai.templatetags.ai_duration import format_human_duration
        self.assertEqual(format_human_duration(4462), "1 ч 14 мин")
        self.assertEqual(format_human_duration(3630), "1 ч 0 мин")
        self.assertEqual(format_human_duration(3660), "1 ч 1 мин")
        self.assertEqual(format_human_duration(63), "1 мин 3 с")
        self.assertEqual(format_human_duration(60), "1 мин")
        self.assertEqual(format_human_duration(30), "30 с")
        self.assertEqual(format_human_duration(7.3), "7 с")
        self.assertEqual(format_human_duration(None), "")
        self.assertEqual(format_human_duration(0), "")


class MpaMultiWindowExtractionTests(SimpleTestCase):
    """Multi-window экстрактор С-МПА (лог #2606801, «выкусывания навсегда»):
    модель пишет СПЛОШНУЮ простыню рассуждения без фенсов и внутри неё
    ПОЛНОСТЬЮ проговаривает программу (иногда несколько раз). Конвейер
    должен вернуть финальную программу, а не «Не удалось извлечь код»."""

    def _c(self, text, ext=".mpc"):
        from ai.arm_runner import _extract_code_from_response
        return _extract_code_from_response(text, file_extension=ext)

    def _wall(self, prog, repeat=2):
        """Простыня размышлений: программа внутри прозы, repeat раз."""
        wall = "We need to answer only code.\n" + prog
        for _ in range(repeat - 1):
            wall += "\nLet me re-verify. The final answer program is:\n" + prog
        return wall

    def test_program_extracted_from_prose_wall(self):
        """Проза-простыня без фенсов вокруг программы → извлекается программа,
        проза не тянетя и не склеивает две программы в одного кандидата."""
        prog = (
            "int __in __bits(16) a;\nint __in __bits(16) b;\n"
            "int __out __bits(16) y;\nvoid main() {\n"
            "  if (a > b) { { y = a; } } else { { y = b; } }\n}"
        )
        got = self._c(self._wall(prog), ext=".mpc")
        self.assertEqual(got, prog)

    def test_duplicate_program_between_windows_not_rejected(self):
        """Модель описывает программу дважды в разных местах текста: дубли
        МЕЖДУ окнами — норма (каждое окно закрывается на своём балансе скобок,
        а не растягивается до последней скобки файла)."""
        prog = (
            "int __in __bits(8) x;\nint __out __bits(8) z;\n"
            "void main() {\nz = x;\n}"
        )
        got = self._c(self._wall(prog, repeat=2), ext=".mpc")
        self.assertEqual(got, prog)

    def test_unbalanced_prose_window_dropped(self):
        """Фрагмент с `{` без закрытия внутри прозы отбрасывается СВОИМ окном:
        полная программа рядом всё равно извлекается."""
        small = "int __in __bits(8) x;\nint __out __bits(8) z;\nvoid main() {\nz = x;\n}"
        wall = (
            "Intro prose includes a broken fragment:\n"
            "void main() {\ny = something and then more prose never closes it\n"
            "Now the correct program:\n" + small + "\nDone."
        )
        self.assertEqual(self._c(wall, ext=".mpc"), small)

    def test_pseudo_c_port_declarations_are_anchors(self):
        """Псевдо-C диалект портов («unsigned char __in __bits(8) N1;» — тип
        В НАЧАЛЕ строки) тоже является якорем-объявлением: регулярка матчила
        только старый DL-каркас «int N1 __in __bits(8);», окно снимали по main
        и порты выпадали — «Undefined variable N1» на DL (лог queue 15211904).
        Обычный `unsigned char n;` (без __in/__out/__bits) якорем НЕ станет."""
        prog = (
            "unsigned char __in __bits(8) N1;\n"
            "unsigned char __out __bits(8) R;\n"
            "void main() {\n"
            "  unsigned char n;\n"
            "  n = N1;\n"
            "  R = n;\n"
            "}"
        )
        wall = "Here is the solution program:\n" + prog + "\nHope this helps."
        self.assertEqual(self._c(wall, ext=".mpc"), prog)

    def test_window_closes_at_first_balanced_main(self):
        """Окно закрывается на ПЕРВОМ балансе скобок: два main подряд дают
        отдельных кандидатов («от анкера до конца файла» больше не существует)."""
        text = (
            "int __in __bits(8) x;\nvoid main() {\nx = 1;\n}\n"
            "Now reconsider: rewrite it again.\n"
            "void main() {\nx = 2;\n}\n"
        )
        # У decl+main-окна 4 строки против 3 у main-окон — выигрывает decl+main1.
        self.assertEqual(
            self._c(text, ext=".mpc"),
            "int __in __bits(8) x;\nvoid main() {\nx = 1;\n}",
        )

    def test_relaxed_keeps_prose_inside_window(self):
        """Relaxed-тир (прямые вызовы code_carver): строгий режим срезает прозу
        ВНУТРИ кандидата, relaxed сохраняет кандидата как есть."""
        from ai.services.code_carver import extract_mpa_c
        wall = "We need to answer only code.\n" + (
            "int __in __bits(8) x;\nint __out __bits(8) z;\nvoid main() {\n"
            "Here the model reasons at length in plain English and only later "
            "arrives at the assignment for the output bit.\n"
            "z = x;\n}"
        )
        strict = extract_mpa_c(wall)
        relaxed = extract_mpa_c(wall, relaxed=True)
        self.assertIn("z = x;", strict)
        self.assertNotIn("Here the model", strict)
        self.assertIn("z = x;", relaxed)
        self.assertIn("Here the model", relaxed)

    def test_gate_blocks_markerless_prose(self):
        """Гейт has_language_marker в _extract_code_from_response — единственный,
        relaxing не снимает его: без структурных маркеров (нет ни decl
        `__bits`, ни `void main()`) простыня → пусто."""
        self.assertEqual(self._c("We need to answer only code. No program here."), "")
        self.assertEqual(
            self._c("Sure. Here we go. Let me write it. Yes I will. Very short."), ""
        )

    def test_asm_program_extracted_from_prose_wall(self):
        """То же для i8086: программа-концевик внутри прозы возвращается целиком."""
        asm = "jmp begin\nbegin:\nmov ax, 1\nout 1\nEnds:\njmp Ends ;$E"
        self.assertEqual(self._c(self._wall(asm), ext=".i86"), asm)

    def test_helper_functions_before_main_are_kept(self):
        """Хелперы (abs_val/sign) ДО main — часть программы: окно от
        якоря-объявления закрывается на закрытии main, а НЕ на первой
        сбалансированной функции (иначе в code пишется голый main и DL
        падает «Undefined variable …», лог случая задачи №470/16126)."""
        prog = (
            "int __in __bits(16) a;\nint __in __bits(16) b;\n"
            "int __in __bits(8) c;\nint __in __bits(8) d;\n"
            "int __out __bits(16) RES;\n"
            "\n"
            "int abs_val(int x) {\n    return (x < 0) ? -x : x;\n}\n"
            "\n"
            "void main() {\n"
            "    if (c > 0) {\n        RES = abs_val(a) + d;\n"
            "    } else {\n        RES = abs_val(b);\n    }\n"
            "}"
        )
        # Голый ответ модели (без оградок и прозы): declи + хелпер + main целиком.
        self.assertEqual(self._c(prog), prog)
        # И внутри простыни размышлений.
        self.assertEqual(self._c(self._wall(prog)), prog)

    def test_helper_fenced_code_kept(self):
        """То же через закрытую оградку: структурник работает по тексту фенса
        тем же окном — хелперы не теряются."""
        prog = (
            "int __in __bits(8) x;\nint __out __bits(8) z;\n"
            "int dbl(int v) {\n    return v + v;\n}\n"
            "void main() {\nz = dbl(x);\n}"
        )
        self.assertEqual(
            self._c("Ответ:\n```mpc\n" + prog + "\n```\nГотово."),
            prog,
        )


class ArmPromptResultPersistenceTests(TestCase):
    """Снимок промпта пары (AIModelTestResult.prompt_id/prompt_name):
    явный prompt_id → записан в пару; «по привязке»/без промпта → NULL/"" ;
    снимок доезжает до снапшотов (light-строки и журнал)."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(username="ppsnap", password="x")
        self.lang = ProgrammingLanguage.objects.create(language_name="Pascal")
        self.topic = Topic.objects.create(topic_name_ru="Линейные", programming_language=self.lang)
        self.t1 = Task.objects.create(
            node_id=7101, task_id=8101, name="A", statement="Сложите a и b",
            topic=self.topic, programming_language=self.lang, file_extension=".pas",
        )
        self.prompt = Prompt.objects.create(
            prompt_name_ru="Учитель Паскаля",
            prompt_text_ru="Ты учитель Паскаля.",
        )

    @staticmethod
    def _dl_ok():
        def dl_mock(sid, node_id, code, ext, **kw):
            return {
                "verdict": "solved", "comment": "Все тесты успешно пройдены",
                "submit_error": "", "queue_id": 11, "code_sent": code,
            }
        return dl_mock

    def _run(self, prompt_id):
        from ai import arm_runner

        async def fake_handler(messages, conv_id):
            return ("program a; begin writeln(1); end.", 12)

        run_id = uuid.uuid4().hex
        now_ts = time.time()
        arm_runner._jobs[run_id] = {
            "run_id": run_id, "user_id": self.user.id, "run_type": "batch",
            "status": "running", "error_message": "",
            "cancel_requested": False, "cancelled": False,
            "pause_requested": False, "paused": False,
            "total_models": 1, "total_pairs": 1, "completed_pairs": 0,
            "completed_models": 0, "current_model_key": "FakeModel",
            "current_model_title": "FakeModel",
            "current_task_node_id": "", "current_task_name": "",
            "model_progress": [], "results": [], "report": None,
            "created_at_ts": now_ts, "updated_at_ts": now_ts,
        }
        try:
            with patch("ai.arm_runner._test_solution_on_dl", self._dl_ok()):
                arm_runner._run_batch_job_worker(
                    run_id, [self.t1.node_id],
                    [{"key": "FakeModel", "title": "FakeModel", "handler": fake_handler}],
                    self.user.id, "DLSID-1",
                    ui_language="Русский", dl_test=True,
                    solve_file_extension=".pas", solve_prog_lang_name="Pascal",
                    programming_language_id=self.lang.id,
                    course_id=1450, prompt_id=prompt_id,
                    run_params={"record_stats": False},
                )
        finally:
            arm_runner._jobs.pop(run_id, None)
        return run_id

    def _row(self, run_id):
        from ai.models import AIModelTestResult, AIModelTestRun
        run = AIModelTestRun.objects.get(run_id=run_id)
        return AIModelTestResult.objects.get(run=run)

    def test_explicit_prompt_saved(self):
        row = self._row(self._run(self.prompt.id))
        self.assertEqual(row.prompt_id, self.prompt.id)
        self.assertEqual(row.prompt_name, "Учитель Паскаля")

    def test_no_prompt_is_null(self):
        """prompt_id не задан и привязки нет → NULL/"" (в UI «—»)."""
        row = self._row(self._run(None))
        self.assertIsNone(row.prompt_id)
        self.assertEqual(row.prompt_name, "")

    def test_snapshot_covers_light_and_full(self):
        """Снимок пары доезжает до light-строк и до снапшота журнала."""
        from ai import arm_runner
        from ai.admin.request_logs import _build_batch_log_snapshot
        from ai.models import AIModelTestRun
        run_id = self._run(self.prompt.id)
        run_obj = AIModelTestRun.objects.get(run_id=run_id)
        light = arm_runner._batch_results_from_db(run_obj, light=True)[0]
        self.assertEqual(light["prompt_name"], "Учитель Паскаля")
        sn = _build_batch_log_snapshot(
            AIRequestLog.objects.create(
                user=self.user, source="arm", mode="batch_solve",
                message=f"Batch solve run {run_id}",
                status=AIRequestLog.STATUS_SUCCESS, sent_at=timezone.now(),
            )
        )
        self.assertEqual(
            sn["prompts"], [{"id": self.prompt.id, "name": "Учитель Паскаля"}]
        )

    def test_light_row_without_prompt_is_empty(self):
        from ai import arm_runner
        from ai.models import AIModelTestRun
        run_id = self._run(None)
        run_obj = AIModelTestRun.objects.get(run_id=run_id)
        light = arm_runner._batch_results_from_db(run_obj, light=True)[0]
        self.assertEqual(light["prompt_id"], None)
        self.assertEqual(light["prompt_name"], "")


class ArmPauseTests(TestCase):
    """Приостановка batch-прогона (2026-10): flip job+БД, снятие, отмена из
    паузы, отказ rerun-гварда, снапшот и orphan-самоисцеление по paused."""

    def setUp(self):
        from ai import arm_runner
        from ai.models import AIModelTestRun
        self.run_id = "d" * 32
        self.test_run = AIModelTestRun.objects.create(
            run_id=self.run_id,
            run_type=AIModelTestRun.RUN_TYPE_BATCH,
            status=AIModelTestRun.STATUS_RUNNING,
        )
        now = time.time()
        self.job = {
            "run_id": self.run_id, "user_id": 1, "run_type": "batch",
            "status": "running", "error_message": "",
            "cancel_requested": False, "cancelled": False,
            "pause_requested": False, "paused": False,
            "total_models": 1, "total_pairs": 2, "completed_pairs": 1,
            "completed_models": 0, "current_model_key": "FakeModel",
            "current_model_title": "FakeModel",
            "current_task_node_id": "", "current_task_name": "",
            "model_progress": [], "results": [], "report": None,
            "created_at_ts": now, "updated_at_ts": now,
        }
        arm_runner._jobs[self.run_id] = self.job

    def tearDown(self):
        from ai import arm_runner
        arm_runner._jobs.pop(self.run_id, None)

    def test_pause_and_resume_flip_job_and_db(self):
        from ai import arm_runner
        from ai.models import AIModelTestRun
        self.assertTrue(arm_runner.pause_arm_run(self.run_id))
        self.assertEqual(self.job["status"], "paused")
        self.assertTrue(self.job["pause_requested"])
        self.test_run.refresh_from_db()
        self.assertEqual(self.test_run.status, AIModelTestRun.STATUS_PAUSED)

        self.assertTrue(arm_runner.resume_arm_run(self.run_id))
        self.assertEqual(self.job["status"], "running")
        self.assertFalse(self.job["paused"])
        self.test_run.refresh_from_db()
        self.assertEqual(self.test_run.status, AIModelTestRun.STATUS_RUNNING)

    def test_pause_without_live_job_returns_false(self):
        """БД-прогон без живого job НЕ ставится на паузу (орфан добьёт
        orphan-детектор) — вью вернёт отказ «Процесс не найден…»."""
        from ai import arm_runner
        from ai.models import AIModelTestRun
        arm_runner._jobs.pop(self.run_id, None)
        self.assertFalse(arm_runner.pause_arm_run(self.run_id))
        self.test_run.refresh_from_db()
        self.assertEqual(self.test_run.status, AIModelTestRun.STATUS_RUNNING)

    def test_cancel_from_pause(self):
        """Отмена работает из паузы: job и БД сразу CANCELLED."""
        from ai import arm_runner
        from ai.models import AIModelTestRun
        arm_runner.pause_arm_run(self.run_id)
        self.assertTrue(arm_runner.cancel_arm_run(self.run_id))
        self.assertEqual(self.job["status"], "cancelled")
        self.test_run.refresh_from_db()
        self.assertEqual(self.test_run.status, AIModelTestRun.STATUS_CANCELLED)
        self.assertEqual(self.test_run.error_message, "Прервано пользователем")

    def test_wait_resume_breaks_on_cancel(self):
        from ai import arm_runner
        with patch("ai.arm_runner.time.sleep"):
            arm_runner.pause_arm_run(self.run_id)
            arm_runner.cancel_arm_run(self.run_id)
            self.assertFalse(arm_runner._wait_resume(self.run_id))

    def test_rerun_guard_refuses_paused(self):
        from ai import arm_runner
        from ai.models import AIModelTestRun
        AIModelTestRun.objects.filter(pk=self.test_run.pk).update(
            status=AIModelTestRun.STATUS_PAUSED,
        )
        run_id, message, dropped = arm_runner.start_batch_rerun_pairs(
            self.run_id, 1, "DLSID-1"
        )
        self.assertIsNone(run_id)
        self.assertIn("паузе", message)
        self.assertEqual(dropped, [])

    def test_batch_snapshot_reports_paused(self):
        from ai import arm_runner
        from ai.models import AIModelTestRun
        AIModelTestRun.objects.filter(pk=self.test_run.pk).update(
            status=AIModelTestRun.STATUS_PAUSED,
        )
        arm_runner._jobs.pop(self.run_id, None)  # чистый DB-снапшот
        snapshot = arm_runner.get_arm_run_snapshot(self.run_id, light_results=True)
        self.assertEqual(snapshot["status"], "paused")

    def test_orphan_detector_heals_paused_run(self):
        """Осиротевший (после рестарта) PAUSED-прогон → FAILED
        («Процесс потерян»), а не вечная «пауза» для фронтенда."""
        from ai import arm_runner
        from ai.models import AIModelTestRun
        AIModelTestRun.objects.filter(pk=self.test_run.pk).update(
            status=AIModelTestRun.STATUS_PAUSED,
        )
        arm_runner._jobs.pop(self.run_id, None)
        run_obj = AIModelTestRun.objects.get(pk=self.test_run.pk)
        arm_runner._mark_orphaned_run_failed(run_obj)
        run_obj.refresh_from_db()
        self.assertEqual(run_obj.status, AIModelTestRun.STATUS_FAILED)
        self.assertEqual(run_obj.error_message, "Процесс потерян (перезапуск сервера)")


class ArmPauseIntegrationTests(TransactionTestCase):
    """Интеграционная проверка паузы в цикле модели: in-flight пара доживает,
    следующая не стартует; отмена из паузы обрывает цикл."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(username="pauser", password="x")
        self.lang = ProgrammingLanguage.objects.create(language_name="Pascal")
        self.topic = Topic.objects.create(topic_name_ru="Паузные", programming_language=self.lang)
        self.t1 = Task.objects.create(
            node_id=9301, task_id=9401, name="A", statement="x",
            topic=self.topic, programming_language=self.lang, file_extension=".pas",
        )
        self.t2 = Task.objects.create(
            node_id=9302, task_id=9402, name="B", statement="y",
            topic=self.topic, programming_language=self.lang, file_extension=".pas",
        )

    @staticmethod
    def _dl_ok():
        def dl_mock(sid, node_id, code, ext, **kw):
            return {
                "verdict": "solved", "comment": "Все тесты успешно пройдены",
                "submit_error": "", "queue_id": 21, "code_sent": code,
            }
        return dl_mock

    def _seed_job(self, run_id):
        from ai import arm_runner
        now_ts = time.time()
        job = {
            "run_id": run_id, "user_id": self.user.id, "run_type": "batch",
            "status": "running", "error_message": "",
            "cancel_requested": False, "cancelled": False,
            "pause_requested": False, "paused": False,
            "total_models": 1, "total_pairs": 2, "completed_pairs": 0,
            "completed_models": 0, "current_model_key": "ModelA",
            "current_model_title": "Model A",
            "current_task_node_id": "", "current_task_name": "",
            "model_progress": [], "results": [], "report": None,
            "created_at_ts": now_ts, "updated_at_ts": now_ts,
        }
        arm_runner._jobs[run_id] = job
        return job

    @staticmethod
    def _handler(calls):
        import asyncio

        async def handler(messages, conv_id):
            node_id = int(conv_id.rsplit("-", 1)[1])
            calls.append(node_id)
            # Генерация подольше пары — watcher успевает поставить паузу
            # ПОКА идёт пара 1 (in-flight доживает, проверка паузы — на
            # границе пар).
            await asyncio.sleep(0.4)
            return ("program p; begin writeln(1); end.", 7)

        return handler

    def _watcher(self, run_id, action, calls):
        """Отдельный поток: видит первый вызов модели → ставит паузу (sync-ORM
        возможен только вне async-контекста — из вьюхи это работает, из
        async-хендлера теста нет); видит paused → resume/cancel."""
        import threading
        from ai import arm_runner

        def tick():
            for _ in range(200):
                if len(calls) >= 1:
                    arm_runner.pause_arm_run(run_id)
                    break
                time.sleep(0.02)
            for _ in range(200):
                job = arm_runner._jobs.get(run_id)
                if job and job.get("paused"):
                    if action == "resume":
                        arm_runner.resume_arm_run(run_id)
                    else:
                        arm_runner.cancel_arm_run(run_id)
                    return
                time.sleep(0.05)

        thread = threading.Thread(target=tick, daemon=True)
        thread.start()
        return thread

    def test_pause_holds_next_pair_until_resume(self):
        """Пара на паузе завершена, вторая не стартует до resume; после снятия
        прогон доводится до completed со ВСЕМИ парами."""
        from ai import arm_runner
        from ai.models import AIModelTestResult, AIModelTestRun

        calls = []
        run_id = "parpause-" + uuid.uuid4().hex[:8]
        self._seed_job(run_id)
        watcher = self._watcher(run_id, "resume", calls)
        try:
            with patch("ai.arm_runner._test_solution_on_dl", self._dl_ok()):
                arm_runner._run_batch_job_worker(
                    run_id, [self.t1.node_id, self.t2.node_id],
                    [{"key": "ModelA", "title": "Model A",
                      "handler": self._handler(calls)}],
                    self.user.id, "DLSID-1", ui_language="Русский", dl_test=True,
                )
        finally:
            arm_runner._jobs.pop(run_id, None)
        watcher.join(timeout=2)

        run = AIModelTestRun.objects.get(run_id=run_id)
        self.assertEqual(run.status, AIModelTestRun.STATUS_COMPLETED)
        self.assertEqual(
            sorted(r.task.node_id for r in AIModelTestResult.objects.filter(run=run)),
            [self.t1.node_id, self.t2.node_id],
        )
        # Пауза была запрошена внутри пары 1 и снята ДО пары 2.
        self.assertEqual(calls, [self.t1.node_id, self.t2.node_id])

    def test_cancel_from_pause_interrupts_loop(self):
        """Отмена из паузы: цикл модели прерывается, завершённая пара записана,
        прогон остаётся CANCELLED (finalize не перетирает)."""
        from ai import arm_runner
        from ai.models import AIModelTestResult, AIModelTestRun

        calls = []
        run_id = "parcancel-" + uuid.uuid4().hex[:8]
        self._seed_job(run_id)
        watcher = self._watcher(run_id, "cancel", calls)
        try:
            with patch("ai.arm_runner._test_solution_on_dl", self._dl_ok()):
                arm_runner._run_batch_job_worker(
                    run_id, [self.t1.node_id, self.t2.node_id],
                    [{"key": "ModelA", "title": "Model A",
                      "handler": self._handler(calls)}],
                    self.user.id, "DLSID-1", ui_language="Русский", dl_test=True,
                )
        finally:
            arm_runner._jobs.pop(run_id, None)
        watcher.join(timeout=2)

        run = AIModelTestRun.objects.get(run_id=run_id)
        self.assertEqual(run.status, AIModelTestRun.STATUS_CANCELLED)
        # Вторая пара не решалась.
        self.assertEqual(calls, [self.t1.node_id])
        # In-flight пара отменилась по контракту отмены (после LLM-вызова
        # пара отбрасывается) — решённых пар 0, строк максимум одна
        # (failed, гонка с моментом отмены).
        self.assertEqual(
            AIModelTestResult.objects.filter(run=run, verdict="solved").count(), 0
        )
        self.assertLessEqual(
            AIModelTestResult.objects.filter(run=run).count(), 1
        )


class ArmPauseViewTests(TestCase):
    """POST pause/resume на /arm/solve/ — JSON-контракт как у rerun-pairs."""

    def setUp(self):
        from ai.models import AIModelTestRun
        self.factory = RequestFactory()
        self.superuser = get_user_model().objects.create_superuser(
            username="pause_view_admin", password="x", email="pv@t.com",
        )
        self.run_id = "f" * 32
        self.test_run = AIModelTestRun.objects.create(
            run_id=self.run_id,
            run_type=AIModelTestRun.RUN_TYPE_BATCH,
            status=AIModelTestRun.STATUS_RUNNING,
        )
        self.job = {
            "run_id": self.run_id, "user_id": self.superuser.id,
            "run_type": "batch", "status": "running", "error_message": "",
            "cancel_requested": False, "cancelled": False,
            "pause_requested": False, "paused": False,
            "total_models": 1, "total_pairs": 1, "completed_pairs": 0,
            "completed_models": 0, "current_model_key": "FakeModel",
            "current_model_title": "FakeModel",
            "current_task_node_id": "", "current_task_name": "",
            "model_progress": [], "results": [], "report": None,
            "created_at_ts": time.time(), "updated_at_ts": time.time(),
        }

    def _inject(self):
        from ai import arm_runner
        self.addCleanup(arm_runner._jobs.pop, self.run_id, None)
        arm_runner._jobs[self.run_id] = self.job

    def _post(self, url, body=None):
        from ai.admin import arm as arm_admin
        path = "/ai/admin" + url
        request = self.factory.post(path, data=body or {},
                                    content_type="application/json")
        request.user = self.superuser
        request.session = {}
        if url.startswith("/arm/solve/pause"):
            return arm_admin.admin_arm_solve_pause_view(request)
        return arm_admin.admin_arm_solve_resume_view(request)

    def test_get_not_allowed(self):
        from ai.admin import arm as arm_admin
        request = self.factory.get("/ai/admin/arm/solve/pause/")
        request.user = self.superuser
        request.session = {}
        self.assertEqual(arm_admin.admin_arm_solve_pause_view(request).status_code, 405)
        self.assertEqual(arm_admin.admin_arm_solve_resume_view(request).status_code, 405)

    def test_run_id_required(self):
        response = self._post("/arm/solve/pause/")
        self.assertEqual(response.status_code, 400)
        self.assertIn("run_id", json.loads(response.content)["message"])

    def test_unknown_run_404(self):
        request = self.factory.post(
            "/ai/admin/arm/solve/pause/",
            data=json.dumps({"run_id": "0" * 32}), content_type="application/json",
        )
        request.user = self.superuser
        request.session = {}
        from ai.admin.arm import admin_arm_solve_pause_view
        response = admin_arm_solve_pause_view(request)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            json.loads(response.content)["message"], "Процесс не найден или уже завершен"
        )

    def test_pause_returns_paused_snapshot(self):
        from ai.models import AIModelTestRun
        self._inject()
        response = self._post("/arm/solve/pause/", {"run_id": self.run_id})
        data = json.loads(response.content)
        self.assertTrue(data["ok"])
        self.assertIn("приостановлен", data["message"])
        self.assertEqual(data["run"]["status"], "paused")
        self.test_run.refresh_from_db()
        self.assertEqual(self.test_run.status, AIModelTestRun.STATUS_PAUSED)

    def test_resume_returns_running_snapshot(self):
        from ai import arm_runner
        from ai.models import AIModelTestRun
        self._inject()
        arm_runner.pause_arm_run(self.run_id)
        response = self._post("/arm/solve/resume/", {"run_id": self.run_id})
        data = json.loads(response.content)
        self.assertTrue(data["ok"])
        self.assertEqual(data["run"]["status"], "running")
        self.test_run.refresh_from_db()
        self.assertEqual(self.test_run.status, AIModelTestRun.STATUS_RUNNING)


# ===================================================================
# Журнал запросов: 5 фильтров и 11 колонок (по ТЗ) + «Добавить запись»
# ===================================================================

class RequestLogFiltersUiTests(TestCase):
    """Форма журнала оставляет только фильтры Режим/Модель/ФИО/С/По
    (q/ID/Статус/Задача убраны); таблица — 11 колонок «Кто отправлял → ★»;
    localStorage-персистентность знает только оставшиеся поля. Кнопок
    «Фильтровать»/«Обновить таблицу»/«Добавить запись» НЕТ (динамический
    фильтр применяет всё сам); под фильтрами — «Дневной отчёт» → «Сбросить»."""

    def test_filter_form_has_only_remaining_fields(self):
        from django.template.loader import get_template
        src = get_template("admin/ai/request_logs.html").template.source
        for fragment in (
            "id_mode", "id_model", "id_user", "id_date_from", "id_date_to",
        ):
            self.assertIn(fragment, src)
        for removed in ("id_q", "id_record_id", "id_status", "id_task"):
            self.assertNotIn(removed, src)
        # Подпись фильтра пользователя.
        self.assertIn('label for="id_user">ФИО<', src)
        # ФИО — ПЕРВЫЙ фильтр (просьба владельца): раньше любого другого поля.
        self.assertLess(src.index('id="id_user"'), src.index('id="id_mode"'))

    def test_buttons_under_filters(self):
        """Ровно две кнопки под фильтрами: «Дневной отчёт» ПЕРЕД «Сбросить»;
        старых кнопок нет — динамический фильтр применяет всё сам."""
        from django.template.loader import get_template
        src = get_template("admin/ai/request_logs.html").template.source
        self.assertIn("ai-logs-actions", src)
        actions = src.index("ai-logs-actions")
        self.assertLess(actions, src.index("Дневной отчёт →"))
        # ?reset=1 ищем ПОСЛЕ начала .ai-logs-actions: комментарий над рядком
        # тоже упоминает «?reset=1» (и он раньше самой ссылки «Сбросить»).
        self.assertLess(src.index("Дневной отчёт →"), src.index("?reset=1", actions))
        # Убраны: submit «Фильтровать», AJAX «Обновить таблицу», «Добавить
        # запись» (генератор живёт в меню «Тестовые данные») и флаг show_test_data_link.
        self.assertNotIn("Фильтровать", src)
        self.assertNotIn("ai-logs-table-update-button", src)
        self.assertNotIn("Добавить запись", src)
        self.assertNotIn("show_test_data_link", src)

    def test_index_js_dynamic_filters_no_tdz(self):
        """TDZ-фикс: `const form` объявлен РАНЬШЕ первого вызова
        restoreSavedFilters (иначе ReferenceError гасил весь
        DOMContentLoaded — подробный отчёт/аккордеон/авто-фильтры мертвы);
        авто-применение (input debounce + change) на месте; мёртвый обработчик
        «Обновить таблицу» удалён."""
        from django.conf import settings
        path = os.path.join(
            settings.BASE_DIR, "static", "admin", "ai", "request_logs", "index.js",
        )
        with open(path, encoding="utf-8") as fh:
            js = fh.read()
        self.assertLess(js.index("const form = document.querySelector('.ai-logs-filters');"),
                        js.index("(function restoreSavedFilters()"))
        self.assertIn("addEventListener('input'", js)
        self.assertIn("addEventListener('change'", js)
        self.assertNotIn("ai-logs-table-update-button", js)

    def test_response_text_lazy_in_table(self):
        """Ответ модели не вшивается в таблицу (он делал AJAX-обновление
        мегабайтным): развёртка строки обычного запроса
        (admin/ai/_ai_log_detail.html) ставит .ai-log-response-body с
        data-response-url → detail-json, а index.js подтягивает текст по
        первому раскрытию строки (loadLazyResponse)."""
        from django.template.loader import get_template
        from django.conf import settings
        src = get_template("admin/ai/_ai_log_detail.html").template.source
        self.assertNotIn("{{ log.response_text", src)
        self.assertIn('class="ai-log-response-body"', src)
        self.assertIn("ai_request_log_detail_json", src)
        # В таблице журнала самого текста ответа тоже нет.
        table = get_template("admin/ai/request_logs_table.html").template.source
        self.assertNotIn("{{ log.response_text", table)
        path = os.path.join(
            settings.BASE_DIR, "static", "admin", "ai", "request_logs", "index.js",
        )
        with open(path, encoding="utf-8") as fh:
            js = fh.read()
        self.assertIn("function loadLazyResponse", js)
        self.assertIn("data-response-url", js)
        self.assertIn("loadLazyResponse(detailRow);", js)

    def test_table_columns_order(self):
        """Одна шапка журнала — 18 колонок (admin/ai/_ai_journal_head.html):
        ★, Кто отправлял, Отправлен, Получен, Режим, Название прогона, Курс,
        Расширение, Язык, Тема, Промпт, Модель, Решено, Не решено, Всего пар,
        Статус, Время, (Детали). ID и Источник вынесены в развёртку."""
        from django.template.loader import get_template
        head = get_template("admin/ai/_ai_journal_head.html").template.source
        order = [
            "<th>Кто отправлял</th>",
            "<th>Отправлен</th>",
            "<th>Получен</th>",
            "<th>Режим</th>",
            "<th>Название прогона</th>",
            "<th>Курс</th>",
            "<th>Расширение</th>",
            "<th>Язык</th>",
            "<th>Тема</th>",
            "<th>Промпт</th>",
            "<th>Модель</th>",
            "<th>Решено</th>",
            "<th>Не решено</th>",
            "<th>Всего пар</th>",
            "<th>Статус</th>",
        ]
        positions = [head.index(item) for item in order]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(head.count("<th"), 18)
        self.assertNotIn("<th>ID</th>", head)
        self.assertNotIn("<th>Источник</th>", head)
        # Развёртка строки обычного запроса — на 18 колонок.
        src = get_template("admin/ai/_ai_log_detail.html").template.source
        self.assertIn('colspan="18"', src)
        detail = get_template("admin/ai/_ai_batch_detail.html").template.source
        self.assertIn('colspan="18"', detail)

    def test_index_js_filter_fields(self):
        from django.conf import settings
        path = os.path.join(
            settings.BASE_DIR, "static", "admin", "ai", "request_logs", "index.js",
        )
        with open(path, encoding="utf-8") as fh:
            js = fh.read()
        self.assertIn(
            "const FILTER_FIELDS = ['mode', 'model', 'user', 'date_from', 'date_to'];",
            js,
        )


# ===================================================================
# «Промпты по умолчанию»: суперюзер + prompt_developer, гость — нет
# ===================================================================

class PromptDefaultsAccessTests(_AdminViewRequestMixin, TestCase):
    """«Промпты по умолчанию» (/ai/admin/prompt-defaults/) открыты супер-
    юзеру и любому допущенному в админку (page.prompt_defaults — baseline,
    ТЗ 2026-10-19); юзер без допуска (не staff, без page.admin) — 403;
    в гостевом режиме пункт навигации скрыт."""

    def setUp(self):
        self.factory = RequestFactory()
        self.user_model = get_user_model()

    def _get(self, user):
        from unittest.mock import patch
        from ai.admin.prompt_defaults import admin_prompt_defaults_view
        request = self._admin_request(user, path="/ai/admin/prompt-defaults/")
        with patch(
            "ai.admin.site.get_external_user_id_from_request", return_value="12345",
        ):
            return admin_prompt_defaults_view(request)

    def test_superuser_allowed(self):
        su = self.user_model.objects.create_user(
            username="pd-super", password="x", is_superuser=True, is_staff=True,
        )
        response = self._get(su)
        self.assertEqual(response.status_code, 200)

    def test_prompt_developer_allowed(self):
        dev = self.user_model.objects.create_user(username="pd-dev", password="x")
        make_prompt_developer(dev)
        response = self._get(dev)
        self.assertEqual(response.status_code, 200)

    def test_plain_staff_forbidden(self):
        """Staff с допуском в админку — 200 (baseline); юзер вовсе без
        допуска (не staff, нет page.admin) — 403."""
        staff = self.user_model.objects.create_user(
            username="pd-staff", password="x", is_staff=True,
        )
        response = self._get(staff)
        self.assertEqual(response.status_code, 200)

        outsider = self.user_model.objects.create_user(
            username="pd-outsider", password="x",
        )
        response = self._get(outsider)
        self.assertEqual(response.status_code, 403)

    def _nav_tools(self, user, view_as_group=None):
        from unittest.mock import patch
        from ai.admin.site import ai_admin_site
        request = self._admin_request(user, path="/ai/admin/")
        if view_as_group is not None:
            self._enter_view_as(request, view_as_group)
        with patch(
            "ai.admin.site.get_external_user_id_from_request", return_value="12345",
        ):
            ctx = ai_admin_site.each_context(request)
        return {
            m["object_name"]
            for app in ctx["available_apps"]
            if app["app_label"].startswith("ai-tools")
            for m in app["models"]
        }

    def test_prompt_defaults_nav_for_developer(self):
        dev = self.user_model.objects.create_user(username="pd-nav", password="x")
        make_prompt_developer(dev)
        self.assertIn("AiArmPromptDefaults", self._nav_tools(dev))

    def test_prompt_defaults_nav_honest_in_view_as(self):
        """baseline: у группы с page.admin «Промпты по умолчанию» остаются
        видны; без page.admin честные ключи пусты — пункт скрыт."""
        su = self.user_model.objects.create_user(
            username="pd-nav-viewas", password="x", is_superuser=True, is_staff=True,
        )
        self.assertIn("AiArmPromptDefaults", self._nav_tools(su))
        with_admin = self._view_as_group("Эмуля-with-admin", ["page.admin"])
        self.assertIn(
            "AiArmPromptDefaults",
            self._nav_tools(su, view_as_group=with_admin),
            "page.prompt_defaults — baseline, page.admin сохраняет его в эмуляции",
        )
        bare = self._view_as_group("Эмуля-bare-nav", [])
        self.assertNotIn(
            "AiArmPromptDefaults",
            self._nav_tools(su, view_as_group=bare),
        )

    def test_test_data_nav_stays_without_context_flag(self):
        """Генератор тестовых записей живёт ТОЛЬКО в меню «Тестовые данные»;
        флаг show_test_data_link для журнала удалён вместе с кнопкой
        «Добавить запись» (контекст больше его не несёт)."""
        su = self.user_model.objects.create_user(
            username="td-link", password="x", is_superuser=True, is_staff=True,
        )
        from unittest.mock import patch
        from ai.admin.site import ai_admin_site
        request = self._admin_request(su, path="/ai/admin/")
        with patch(
            "ai.admin.site.get_external_user_id_from_request", return_value="12345",
        ):
            ctx = ai_admin_site.each_context(request)
        self.assertNotIn("show_test_data_link", ctx)
        self.assertIn("AiTestData", self._nav_tools(su))
        bare = self._view_as_group("Эмуля-bare-td", [])
        self.assertNotIn("AiTestData", self._nav_tools(su, view_as_group=bare))


# ===================================================================
# Фикс 400: авто-перевод языковых табов формы промпта
# ===================================================================

class PromptTranslateTabsArityTests(SimpleTestCase):
    """translateField(targetKey, sourceText) зовётся РОВНО двумя
    аргументами (target, текст-источник). Раньше вызов шёл тремя
    (kind, key, source.text): «kind» попадал в targetKey → сервер получал
    target="name"/"text" и всегда отвечал 400 «Неизвестный целевой язык»
    — автоперевод RU→EN/FR никогда не срабатывал."""

    def _source(self):
        from pathlib import Path
        path = (
            Path(__file__).resolve().parent.parent
            / "static" / "admin" / "js" / "prompt_translate_tabs.js"
        )
        return path.read_text(encoding="utf-8")

    def test_translate_field_called_with_two_args(self):
        src = self._source()
        self.assertIn("translateField(key, source.text)", src)
        self.assertNotIn("translateField(kind, key", src)

    def test_batch_results_template_renders_dl_link_and_interim_note(self):
        """Карточка результата: r.dl_url → ссылка в DL (escapeHtml НЕ нужен —
        link.textContent); в renderReport есть ветка плашки interim.

        Логика карточки вынесена в отдельный JS-файл
        (static/admin/ai/batch_results/index.js), а шаблон — чистый
        контейнер (общий partial у четырёх страниц)."""
        from pathlib import Path
        path = (
            Path(__file__).resolve().parent.parent / "static" / "admin" / "ai"
            / "batch_results" / "index.js"
        )
        src = path.read_text(encoding="utf-8")
        self.assertIn("r.dl_url", src)
        self.assertIn("link.textContent", src)
        self.assertNotIn("innerHTML = r.", src)  # сырой контент модели не пробрасывается
        self.assertIn("setInterimNote", src)
        self.assertIn("report.interim", src)


class ArmDlUrlAndInterimReportTests(TestCase):
    """dl_url на result-item'ах (сервер: dl_task_url из task_registry) и
    живой промежуточный отчёт выполняющегося прогона (report.interim)."""

    def setUp(self):
        from ai.models import AIModelTestResult, AIModelTestRun, ProgrammingLanguage, Task, Topic

        self.lang = ProgrammingLanguage.objects.create(language_name="Pascal")
        self.topic = Topic.objects.create(topic_name_ru="Линейные", programming_language=self.lang)
        self.t1 = Task.objects.create(
            node_id=6101, task_id=7101, name="Задача А", statement="x",
            topic=self.topic, programming_language=self.lang, file_extension=".pas",
        )
        self.run_id = "a" * 32
        self.test_run = AIModelTestRun.objects.create(
            run_id=self.run_id,
            run_type=AIModelTestRun.RUN_TYPE_BATCH,
            status=AIModelTestRun.STATUS_COMPLETED,
            course_id=1450,
        )
        self.result = AIModelTestResult.objects.create(
            run=self.test_run, task=self.t1,
            model_key="M1", model_title="Model One",
            status="ok", verdict="solved",
            duration_seconds=2.0, tokens=11,
            raw_response="полный ответ",
        )

    def _make_job(self, status="running", run_type="batch"):
        return {
            "run_id": self.run_id, "run_type": run_type, "status": status,
            "total_pairs": 2, "completed_pairs": 1,
            "results": [{
                "result_id": self.result.id, "task_id": self.t1.id,
                "task_node_id": 6101, "task_name": "Задача А",
                "dl_url": "https://dl.gsu.by/task.jsp?nid=6101&cid=1450",
                "model_key": "M1", "model_title": "Model One",
                "duration": 2.0, "tokens": 11,
                "short_response": "краткий", "status": "ok",
                "verdict": "solved", "raw_response": "полный ответ",
                "code": "begin end.", "dl_comment": "ок", "dl_error": "",
                "dl_queue_id": 0, "file_extension": ".pas",
                "topic_name": "Линейные", "prog_lang_name": "Pascal",
            }],
            "report": None,
            "run_params": {"node_ids": [6101]},
            "created_at_ts": 1000.0, "updated_at_ts": 1010.0,
        }

    def test_db_result_items_carry_dl_url(self):
        """И light-, и full-ветка _batch_results_from_db отдают dl_task_url
        с course_id прогона (nid первым, cid вторым)."""
        from ai.arm_runner import _batch_results_from_db

        for light in (True, False):
            item = _batch_results_from_db(self.test_run, light=light)[0]
            self.assertEqual(
                item["dl_url"],
                "https://dl.gsu.by/task.jsp?nid=6101&cid=1450",
            )

    def test_db_result_without_task_has_no_dl_url(self):
        """Легаси-результаты без задачи: dl_url None, падения нет."""
        from ai.arm_runner import _batch_results_from_db

        self.result.task = None
        self.result.save(update_fields=["task"])
        try:
            item = _batch_results_from_db(self.test_run, light=True)[0]
            self.assertIsNone(item["dl_url"])
            full = _batch_results_from_db(self.test_run)[0]
            self.assertIsNone(full["dl_url"])
        finally:
            self.result.task = self.t1
            self.result.save(update_fields=["task"])

    def test_running_live_snapshot_builds_interim_report(self):
        """Выполняющийся прогона: снапшот отдаёт отчёт по уже завершённым
        парам c флагом interim и run_meta.running=True; job не мутируется."""
        from ai import arm_runner

        job = self._make_job(status="running")
        arm_runner._jobs[self.run_id] = job
        try:
            for snapshot in (
                arm_runner.get_arm_run_snapshot(self.run_id, light_results=True),
                arm_runner.get_arm_run_snapshot(self.run_id),
            ):
                self.assertIsNotNone(snapshot["report"])
                self.assertTrue(snapshot["report"]["interim"])
                self.assertEqual(snapshot["report"]["total_pairs"], 1)
                self.assertEqual(snapshot["report"]["solved"], 1)
                self.assertTrue(snapshot["report"]["run_meta"]["running"])
            # job сам не тронут: финальный отчёт по-прежнему не построен.
            self.assertIsNone(job["report"])
        finally:
            arm_runner._jobs.pop(self.run_id, None)

    def test_completed_job_gets_no_interim_flag(self):
        """Завершённый живой job: отчёт пришёл из финала (interim нет);
        interim-путь не подмешивается."""
        from ai import arm_runner

        job = self._make_job(status="completed")
        job["report"] = {"total_pairs": 2, "solved": 1, "failed": 1}
        arm_runner._jobs[self.run_id] = job
        try:
            snapshot = arm_runner.get_arm_run_snapshot(self.run_id, light_results=True)
            self.assertIs(snapshot["report"], job["report"])
            self.assertNotIn("interim", snapshot["report"])
        finally:
            arm_runner._jobs.pop(self.run_id, None)

    def test_interim_report_skipped_for_single_runs_and_empty_results(self):
        """_interim_live_report: single-прогоны и пустые results → None."""
        from ai import arm_runner

        self.assertIsNone(arm_runner._interim_live_report(self._make_job(run_type="single")))
        job = self._make_job()
        job["results"] = []
        self.assertIsNone(arm_runner._interim_live_report(job))

    def test_xlsx_running_note_lands_in_first_row(self):
        """build_arm_results_xlsx(note=…) пишет пометку в шапку листа
        «Сводка»; без note первая строка — заголовок матрицы (как раньше)."""
        from io import BytesIO

        from openpyxl import load_workbook

        from ai.admin.export import build_arm_results_xlsx

        results = [{
            "task_node_id": 6101, "task_name": "Задача А",
            "model_key": "M1", "model_title": "Model One",
            "verdict": "solved", "duration": 2.0, "tokens": 11, "status": "ok",
        }]
        note = "ВНИМАНИЕ: работа ещё выполняется — в файле завершённые пары (1 из 2)."
        ws = load_workbook(BytesIO(build_arm_results_xlsx(results, note=note)))["Сводка"]
        self.assertEqual(ws.cell(row=1, column=1).value, note)
        # Без note — ровно прежний файл: заголовок матрицы в первой строке.
        ws_plain = load_workbook(BytesIO(build_arm_results_xlsx(results)))["Сводка"]
        self.assertEqual(ws_plain.cell(row=1, column=1).value, "Node ID")

    def test_journal_snapshot_running_marks_interim(self):
        """Детали журнала выполняющегося прогона: отчёт по текущим строкам БД
        помечен interim (та же плашка, что у живого снапшота)."""
        import json as _json

        from ai.admin.request_logs import _build_batch_log_snapshot
        from ai.models import AIRequestLog

        self.test_run.status = "running"
        self.test_run.started_at = timezone.now() - timedelta(seconds=30)
        self.test_run.finished_at = None
        self.test_run.save(update_fields=["status", "started_at", "finished_at"])
        batch_log = AIRequestLog.objects.create(
            user=None, source="arm", mode="batch_solve",
            message=f"Batch solve run {self.run_id}",
            sent_at=timezone.now(),
        )
        try:
            snapshot = _build_batch_log_snapshot(batch_log)
            self.assertIsNotNone(snapshot)
            self.assertEqual(snapshot["run_status"], "running")
            self.assertEqual(snapshot["run_completed_pairs"], 1)
            self.assertTrue(snapshot["report"]["interim"])
            self.assertEqual(snapshot["report"]["total_pairs"], 1)
            self.assertTrue(snapshot["report"]["run_meta"]["running"])
        finally:
            self.test_run.status = "completed"
            self.test_run.started_at = timezone.now() - timedelta(seconds=90)
            self.test_run.finished_at = timezone.now()
            self.test_run.save(update_fields=["status", "started_at", "finished_at"])
            batch_log.delete()


# ===================================================================
# 2026-10 пакет: словарь «вуз/школа + группа», генератор тестовых данных,
# скоуп-статистика моделей (API, подпись, вычет), страницы журнала/настроек
# ===================================================================

class EducationDirectoryTests(TestCase):
    """Справочник DLEducationName (id→название, ручное ведение) и единственный
    читатель — education_directory (DRY)."""

    def test_unique_kind_key(self):
        from django.db import IntegrityError

        from ai.models import DLEducationName
        DLEducationName.objects.create(kind="school", key="S-1", title="ГГУ")
        with self.assertRaises(IntegrityError):
            DLEducationName.objects.create(kind="school", key="S-1", title="Дубль")

    def test_education_title_maps(self):
        from ai.models import DLEducationName
        from ai.services.education_directory import education_title_maps
        DLEducationName.objects.create(kind="school", key="S-1", title="ГГУ")
        DLEducationName.objects.create(kind="group", key="G-1", title="ИТП-31", school="S-1")
        DLEducationName.objects.create(kind="group", key="G-2", title="КЛ-9А")
        schools, groups, groups_school = education_title_maps()
        self.assertEqual(schools, {"S-1": "ГГУ"})
        self.assertEqual(groups, {"G-1": "ИТП-31", "G-2": "КЛ-9А"})
        # Привязка группы → заведение: только у group-строк с заполненным
        # school (КЛ-9А без привязки в словарь не попадает).
        self.assertEqual(groups_school, {"G-1": "S-1"})

    def test_school_display_fallbacks(self):
        """Цепочка: справочник → сырой id → «kind №no» → «—» (и группа)."""
        from ai.models import ExternalDLAccount
        from ai.services.education_directory import group_display, school_display
        schools, groups = {"S-1": "ГГУ"}, {"G-1": "ИТП-31"}
        self.assertEqual(school_display(schools, None), "—")
        acc = SimpleNamespace(
            education_school_id="S-1", education_school_kind="", education_school_no="",
            education_group_mask_id="G-1",
        )
        self.assertEqual(school_display(schools, acc), "ГГУ")
        self.assertEqual(group_display(groups, acc), "ИТП-31")
        acc_raw = SimpleNamespace(
            education_school_id="S-9", education_school_kind="ФГБОУ",
            education_school_no="5", education_group_mask_id="G-7",
        )
        # unmapped schoolId → сырой id; unmapped маска группы → сырая маска.
        self.assertEqual(school_display(schools, acc_raw), "S-9")
        self.assertEqual(group_display(groups, acc_raw), "G-7")
        acc_noid = SimpleNamespace(
            education_school_id="", education_school_kind="ФГБОУ",
            education_school_no="5", education_group_mask_id="",
        )
        self.assertEqual(school_display(schools, acc_noid), "ФГБОУ №5")
        acc_empty = SimpleNamespace(
            education_school_id="", education_school_kind="",
            education_school_no="", education_group_mask_id="",
        )
        self.assertEqual(school_display(schools, acc_empty), "—")
        self.assertEqual(group_display(groups, acc_empty), "—")

    def test_school_from_group_fallback(self):
        """school_title_for: школьные поля студента пусты (school_display → «—»)
        → берём заведение его группы из groups_school; сырой школьный id и
        «kind №no» честнее привязки группы и НЕ перекрываются."""
        from ai.services.education_directory import school_title_for
        schools = {"S-1": "ГГУ"}
        groups_school = {"G-1": "S-1"}
        acc = SimpleNamespace(
            education_school_id="", education_school_kind="", education_school_no="",
            education_group_mask_id="G-1",
        )
        self.assertEqual(school_title_for(schools, groups_school, acc), "ГГУ")
        # Сырой школьный id не перекрывается привязкой группы.
        acc_raw = SimpleNamespace(
            education_school_id="S-9", education_school_kind="", education_school_no="",
            education_group_mask_id="G-1",
        )
        self.assertEqual(school_title_for(schools, groups_school, acc_raw), "S-9")
        # «kind №no» тоже честный — не перекрывается.
        acc_kind = SimpleNamespace(
            education_school_id="", education_school_kind="ФГБОУ", education_school_no="5",
            education_group_mask_id="G-1",
        )
        self.assertEqual(school_title_for(schools, groups_school, acc_kind), "ФГБОУ №5")
        # Группа без привязки → «—»; аккаунта нет → «—».
        acc_nolink = SimpleNamespace(
            education_school_id="", education_school_kind="", education_school_no="",
            education_group_mask_id="G-7",
        )
        self.assertEqual(school_title_for(schools, groups_school, acc_nolink), "—")
        self.assertEqual(school_title_for(schools, groups_school, None), "—")

    def test_group_and_school_info_keys(self):
        """group_info / school_info отдают (display, key, has_title): сырой ID
        из DL для фильтра и честный признак «названия нет»."""
        from ai.services.education_directory import group_info, school_info
        schools = {"S-1": "ГГУ"}
        groups = {"G-1": "ИТП-31"}
        groups_school = {"G-1": "S-1"}
        named = SimpleNamespace(
            education_school_id="S-1", education_school_kind="", education_school_no="",
            education_group_mask_id="G-1",
        )
        self.assertEqual(group_info(groups, named), ("ИТП-31", "G-1", True))
        self.assertEqual(school_info(schools, groups_school, named), ("ГГУ", "S-1", True))
        # Ключа нет в справочнике → display = сырой ID, has_title = False.
        raw = SimpleNamespace(
            education_school_id="S-9", education_school_kind="", education_school_no="",
            education_group_mask_id="G-7",
        )
        self.assertEqual(group_info(groups, raw), ("G-7", "G-7", False))
        self.assertEqual(school_info(schools, groups_school, raw), ("S-9", "S-9", False))
        # Пустые поля → «—», без ключа.
        empty = SimpleNamespace(
            education_school_id="", education_school_kind="", education_school_no="",
            education_group_mask_id="",
        )
        self.assertEqual(group_info(groups, empty), ("—", "", False))
        self.assertEqual(school_info(schools, groups_school, empty), ("—", "", False))
        # Аккаунта нет → «—».
        self.assertEqual(group_info(groups, None), ("—", "", False))
        self.assertEqual(school_info(schools, groups_school, None), ("—", "", False))

    def test_dl_names_take_precedence_over_directory(self):
        """Название от DL (education.schoolName/groupName) ВАЖНЕЕ ручного
        справочника: DL — источник истины, справочник — лишь фолбэк."""
        from ai.services.education_directory import group_info, school_info
        schools = {"S-1": "Старое название школы"}
        groups = {"G-1": "Старая группа"}
        groups_school = {"G-1": "S-1"}
        acc = SimpleNamespace(
            education_school_id="S-1", education_school_kind="", education_school_no="",
            education_group_mask_id="G-1",
            education_school_name="ГГУ", education_group_name="ИТП-31",
        )
        self.assertEqual(group_info(groups, acc), ("ИТП-31", "G-1", True))
        self.assertEqual(school_info(schools, groups_school, acc), ("ГГУ", "S-1", True))
        # Название есть, а ID нет — название всё равно показываем (ключ пуст).
        acc_no_id = SimpleNamespace(
            education_school_id="", education_school_kind="", education_school_no="",
            education_group_mask_id="",
            education_school_name="ГГУ", education_group_name="ИТП-31",
        )
        self.assertEqual(group_info(groups, acc_no_id), ("ИТП-31", "", True))
        self.assertEqual(school_info(schools, groups_school, acc_no_id), ("ГГУ", "", True))

    def test_admin_school_field_and_column(self):
        """Форма справочника: поле school — select с вариантами school-строк
        («key — title», пустой вариант) и валидацией choices; колонка
        school_title — название привязки у group-строк, «—» у school-строк
        и без привязки."""
        from ai.admin.models import DLEducationNameAdmin, DLEducationNameAdminForm
        from ai.models import DLEducationName
        DLEducationName.objects.create(kind="school", key="S-1", title="ГГУ")
        DLEducationName.objects.create(kind="school", key="S-2", title="СГУ")
        group_row = DLEducationName.objects.create(
            kind="group", key="G-1", title="ИТП-31", school="S-1")
        plain_group = DLEducationName.objects.create(kind="group", key="G-2", title="КЛ-9А")

        form = DLEducationNameAdminForm(instance=group_row)
        self.assertEqual(
            list(form.fields["school"].choices),
            [("", "— не выбрано —"), ("S-1", "S-1 — ГГУ"), ("S-2", "S-2 — СГУ")],
        )
        # school="S-2" — валидно; school не из списка school-строк — ошибка.
        form = DLEducationNameAdminForm(
            data={"kind": "group", "key": "G-1", "title": "ИТП-31", "school": "S-2"},
            instance=group_row,
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.assertEqual(group_row.school, "S-2")
        bad = DLEducationNameAdminForm(
            data={"kind": "group", "key": "G-1", "title": "ИТП-31", "school": "G-1"})
        self.assertFalse(bad.is_valid())
        self.assertIn("school", bad.errors)

        # Колонка school_title: резолв через справочник, «—» у остальных.
        # Справочник крошечный и чанглист пагинирован — построчный запрос в
        # методе ок (комментарий в админке повторяет это).
        column = DLEducationNameAdmin(DLEducationName, None).school_title
        self.assertEqual(column(group_row), "СГУ")
        self.assertEqual(column(plain_group), "—")
        school_row = DLEducationName.objects.get(kind="school", key="S-1")
        self.assertEqual(column(school_row), "—")

    def test_registered_in_admin_site(self):
        from django.contrib import admin as django_admin

        from ai.admin.models import DLEducationNameAdmin
        from ai.admin.site import ai_admin_site
        from ai.models import DLEducationName
        model_admin = ai_admin_site._registry[DLEducationName]
        self.assertIs(type(model_admin), DLEducationNameAdmin)
        self.assertEqual(
            model_admin.list_display, ("kind", "key", "title", "school_title"))
        # Скорость чанглистов: facets «Show counts» никогда (CLAUDE.md).
        self.assertEqual(model_admin.show_facets, django_admin.ShowFacets.NEVER)


class TestDataGenerateTests(TestCase):
    """Генератор тестовых записей журнала: пишет РЕАЛЬНЫЕ строки (модели из
    реестра, существующие пользователи, существующие режимы и справочники БД)
    через LogWriter sync-ядро (раньше синхронный view получал корутину — записи
    не было вовсе)."""

    def setUp(self):
        self.factory = RequestFactory()
        self.superuser = get_user_model().objects.create_superuser(
            username="td-gen", password="x", email="g@t.com",
        )
        self.student = get_user_model().objects.create_user(
            username="td-student", password="x", first_name="Иван", last_name="Иванов",
        )
        self.account = ExternalDLAccount.objects.create(
            user=self.student, external_user_id="5501", external_login="td-student",
        )
        # По одному объекту каждого справочника — случайный выбор детерминирован.
        self.lang = ProgrammingLanguage.objects.create(language_name="Pascal")
        self.topic = Topic.objects.create(
            topic_name_ru="Массивы", programming_language=self.lang)
        self.prompt = Prompt.objects.create(
            prompt_name_ru="Мой промпт", prompt_text_ru="Реши по шагам",
            topic=self.topic, owner=self.student,
        )
        self.task = Task.objects.create(node_id=4321, name="Сумма массива", active=True)

    def _emit(self, user, mode):
        from ai.admin.test_data import test_data_emit_request_view
        request = self.factory.post(
            "/ai/admin/test_data/emit_request/", HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        request.user = user
        with patch("ai.admin.test_data.pick_mode", return_value=mode):
            return test_data_emit_request_view(request)

    def _last_log(self):
        return AIRequestLog.objects.order_by("-pk").first()

    def test_emit_uses_real_data_solve(self):
        before = AIRequestLog.objects.count()
        response = self._emit(self.superuser, AIRequestLog.MODE_SOLVE)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(json.loads(response.content)["ok"])
        self.assertEqual(AIRequestLog.objects.count(), before + 1)
        log = self._last_log()
        self.assertEqual(log.mode, AIRequestLog.MODE_SOLVE)
        # Реальный пользователь (identity — get_user_identity_for_log).
        self.assertEqual(log.user_full_name, "Иван Иванов")
        self.assertEqual(log.external_user_id, "5501")
        self.assertEqual(log.user_id, self.student.pk)
        # Модель — из реестра (не выдуманная строка).
        from ai.model_clients import registry
        titles = {registry.title(k) for k in registry.keys()}
        self.assertTrue(log.model_names)
        self.assertIn(log.model_names[0], titles)
        # Язык/тема/промпт — реальные строки БД, id и имя согласованы.
        self.assertEqual(log.programming_language_id, self.lang.pk)
        self.assertEqual(log.programming_language_name, "Pascal")
        self.assertEqual(log.topic_id, self.topic.pk)
        self.assertEqual(log.topic_name, "Массивы")
        self.assertEqual(log.prompt_id, self.prompt.pk)
        self.assertEqual(log.prompt_name, "Мой промпт")
        # Задача — только для solve/find_error (живая ссылка в деталях).
        self.assertEqual(log.task_node_id, 4321)
        self.assertEqual(log.task_name, "Сумма массива")
        self.assertIn(log.status, (AIRequestLog.STATUS_SUCCESS, AIRequestLog.STATUS_ERROR))

    def test_chat_mode_has_no_task(self):
        self._emit(self.superuser, AIRequestLog.MODE_CHAT)
        log = self._last_log()
        self.assertEqual(log.mode, AIRequestLog.MODE_CHAT)
        self.assertIsNone(log.task_node_id)
        self.assertEqual(log.task_name, "")

    def test_non_superuser_forbidden(self):
        plain = get_user_model().objects.create_user(username="td-plain", password="x")
        before = AIRequestLog.objects.count()
        response = self._emit(plain, AIRequestLog.MODE_CHAT)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(AIRequestLog.objects.count(), before)

    def test_logwriter_sync_core_parity(self):
        """Sync-ядро LogWriter — единственная реализация; async-обёртки
        (протокол WS-consumer не меняется) остаются корутинными и делегируют."""
        import asyncio

        from ai.services import log_writer as log_writer_module
        self.assertTrue(asyncio.iscoroutinefunction(LogWriter.create))
        self.assertTrue(asyncio.iscoroutinefunction(LogWriter.update_success))
        self.assertTrue(asyncio.iscoroutinefunction(LogWriter.update_error))
        self.assertFalse(asyncio.iscoroutinefunction(LogWriter.create_sync))

        writer = LogWriter()
        sent_at = timezone.now()
        log = writer.create_sync(
            user=self.student, username="td-student", external_user_id="5501",
            user_full_name="Иван Иванов", client_id="c1", source="websocket",
            mode="chat", sent_at=sent_at, model_names=["M"], message="m",
            programming_language_id=self.lang.pk, programming_language_name="Pascal",
            topic_id=self.topic.pk, topic_name="Массивы",
            prompt_id=self.prompt.pk, prompt_name="Мой промпт",
        )
        writer.update_success_sync(log, "готово", 42, "M1")
        log.refresh_from_db()
        self.assertEqual(log.status, AIRequestLog.STATUS_SUCCESS)
        self.assertEqual(log.tokens, 42)
        self.assertEqual(log.model_names, ["M1"])
        self.assertEqual(log.topic_name, "Массивы")
        writer.update_error_sync(log, "друж", "детально")
        log.refresh_from_db()
        self.assertEqual(log.status, AIRequestLog.STATUS_ERROR)
        self.assertEqual(log.error_message, "детально")


class ModelStatsApiTests(TestCase):
    """GET /ai/api/model-stats/?progLng=&topic= — скоуп-отдача для суффиксов
    задачных страниц: тема+язык — точные числа темы, только язык — общие
    данные по темам языка, без обоих — общие по ВСЕМ темам и языкам (агрегат);
    битый progLng/topic = отсутствие параметра; доступ — как у /ai/api/problem-data/."""

    def setUp(self):
        self.factory = RequestFactory()
        self.superuser = get_user_model().objects.create_superuser(
            username="stats-api", password="x", email="sa@t.com",
        )
        from ai.model_health import MODEL_CATALOG_KEYS
        if not MODEL_CATALOG_KEYS:
            self.skipTest("No models in registry")
        self.model_key = MODEL_CATALOG_KEYS[0]
        self.lang = ProgrammingLanguage.objects.create(language_name="Pascal")
        self.topic = Topic.objects.create(
            topic_name_ru="Массивы", programming_language=self.lang)

    def _request(self, params):
        from ai.views import get_model_stats_view
        request = self.factory.get("/ai/api/model-stats/", params)
        request.user = self.superuser
        request.session = {}
        request.user_info = {"userId": "77"}
        return get_model_stats_view(request)

    def _record(self, verdict, duration, topic_id):
        from ai.services.model_stats import record_batch_solve_stats
        record_batch_solve_stats([{
            "model_key": self.model_key, "model_title": "M", "verdict": verdict,
            "duration": duration,
            "programming_language_id": self.lang.pk, "topic_id": topic_id,
        }])

    def test_topic_scope(self):
        self._record("solved", 2.0, self.topic.pk)
        data = json.loads(self._request({"progLng": str(self.lang.pk), "topic": str(self.topic.pk)}).content)
        self.assertTrue(data["success"])
        self.assertEqual(data["stats"][self.model_key]["percent_solved"], 100.0)
        self.assertEqual(data["stats"][self.model_key]["avg_seconds"], 2.0)

    def test_language_scope_without_topic(self):
        self._record("failed", 4.0, None)
        data = json.loads(self._request({"progLng": str(self.lang.pk)}).content)
        self.assertEqual(data["stats"][self.model_key]["percent_solved"], 0.0)

    def test_global_aggregate_when_requested_without_scope(self):
        """Без языка/темы — ОБЩИЕ данные по всем темам и языкам: взвешенная
        сумма scoped-строк модели; легаси-строки (NULL, NULL) в агрегат не
        попадают; битый progLng по-прежнему пусто."""
        self._record("solved", 2.0, None)
        stats = json.loads(self._request({}).content)["stats"]
        self.assertEqual(
            stats[self.model_key], {"avg_seconds": 2.0, "percent_solved": 100.0})
        # Легаси-строка не влияет на агрегат:
        from ai.models import AIModelStats as AMS
        AMS.objects.create(
            model_key=self.model_key, solved_count=5, total_count=5,
            duration_count=1, duration_sum=9.0)
        stats = json.loads(self._request({}).content)["stats"]
        self.assertEqual(
            stats[self.model_key], {"avg_seconds": 2.0, "percent_solved": 100.0})
        # Вторая запись суммируется взвешенно: (2+4)/2 = 3.0; 1 из 2 = 50%.
        self._record("failed", 4.0, None)
        stats = json.loads(self._request({}).content)["stats"]
        self.assertEqual(
            stats[self.model_key], {"avg_seconds": 3.0, "percent_solved": 50.0})
        # Битый progLng = отсутствие параметра → тот же общий скоуп.
        self.assertEqual(
            json.loads(self._request({"progLng": "abc"}).content)["stats"],
            json.loads(self._request({}).content)["stats"],
        )

    def test_global_aggregate_spans_languages_and_topics(self):
        """Общий скоуп суммирует ВСЕ языки и темы модели (взвешенно), а не
        только последний записанный язык."""
        other_lang = ProgrammingLanguage.objects.create(language_name="Python")
        self._record("solved", 2.0, None)                       # Pascal-языковая
        self._record("failed", 4.0, None)                       # Pascal-языковая
        from ai.services.model_stats import record_batch_solve_stats
        record_batch_solve_stats([{
            "model_key": self.model_key, "model_title": "M", "verdict": "solved",
            "duration": 1.0, "programming_language_id": other_lang.pk,
            "topic_id": None,
        }])
        # (2+4+1)/3 = 2.3; решено 2 из 3 = 66.7.
        stats = json.loads(self._request({}).content)["stats"]
        self.assertEqual(
            stats[self.model_key], {"avg_seconds": 2.3, "percent_solved": 66.7})

    def test_no_rows_for_scope(self):
        self._record("solved", 2.0, self.topic.pk)
        other_topic = Topic.objects.create(
            topic_name_ru="Строки", programming_language=self.lang)
        data = json.loads(
            self._request({"progLng": str(self.lang.pk), "topic": str(other_topic.pk)}).content)
        self.assertEqual(data["success"], True)
        self.assertEqual(data["stats"], {})  # нет строк скоупа → «ничего»

    def test_requires_page_access(self):
        from ai.views import get_model_stats_view
        request = self.factory.get("/ai/api/model-stats/")
        request.user = self.superuser
        request.session = {}
        # Нет user_info и внешних id → 403 (как у problem-data); dev-bypass
        # глушим явно — views.py держит СВОЮ ссылку на _dev_bypass_enabled.
        with patch("ai.views._dev_bypass_enabled", return_value=False):
            with patch("ai.views.get_external_user_id_from_request", return_value=""):
                self.assertEqual(get_model_stats_view(request).status_code, 403)


class DeleteArmRunStatsTests(TestCase):
    """delete_arm_run вычитает вклад пары из ОБЕИХ скоуп-строк (тема + язык),
    язык прогона — из run_params.language_id; тема — из topic_id_snapshot
    (снимок task.topic_id на момент записи). Легаси-прогоны без языка
    уменьшают только (NULL, NULL)-строку."""

    def setUp(self):
        from ai.management.commands.delete_arm_run import Command
        self.cmd = Command()
        self.lang = ProgrammingLanguage.objects.create(language_name="Pascal")
        self.topic = Topic.objects.create(
            topic_name_ru="Массивы", programming_language=self.lang)

    @staticmethod
    def _res(key, verdict, duration, topic_id):
        return SimpleNamespace(
            model_key=key, verdict=verdict, duration_seconds=duration,
            topic_id_snapshot=topic_id,
        )

    def test_per_scope_deltas_buckets(self):
        # Тема-бакет видит только пары с этой темой; языковой — все три
        # (зеркально record_batch_solve_stats: solved/failed считаются,
        # skipped не считается И не даёт длительности).
        results = [
            self._res("K1", "solved", 2.0, self.topic.pk),
            self._res("K1", "failed", 4.0, self.topic.pk),
            self._res("K1", "skipped", 99.0, None),
        ]
        deltas = self.cmd._per_scope_deltas(results, self.lang.pk)
        topic_bucket = deltas[("K1", self.lang.pk, self.topic.pk)]
        self.assertEqual((topic_bucket["solved"], topic_bucket["total"]), (1, 2))
        self.assertEqual((topic_bucket["dur_n"], topic_bucket["dur_sum"]), (2, 6.0))
        lang_bucket = deltas[("K1", self.lang.pk, None)]
        self.assertEqual((lang_bucket["solved"], lang_bucket["total"]), (1, 2))
        self.assertEqual((lang_bucket["dur_n"], lang_bucket["dur_sum"]), (2, 6.0))

    def test_legacy_run_without_language(self):
        deltas = self.cmd._per_scope_deltas([self._res("K1", "solved", 2.0, self.topic.pk)], None)
        self.assertEqual(list(deltas.keys()), [("K1", None, None)])

    def test_subtract_stats_floors_at_zero(self):
        from ai.models import AIModelStats
        from ai.services.model_stats import record_batch_solve_stats
        record_batch_solve_stats([{
            "model_key": "K1", "model_title": "M", "verdict": "solved",
            "duration": 2.0,
            "programming_language_id": self.lang.pk, "topic_id": self.topic.pk,
        }])
        # Вычитаем БОЛЬШЕ, чем есть в теме-строке: пол на нуле (Greatest),
        # не минус. Языковую строку дельта не трогает.
        deltas = {("K1", self.lang.pk, self.topic.pk): {"solved": 5, "total": 5, "dur_n": 5, "dur_sum": 50.0}}
        self.cmd._subtract_stats(deltas, apply=True)
        row = AIModelStats.objects.get(
            model_key="K1", programming_language_id=self.lang.pk, topic_id=self.topic.pk)
        self.assertEqual((row.solved_count, row.total_count), (0, 0))
        self.assertEqual((row.duration_count, row.duration_sum), (0, 0.0))
        legacy = AIModelStats.objects.get(
            model_key="K1", programming_language_id=self.lang.pk, topic__isnull=True)
        self.assertEqual((legacy.solved_count, legacy.total_count), (1, 1))

    def test_subtract_from_both_scoped_rows(self):
        from ai.models import AIModelStats
        from ai.services.model_stats import record_batch_solve_stats
        record_batch_solve_stats([{
            "model_key": "K1", "model_title": "M", "verdict": "solved",
            "duration": 2.0,
            "programming_language_id": self.lang.pk, "topic_id": self.topic.pk,
        }])
        results = [self._res("K1", "solved", 2.0, self.topic.pk)]
        self.cmd._subtract_stats(self.cmd._per_scope_deltas(results, self.lang.pk), apply=True)
        for row in AIModelStats.objects.filter(model_key="K1"):
            self.assertEqual(row.total_count, 0)
        self.assertEqual(AIModelStats.objects.filter(total_count=0).count(), 2)


class ClientStatsGatingTests(SimpleTestCase):
    """Статик-контракт суффиксов (Batch B): суффиксы живут только на задачных
    страницах (гейт по #selectProgLng), цифры — из скоуп-ответа
    /ai/api/model-stats/, data-атрибуты остались сортировке."""

    def _read(self, *parts):
        from django.conf import settings
        with open(os.path.join(settings.BASE_DIR, *parts), encoding="utf-8") as fh:
            return fh.read()

    def test_ai_common_suffix_gate_and_scoped_fetch(self):
        js = self._read("static", "admin", "js", "ai-common.js")
        self.assertIn("function modelSuffixesEnabled()", js)
        self.assertIn("if (!modelSelect || !modelSuffixesEnabled()) return;", js)
        self.assertIn("function updateScopedModelStats()", js)
        self.assertIn("'/ai/api/model-stats/'", js)
        self.assertIn("function scopedModelOptionStats(opt)", js)
        # Wiring: смена #selectProgLng/#selectTheme + init/repopulate тянут скоуп.
        self.assertGreaterEqual(js.count("updateScopedModelStats();"), 4)

    def test_base_chat_keeps_sort_data_attrs(self):
        tpl = self._read("ai", "templates", "ai", "base_chat.html")
        self.assertIn("data-avg-seconds", tpl)
        self.assertIn("data-percent-solved", tpl)
        self.assertIn("selectModelSort", tpl)

    def test_arm_runner_results_carry_scope(self):
        src = self._read("ai", "arm_runner.py")
        self.assertIn('"programming_language_id": ctx["programming_language_id"]', src)
        self.assertIn('"topic_id": task.topic_id', src)

    def test_wizard_version_bumped(self):
        from ai.constants import WIZARD_VERSION
        from ai.services import onboarding as onboarding_service
        self.assertEqual(onboarding_service.WIZARD_VERSION, WIZARD_VERSION)
        self.assertGreaterEqual(WIZARD_VERSION, 8)


class SettingsPagesAlignmentTests(TestCase):
    """Единый вид таблиц журнала и пакетных решений.

    ТЗ: «в журнале запросов ВСЁ должно быть в одной таблице» + «ЛЮБОЙ лог
    пакетного решения должен выглядеть одинаково». Поэтому:

    * журнал — ОДНА таблица (шапка admin/ai/_ai_journal_head.html, 18
      колонок), где обычные запросы (admin/ai/_ai_log_row.html +
      _ai_log_detail.html) и пакетные прогоны (admin/ai/_ai_batch_row.html +
      _ai_batch_detail.html) лежат ВМЕСТЕ;
    * партиалы пакета подключаются на ВСЕХ поверхностях (журнал запросов,
      настройки, закреплённые) — вид пакета один.
    """

    def _read(self, *parts):
        from django.conf import settings
        with open(os.path.join(settings.BASE_DIR, *parts), encoding="utf-8") as fh:
            return fh.read()

    def test_settings_page_uses_shared_partials(self):
        src = self._read("ai", "templates", "admin", "ai", "aiappsettings_change_form.html")
        for partial in (
            "admin/ai/_ai_journal_head.html",
            "admin/ai/_ai_log_row.html",
            "admin/ai/_ai_batch_row.html",
            "admin/ai/_ai_batch_detail.html",
        ):
            self.assertIn(partial, src)

    def test_journal_head_has_all_columns(self):
        src = self._read("ai", "templates", "admin", "ai", "_ai_journal_head.html")
        for title in ("Кто отправлял", "Отправлен", "Получен", "Режим",
                      "Название прогона", "Курс", "Расширение", "Язык",
                      "Тема", "Промпт", "Модель", "Решено",
                      "Не решено", "Всего пар", "Статус"):
            self.assertIn("<th>%s</th>" % title, src)
        self.assertIn('<th title="От начала прогона до его окончания">Время</th>', src)
        self.assertIn("★", src)
        # ★ — закрепление пакета; в обычной строке ячейка пустая.
        log_row = self._read("ai", "templates", "admin", "ai", "_ai_log_row.html")
        self.assertIn('<td class="ai-log-pin-cell"></td>', log_row)

    def test_batch_row_partial_shows_run_and_counts(self):
        src = self._read("ai", "templates", "admin", "ai", "_ai_batch_row.html")
        self.assertIn("{{ row.run_name|default:\"—\" }}", src)
        self.assertIn("{{ row.course_id|default:\"—\" }}", src)
        self.assertIn("{{ row.file_extension|default:\"—\" }}", src)
        self.assertIn("{{ row.topics_display|default:\"—\" }}", src)
        self.assertIn("{{ row.solved|default:\"—\" }}", src)
        self.assertIn("{{ row.failed|default:\"—\" }}", src)
        self.assertIn("{{ row.total_pairs|default:\"—\" }}", src)
        self.assertIn("{{ row.time_display|default:\"—\" }}", src)
        # Промпт(ы) прогона — ссылками на промпт.
        self.assertIn("/ai/admin/ai/prompt/{{ p.id }}/change/", src)

    def test_batch_detail_colspan_matches_head(self):
        head = self._read("ai", "templates", "admin", "ai", "_ai_journal_head.html")
        detail = self._read("ai", "templates", "admin", "ai", "_ai_batch_detail.html")
        columns = head.count("<th>") + head.count("<th ")
        self.assertIn('colspan="%d"' % columns, detail)

    def test_log_row_partial_contract(self):
        src = self._read("ai", "templates", "admin", "ai", "_ai_log_row.html")
        self.assertIn("{{ row.sender_display }}", src)
        self.assertIn('{{ row.received_at|date:"d.m.Y:H:i:s"|default:"—" }}', src)
        self.assertIn("{{ row.topic_display }}", src)
        self.assertIn("{{ row.prompt_display }}", src)
        self.assertIn("{{ row.model_display }}", src)
        self.assertIn("{{ row.time_display }}", src)
        self.assertIn("ai-log-row", src)

    def test_journal_page_has_one_table(self):
        """Журнал — ОДНА таблица: обычные запросы и пакетные прогоны вместе."""
        src = self._read("ai", "templates", "admin", "ai", "request_logs_table.html")
        self.assertEqual(src.count("<table"), 1)
        self.assertIn("admin/ai/_ai_journal_head.html", src)
        self.assertIn("admin/ai/_ai_log_row.html", src)
        self.assertIn("admin/ai/_ai_log_detail.html", src)
        self.assertIn("admin/ai/_ai_batch_row.html", src)
        self.assertIn("admin/ai/_ai_batch_detail.html", src)

    def test_every_surface_uses_the_same_batch_partials(self):
        """Ключевое требование ТЗ: вид пакета ОДИН на всех поверхностях."""
        for path in (
            ("ai", "templates", "admin", "ai", "aiappsettings_change_form.html"),
            ("ai", "templates", "admin", "ai", "request_logs_table.html"),
            ("ai", "templates", "admin", "ai", "pinned_runs.html"),
        ):
            src = self._read(*path)
            self.assertIn("admin/ai/_ai_journal_head.html", src, path)
            self.assertIn("admin/ai/_ai_batch_row.html", src, path)
            self.assertIn("admin/ai/_ai_batch_detail.html", src, path)

    def test_recent_log_rows_builder_keys(self):
        factory = RequestFactory()
        su = get_user_model().objects.create_superuser(
            username="align-admin", password="x", email="a@t.com")
        lang = ProgrammingLanguage.objects.create(language_name="Pascal")
        prompt = Prompt.objects.create(
            prompt_name_ru="П", prompt_text_ru="Т", owner=su)
        AIRequestLog.objects.create(
            user=su, source="chat", mode="solve", message="msg",
            status=AIRequestLog.STATUS_SUCCESS, sent_at=timezone.now(),
            received_at=timezone.now(),
            programming_language_id=lang.pk, programming_language_name="Pascal",
            topic_name="Массивы", prompt_id=prompt.pk, prompt_name="П",
            model_names=["M"],
        )
        request = factory.get("/ai/admin/ai/aiappsettings/1/change/")
        request.user = su
        request.session = {}
        from ai.admin.request_logs import build_recent_log_rows
        row = build_recent_log_rows(request, limit=5)["recent_logs"][0]
        self.assertEqual(row["prompt_id"], prompt.pk)
        self.assertEqual(row["prompt_name"], "П")
        self.assertIsNotNone(row["received_at"])

    def test_batch_rows_builder_has_duration(self):
        from ai.models import AIModelTestRun
        from ai.admin.request_logs import build_batch_rows_for_logs
        factory = RequestFactory()
        su = get_user_model().objects.create_superuser(
            username="align-batch", password="x", email="ab@t.com")
        run_hex = uuid.uuid4().hex
        now = timezone.now()
        AIModelTestRun.objects.create(
            run_id=run_hex, run_type=AIModelTestRun.RUN_TYPE_BATCH,
            status=AIModelTestRun.STATUS_COMPLETED,
            started_at=now - timedelta(seconds=45), finished_at=now,
            run_params={},
        )
        log = AIRequestLog.objects.create(
            user=su, source="arm", mode="batch_solve",
            message=f"Batch solve run {run_hex}", sent_at=now,
        )
        request = factory.get("/ai/admin/ai/aiappsettings/1/change/")
        request.user = su
        request.session = {}
        rows = build_batch_rows_for_logs(request, [log])
        self.assertEqual(rows[0]["run_duration_display"], "45 с")


class AutoFiltersStaticTests(TestCase):
    """Динамические фильтры «сам применяет, без кнопки»: общий хелпер
    ai_auto_filters.js (текст с паузой, селекты/даты мгновенно, Enter — сразу,
    submit-перехват против двойной отправки) + страницы без кнопки
    «Фильтровать». Журнал и changelists не трогаем — у каждого своя механика
    (AJAX-партиал / select[data-ai-filter] в change_list.html)."""

    def _read(self, *parts):
        from django.conf import settings
        with open(os.path.join(settings.BASE_DIR, *parts), encoding="utf-8") as fh:
            return fh.read()

    def test_helper_mechanics(self):
        js = self._read("static", "admin", "js", "ai_auto_filters.js")
        self.assertIn("initAiAutoFilters", js)
        # Текстовые поля — с паузой (debounce), селекты и даты — мгновенно.
        self.assertIn("setTimeout", js)
        self.assertIn("'change'", js)
        # Enter — сразу (и preventDefault, чтобы не было второй отправки).
        self.assertIn("Enter", js)
        self.assertIn("preventDefault", js)
        # Полная GET-отправка формы (у страниц-потребителей нет партиала).
        self.assertIn("formEl.submit()", js)

    def test_daily_report_template(self):
        """Дневной отчёт: ФИО-фильтр ПЕРВЫМ (до «Группа»), inline onchange
        снят с селектов, хелпер подключён и инициализируется."""
        src = self._read("ai", "templates", "admin", "ai", "daily_report.html")
        student = src.index('id="daily-filter-student"')
        self.assertLess(student, src.index('id="daily-filter-group"'))
        self.assertIn('name="student"', src)
        self.assertNotIn("onchange=", src)
        self.assertIn("ai_auto_filters.js", src)
        # Инициализация хелпера — в вынесенном JS страницы.
        js = self._read("static", "admin", "ai", "daily_report", "index.js")
        self.assertIn("initAiAutoFilters(document.querySelector('.ai-daily-filters'))", js)

    def test_updates_template_no_filter_button(self):
        """«Обновления»: кнопки «Фильтровать» больше нет, «Сбросить» и
        «Скрытые записи» на месте, хелпер подключён по .ai-updates-filter."""
        src = self._read("ai", "templates", "admin", "ai", "updates.html")
        self.assertNotIn("Фильтровать", src)
        self.assertIn("Сбросить", src)
        self.assertIn('href="/ai/admin/ai/updatelog/?hidden__exact=1"', src)
        self.assertIn("ai_auto_filters.js", src)
        js = self._read("static", "admin", "ai", "updates", "index.js")
        self.assertIn("initAiAutoFilters(document.querySelector('.ai-updates-filter'))", js)
        self.assertIn("{% load i18n static %}", src)

    def test_wizard_registry_updated(self):
        """Туры: у «Тестовых данных» свой scope с 4 шагами; в туре журнала —
        шаг ФИО-фильтра дневного отчёта ПЕРЕД шагами селектов; тексты шагов
        селектов «Обновлений» обещают мгновенное применение."""
        js = self._read("static", "admin", "js", "ai-wizard.js")
        entry = js.index("admin_test_data: [")
        # Конец массива scope'а — закрывающая скобка на своей строке (не
        # "];": registry может продолжиться следующими scope'ами).
        block = js[entry: js.index("\n        ],", entry)]
        for sel in (".test-data-page h2", ".help-test-data",
                    "[data-emit-request] .button", "#emit-status"):
            self.assertIn(f'sel: "{sel}"', block)
        self.assertEqual(block.count("sel:"), 4)
        # ФИО-фильтр дневного отчёта: ПЕРЕД шагом «Группа».
        self.assertLess(
            js.index("#daily-filter-student"), js.index("#daily-filter-group"))
        # admin_updates: «применится само» — в текстах шагов фильтров.
        updates = js[js.index("admin_updates: ["): js.index("admin_test_console: [")]
        self.assertIn("сузится сам", updates)
        self.assertIn("применяется само", updates)

    def test_wizard_registry_admin_test_data_texts(self):
        """Текст шага статуса «Тестовых данных» честен: записи удаляются
        только в стоковом списке журнала, НЕ кнопкой «Сбросить»."""
        js = self._read("static", "admin", "js", "ai-wizard.js")
        self.assertIn("airequestlog", js)

    def test_wizard_admin_tours_test_data_step(self):
        """Ролевые туры (admin_pd/staff/super): шаг «Тестовые данные» идёт
        СРАЗУ ПОСЛЕ «Тестовой консоли» в общем списке страниц меню (по
        требованию владельца). Ссылка супер-админская — pd/staff её не видят,
        и шаг вырезается отсутствием в DOM (v1-принцип, без ветвлений)."""
        js = self._read("static", "admin", "js", "ai-wizard.js")
        console = js.index('a[href^="/ai/admin/test-console/"]')
        testdata = js.index('a[href^="/ai/admin/test_data/"]')
        solved = js.index('a[href^="/ai/admin/ai/tasksolution/"]')
        self.assertLess(console, testdata)
        self.assertLess(testdata, solved)


class ChatThinkToggleStaticTests(TestCase):
    """Кнопка Think (переключатель рассуждений для Ollama-моделей):
    общая шапка base_chat.html — одна кнопка на все три страницы чата;
    механика в ai-common.js (видимость по ключу Ollama_*, состояние в
    ai_state, триязычная подпись); WS-конверты страниц шлют think, когда
    кнопка видима (undefined у не-Ollama — JSON.stringify поле опустит)."""

    def _read(self, *parts):
        from django.conf import settings
        with open(os.path.join(settings.BASE_DIR, *parts), encoding="utf-8") as fh:
            return fh.read()

    def test_button_in_shared_header(self):
        """"Кнопка в общей шапке base_chat.html — по ней достаются все 3 страницы."""
        src = self._read("ai", "templates", "ai", "base_chat.html")
        self.assertIn('id="thinkToggleBtn"', src)
        self.assertIn('class="think-toggle"', src)
        self.assertIn("hidden", src)

    def test_ai_common_mechanics(self):
        js = self._read("static", "admin", "js", "ai-common.js")
        self.assertIn("function getThinkOn", js)
        self.assertIn("function refreshThinkToggleVisibility", js)
        self.assertIn("function updateThinkToggleLabel", js)
        self.assertIn("function initThinkToggle", js)
        self.assertIn("function aiThinkEnabled", js)
        self.assertIn("indexOf('Ollama_')", js)
        self.assertIn("setAiState({ think: getThinkOn() ? 0 : 1 });", js)
        # Хук инициализации в initSelectionPersistence (после restoreSelections).
        self.assertIn("initThinkToggle();", js)
        # Подпись на смене языка UI (applySharedUiLanguageTexts).
        self.assertIn("updateThinkToggleLabel();", js)
        # Chip короткий («Think»), состояние — в aria-pressed/title; локализация
        # подсказки триязычная.
        self.assertIn("aria-pressed", js)
        for key in ("thinkLabel:", "thinkOnTitle:", "thinkOffTitle:"):
            self.assertEqual(js.count(key), 3)

    def test_envelopes_send_think(self):
        """Конверты трёх страниц идут с think: aiThinkEnabled() — у chat type=1,
        solve type=2 и find_error type=3 по два места отправки."""
        self.assertEqual(
            self._read("static", "admin", "js", "chat_template.js").count("think: aiThinkEnabled()"), 2)
        self.assertEqual(
            self._read("static", "admin", "js", "decide_task.js").count("think: aiThinkEnabled(),"), 2)
        self.assertEqual(
            self._read("static", "admin", "js", "find_error.js").count("think: aiThinkEnabled(),"), 2)

    def test_css_state_styling(self):
        css = self._read("static", "admin", "css", "ai_theme.css")
        self.assertIn(".think-toggle {", css)
        self.assertIn(".think-toggle.ai-think-on {", css)
        # author-display перебивает UA-стиль [hidden] — guard обязателен,
        # иначе кнопка светится и на не-Ollama моделях.
        self.assertIn(".think-toggle[hidden] {", css)

    def test_wizard_chat_tour_think_step(self):
        """Чат-тур: шаг кнопки Think — ПОСЛЕ выбора модели и ДО сортировки
        (по требованию владельца), с skipIfHidden: кнопка скрыта на
        не-Ollama моделях, шаг пропускается мгновенно (контракт «скрыта
        сейчас = шаг не для этого состояния»), а не ждать и падать.
        en/fr-переводы шага на месте (триязычный клиент)."""
        js = self._read("static", "admin", "js", "ai-wizard.js")
        chat = js[js.index("        chat: ["): js.index("        solve: [")]
        self.assertIn('sel: "#thinkToggleBtn"', chat)
        self.assertLess(
            chat.index('sel: "#select", rank: 11'), chat.index('sel: "#thinkToggleBtn"'))
        self.assertLess(
            chat.index('sel: "#thinkToggleBtn"'), chat.index('sel: "#selectModelSort"'))
        step_at = js.index('sel: "#thinkToggleBtn", rank: 12')
        self.assertIn("skipIfHidden: true", js[step_at: step_at + 300])
        # Движок: мгновенный пропуск скрытой цели по флагу шага.
        self.assertIn("step.skipIfHidden", js)
        self.assertIn('"#thinkToggleBtn": { title: "The Think button"', js)
        self.assertIn('"#thinkToggleBtn": { title: "Le bouton Think"', js)


class AdminPagesStaticFixesTests(TestCase):
    """Регрессии расрезки CSS/JS admin-шаблонов: содержимое расширяющего
    шаблона вне {% block %} Django НЕ рендерит — теги верхнего уровня
    (скрипт/стили) молча пропадали (на «Настройке ИИ» из-за этого не
    раскрывались детали прогонов/запросов); и кнопка «Обновить» журнала."""

    def _read(self, *parts):
        from django.conf import settings
        with open(os.path.join(settings.BASE_DIR, *parts), encoding="utf-8") as fh:
            return fh.read()

    def test_settings_script_and_css_inside_blocks(self):
        """«Настройка ИИ»: index.css — внутри {% block extrastyle %}, index.js —
        в конце {% block after_field_sets %} (после таблиц — строки уже в DOM);
        верхний уровень (до первого {% block %}) без тегов вовсе."""
        src = self._read("ai", "templates", "admin", "ai", "aiappsettings_change_form.html")
        first_block = src.index("{% block ")
        self.assertNotIn("<link", src[:first_block])
        self.assertNotIn("<script", src[:first_block])
        self.assertIn("{% block extrastyle %}", src)
        self.assertGreater(src.index('app_settings/index.css'), src.index("{% block extrastyle %}"))
        self.assertGreater(src.index('app_settings/index.js'), src.index("{% block after_field_sets %}"))
        self.assertLess(src.index('app_settings/index.js'), src.index("{% endblock %}", src.index('app_settings/index.js')))

    def test_journal_refresh_button(self):
        """Журнал: кнопка «Обновить» в .ai-logs-actions (type=button, чтобы не
        сабмитить форму) и проводка на refreshTable в index.js."""
        src = self._read("ai", "templates", "admin", "ai", "request_logs.html")
        actions = src.index('<div class="ai-logs-actions">')
        self.assertGreater(src.index('<button type="button" class="button" id="ai-logs-refresh"'), actions)
        js = self._read("static", "admin", "ai", "request_logs", "index.js")
        self.assertIn("ai-logs-refresh", js)
        self.assertIn("refreshBtn.addEventListener('click', () => refreshTable())", js)


class KeyedPageAccessTests(_AdminViewRequestMixin, TestCase):
    """Ключи-«непоседы» (ТЗ 2026-10-19): «Тестовые данные», «Группы доступа»,
    док «Разработчик промптов» для не-pd, действия запуска/публикации —
    всё выдаётся через «Группы доступа», не только ролью."""

    def setUp(self):
        self.factory = RequestFactory()
        self.user_model = get_user_model()

    def _permission(self, key):
        from ai.models import AIAdminGroupPermission
        section = ("action" if key.startswith("action.") else
                   "docs" if key.startswith("docs.") else "page")
        row, _ = AIAdminGroupPermission.objects.get_or_create(
            key=key, defaults={"label": key, "section": section},
        )
        return row

    def _grant(self, username, keys):
        from django.contrib.auth import get_user_model
        from ai.models import AIAccessGroup
        rows = {k: self._permission(k) for k in keys}
        group = AIAccessGroup.objects.create(name=f"Ключи-{username}")
        group.permissions.add(*rows.values())
        user = get_user_model().objects.create_user(username=username, password="x")
        group.users.add(user)
        return user

    def _docs_view(self, user, slug):
        from unittest.mock import patch
        from ai.admin.docs import admin_docs_view
        request = self._admin_request(user, path=f"/ai/admin/docs/{slug}/")
        with patch("ai.admin.site.get_external_user_id_from_request", return_value="12345"):
            return admin_docs_view(request, slug=slug)

    def test_test_data_page_by_key_only(self):
        """page.test_data: не-staff держатель ключа видит страницу; без ключа — 403."""
        from ai.admin.test_data import test_data_view
        user = self._grant("testdata-key", ["page.admin", "page.test_data"])
        request = self._admin_request(user, path="/ai/admin/test_data/")
        self.assertEqual(test_data_view(request).status_code, 200)

        user2 = self._grant("testdata-plain", ["page.admin"])
        request = self._admin_request(user2, path="/ai/admin/test_data/")
        self.assertEqual(test_data_view(request).status_code, 403)

    def test_access_groups_page_by_key_only(self):
        """page.groups: держатель видит страницу и сохраняет группу."""
        from ai.admin.access_groups import admin_access_groups_view, admin_access_group_save_view
        user = self._grant("groups-key", ["page.admin", "page.groups"])
        request = self._admin_request(user, path="/ai/admin/ai-groups/")
        self.assertEqual(admin_access_groups_view(request).status_code, 200)

        save = self._admin_request(
            user, method="post", path="/ai/admin/ai-groups/save/", data={
                "name": "Наблюдатели", "permissions": ["page.model_status"],
            })
        self.assertEqual(admin_access_group_save_view(save, 0).status_code, 200)
        from ai.models import AIAccessGroup
        self.assertTrue(
            AIAccessGroup.objects.filter(name="Наблюдатели").exists())

    def test_prompt_developer_doc_by_key_for_non_pd(self):
        """docs.prompt_developer: обычный вошедший (не pd) с ключом читает главу."""
        user = self._grant("docstester-key", ["page.admin", "docs.prompt_developer"])
        self.assertEqual(self._docs_view(user, "prompt-developer").status_code, 200)

        user2 = self._grant("docstester-plain", ["page.admin"])
        self.assertEqual(self._docs_view(user2, "prompt-developer").status_code, 403)

    def test_test_console_start_requires_run_action(self):
        """Просмотр консоли — по ключу страницы; запуск — только с
        action.test_console_run (суперюзер всегда может)."""
        from unittest.mock import patch
        from ai.admin.test_console import admin_test_console_view, admin_test_console_start_view
        user = self._grant("console-viewer", ["page.admin", "page.test_console"])
        request = self._admin_request(user, path="/ai/admin/test-console/")
        self.assertEqual(admin_test_console_view(request).status_code, 200)

        post = self._admin_request(
            user, method="post", path="/ai/admin/test-console/start/")
        self.assertEqual(admin_test_console_start_view(post).status_code, 403)

        runner = self._grant(
            "console-runner",
            ["page.admin", "page.test_console", "action.test_console_run"],
        )
        post = self._admin_request(
            runner, method="post", path="/ai/admin/test-console/start/",
            content_type=None,
        )
        with patch("ai.admin.test_console.start_test_run",
                   return_value=("mock-run-id", None)):
            response = admin_test_console_start_view(post)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content)["run_id"], "mock-run-id")

    def test_updates_publish_action_allows_non_super_save(self):
        """action.updates_publish: не-супер с ModelAdmin-ключом + действием
        правит запись UpdateLog; без действия — только просмотр."""
        from ai.admin.models import UpdateLogAdmin
        from ai.admin.site import ai_admin_site
        from ai.models import UpdateLog

        updater = self._grant(
            "updates-editor",
            ["page.admin", "page.mgmt_update_log", "page.updates"],
        )
        request = self._admin_request(updater, path="/x/")
        admin = UpdateLogAdmin(UpdateLog, ai_admin_site)
        self.assertFalse(admin.has_change_permission(request, obj=None))

        updater2 = self._grant(
            "updates-publisher",
            ["page.admin", "page.mgmt_update_log", "page.updates",
             "action.updates_publish"],
        )
        request = self._admin_request(updater2, path="/x/")
        self.assertTrue(admin.has_change_permission(request, obj=None))


class BaselineMarkerTests(_AdminViewRequestMixin, TestCase):
    """Базовые права (BASELINE_ACCESS_KEYS) в карточках «Групп доступа»:
    чекбокс включён и заблокирован + пометка «база» — фактический доступ
    шире галочек группы, и это видно прямо в карточке."""

    def setUp(self):
        self.factory = RequestFactory()
        self.user_model = get_user_model()

    def test_baseline_keys_rendered_checked_disabled(self):
        from ai.admin.access_groups import admin_access_groups_view
        superuser = self.user_model.objects.create_superuser(
            username="gc-super", password="x", email="a@t.com",
        )
        request = self._admin_request(superuser, path="/ai/admin/ai-groups/")
        response = admin_access_groups_view(request)
        response.render()
        html = response.content.decode()
        for key in ("page.logs", "page.prompt_admin_all"):
            self.assertIn(f'value="{key}"', html)
        self.assertIn('disabled class="ai-perm-baseline"', html)
        self.assertIn(">база</em>", html)

    def test_page_admin_is_first_in_pages_section(self):
        # «Вход в админку ИИ» — гейт входа: самый верх списка «Страницы»,
        # все остальные страницы — после него.
        from ai.admin.access_groups import admin_access_groups_view
        superuser = self.user_model.objects.create_superuser(
            username="gc-order", password="x", email="a@t.com",
        )
        request = self._admin_request(superuser, path="/ai/admin/ai-groups/")
        response = admin_access_groups_view(request)
        response.render()
        html = response.content.decode()
        self.assertLess(
            html.index('value="page.admin"'),
            min(
                html.index(f'value="{key}"')
                for key in ("page.arm_solve", "page.model_status", "page.tasks")
            ),
        )


class WizardSeenNavTests(_AdminViewRequestMixin, TestCase):
    """Ключ page.mgmt_wizard_seen открывает листинг «Пройденные онбординги»
    в левом наве («Раздел ИИ»), как и любой другой mgmt-ключ."""

    def setUp(self):
        self.factory = RequestFactory()
        self.user_model = get_user_model()

    def _context(self, user):
        from unittest.mock import patch
        from ai.admin.site import ai_admin_site
        request = self._admin_request(user, path="/ai/admin/")
        with patch("ai.admin.site.get_external_user_id_from_request", return_value="12345"):
            return ai_admin_site.each_context(request)

    def _grant(self, username, keys):
        from ai.models import AIAccessGroup, AIAdminGroupPermission
        user = self.user_model.objects.create_user(username=username, password="x")
        group = AIAccessGroup.objects.create(name=f"Ключи-{username}")
        for key in keys:
            section = "action" if key.startswith("action.") else "page"
            perm, _ = AIAdminGroupPermission.objects.get_or_create(
                key=key, defaults={"label": key, "section": section},
            )
            group.permissions.add(perm)
        group.users.add(user)
        return user

    def test_wizard_seen_visible_with_key(self):
        user = self._grant(
            "nav-wizard", ["page.admin", "page.mgmt_wizard_seen"],
        )
        context = self._context(user)
        names = {
            model["object_name"]
            for app in context["available_apps"]
            for model in app.get("models", [])
        }
        self.assertIn("AIWizardSeen", names)

    def test_wizard_seen_hidden_without_key(self):
        user = self.user_model.objects.create_user(username="nav-nokey", password="x")
        context = self._context(user)
        names = {
            model["object_name"]
            for app in context["available_apps"]
            for model in app.get("models", [])
        }
        self.assertNotIn("AIWizardSeen", names)

    def test_wizard_seen_superuser_sees(self):
        user = self.user_model.objects.create_user(
            username="nav-super", password="x", is_superuser=True, is_staff=True,
        )
        context = self._context(user)
        names = {
            model["object_name"]
            for app in context["available_apps"]
            for model in app.get("models", [])
        }
        self.assertIn("AIWizardSeen", names)


class FailureReasonClassifierTests(SimpleTestCase):
    """Классификатор причин сбоя по тексту DL-комментария.

    Канонический владелец — ai/dl_api_client.py::dl_failure_reason_from_comment
    (рядом с dl_verdict_from_comment). Причина ОРТОГОНАЛЬНА вердикту:
    «прошёл ли DL» и «почему нет» — разные вопросы.
    """

    def _reason(self, comment):
        from ai.dl_api_client import dl_failure_reason_from_comment
        return dl_failure_reason_from_comment(comment)

    def test_compile_error_markers(self):
        for comment in (
            "Ошибка компиляции в строке 5: expected ';'",
            "compile error",
            "syntax error",
            "Не компилируется",
            "Ошибка синтаксиса",
        ):
            self.assertEqual(self._reason(comment), "compile_error", comment)

    def test_wrong_answer_markers(self):
        for comment in (
            "Неправильный ответ на тесте 2",
            "Неверный вывод программы",
            "Вывод не совпадает с ожидаемым",
            "Не все тесты пройдены",
            "wrong answer on test 3",
            "Solution rejected",
        ):
            self.assertEqual(self._reason(comment), "wrong_answer", comment)

    def test_unrecognized_failure_is_undefined_not_success(self):
        # Провал НИКОГДА не переименовывается в успех: нераспознанный провал
        # = undefined, а не пустая строка.
        self.assertEqual(self._reason("Какая-то ошибка 500"), "undefined")
        self.assertEqual(self._reason(""), "undefined")
        self.assertEqual(self._reason(None), "undefined")

    def test_success_has_no_reason(self):
        self.assertEqual(self._reason("Все тесты пройдены успешно"), "")
        self.assertEqual(self._reason("ОК"), "")

    def test_compile_markers_win_over_generic_failure(self):
        # «ошибк» — общий провальный маркер, но про компиляцию: причина
        # компиляции, а не «не распознано».
        self.assertEqual(
            self._reason("Ошибка компиляции в строке 5"),
            "compile_error",
        )


class FailureReasonBatchReportTests(SimpleTestCase):
    """Раздел «Сбои по причинам» в отчёте прогона (arm_runner)."""

    def _reasons(self, results):
        from ai.arm_runner import _build_failure_reasons
        return {row["key"]: row for row in _build_failure_reasons(results)}

    def test_solved_pairs_are_not_counted(self):
        results = [
            {"verdict": "solved", "failure_reason": ""},
            {"verdict": "failed", "failure_reason": "compile_error"},
        ]
        reasons = self._reasons(results)
        self.assertNotIn("solved", reasons)
        self.assertEqual(reasons["compile_error"]["count"], 1)

    def test_counts_and_percent(self):
        results = [
            {"verdict": "failed", "failure_reason": "compile_error"},
            {"verdict": "failed", "failure_reason": "compile_error"},
            {"verdict": "failed", "failure_reason": "wrong_answer"},
            {"verdict": "solved", "failure_reason": ""},
        ]
        reasons = self._reasons(results)
        self.assertEqual(reasons["compile_error"]["count"], 2)
        self.assertEqual(reasons["wrong_answer"]["count"], 1)
        # Процент — от всех пар прогона (4), а не от проваленных.
        self.assertEqual(reasons["compile_error"]["percent"], 50)

    def test_labels_are_human_readable(self):
        results = [{"verdict": "failed", "failure_reason": "no_code"}]
        reasons = self._reasons(results)
        self.assertEqual(reasons["no_code"]["label"], "Не выкушен код")


class FailureReasonBackfillTests(TestCase):
    """Бэкфилл причин сбоя для старых прогонов (management-команда)."""

    def _make_result(self, **kwargs):
        from ai.models import AIModelTestResult, AIModelTestRun
        run = AIModelTestRun.objects.create(
            run_id=uuid.uuid4().hex,
            run_type=AIModelTestRun.RUN_TYPE_BATCH,
        )
        defaults = {
            "run": run,
            "model_key": "M1",
            "model_title": "Model 1",
            "status": AIModelTestResult.STATUS_OK,
            "verdict": AIModelTestResult.VERDICT_FAILED,
        }
        defaults.update(kwargs)
        return AIModelTestResult.objects.create(**defaults)

    def test_dry_run_writes_nothing(self):
        from io import StringIO
        from django.core.management import call_command
        row = self._make_result(dl_comment="Ошибка компиляции в строке 5")
        out = StringIO()
        call_command("backfill_arm_failure_reasons", stdout=out)
        row.refresh_from_db()
        self.assertEqual(row.failure_reason, "")

    def test_commit_sets_reason(self):
        from io import StringIO
        from django.core.management import call_command
        row = self._make_result(dl_comment="Неправильный ответ на тесте 1")
        out = StringIO()
        call_command("backfill_arm_failure_reasons", "--commit", stdout=out)
        row.refresh_from_db()
        self.assertEqual(row.failure_reason, "wrong_answer")

    def test_solved_rows_are_skipped(self):
        from io import StringIO
        from django.core.management import call_command
        from ai.models import AIModelTestResult
        row = self._make_result(
            dl_comment="Ошибка компиляции",
            verdict=AIModelTestResult.VERDICT_SOLVED,
        )
        out = StringIO()
        call_command("backfill_arm_failure_reasons", "--commit", stdout=out)
        row.refresh_from_db()
        self.assertEqual(row.failure_reason, "")

    def test_rerun_is_idempotent(self):
        from io import StringIO
        from django.core.management import call_command
        self._make_result(dl_comment="Неверный вывод программы")
        out = StringIO()
        call_command("backfill_arm_failure_reasons", "--commit", stdout=out)
        out2 = StringIO()
        call_command("backfill_arm_failure_reasons", "--commit", stdout=out2)
        self.assertIn("Проверено строк без причины: 0", out2.getvalue())


class TokenUsageAggregationTests(TestCase):
    """Агрегация токенов (журнал + пакетные пары) — единственный владелец
    ai/services/token_usage.py."""

    def setUp(self):
        from ai.models import AIRequestLog
        self.now = timezone.now()
        self.log = AIRequestLog.objects.create(
            sent_at=self.now,
            mode=AIRequestLog.MODE_CHAT,
            source=AIRequestLog.SOURCE_WEBSOCKET,
            status=AIRequestLog.STATUS_SUCCESS,
            model_names=["Ollama Gemma"],
            tokens=100,
            tokens_prompt=40,
            user_full_name="Иванов Иван",
            external_user_id="dl-1",
        )

    def test_prompt_plus_completion_is_total(self):
        from ai.services.token_usage import collect_log_slices
        report = collect_log_slices(
            __import__("ai.models", fromlist=["AIRequestLog"]).AIRequestLog.objects.all()
        )
        self.assertEqual(report["total"]["total_tokens"], 140)
        self.assertEqual(report["total"]["completion_tokens"], 100)
        self.assertEqual(report["total"]["prompt_tokens"], 40)

    def test_slices_by_model_user_mode(self):
        from ai.models import AIRequestLog
        from ai.services.token_usage import collect_log_slices
        report = collect_log_slices(AIRequestLog.objects.all())
        self.assertEqual(report["by_model"][0]["label"], "Ollama Gemma")
        self.assertEqual(report["by_user"][0]["label"], "Иванов Иван")
        self.assertEqual(report["by_mode"][0]["key"], "chat")

    def test_web_pool_marks_incomplete(self):
        from ai.models import AIRequestLog
        from ai.services.token_usage import collect_log_slices
        AIRequestLog.objects.create(
            sent_at=self.now,
            mode=AIRequestLog.MODE_CHAT,
            source=AIRequestLog.SOURCE_WEBSOCKET,
            status=AIRequestLog.STATUS_SUCCESS,
            model_names=["Web_DeepSeek"],
            tokens=0,
            tokens_prompt=0,
        )
        report = collect_log_slices(AIRequestLog.objects.all())
        self.assertTrue(report["incomplete"])

    def test_mode_filter(self):
        from ai.models import AIRequestLog
        from ai.services.token_usage import collect_log_slices
        AIRequestLog.objects.create(
            sent_at=self.now,
            mode=AIRequestLog.MODE_SOLVE,
            source=AIRequestLog.SOURCE_WEBSOCKET,
            status=AIRequestLog.STATUS_SUCCESS,
            model_names=["Ollama Gemma"],
            tokens=10,
            tokens_prompt=5,
        )
        report = collect_log_slices(AIRequestLog.objects.all(), mode="chat")
        self.assertEqual(report["total"]["total_tokens"], 140)
        report = collect_log_slices(AIRequestLog.objects.all(), mode="solve")
        self.assertEqual(report["total"]["total_tokens"], 15)

    def test_batch_pairs_slice(self):
        from ai.models import AIModelTestResult, AIModelTestRun
        run = AIModelTestRun.objects.create(
            run_id=uuid.uuid4().hex,
            run_type=AIModelTestRun.RUN_TYPE_BATCH,
            started_at=self.now,
        )
        AIModelTestResult.objects.create(
            run=run,
            model_key="M1",
            model_title="Ollama Gemma",
            status=AIModelTestResult.STATUS_OK,
            verdict=AIModelTestResult.VERDICT_SOLVED,
            tokens=50,
            tokens_prompt=20,
        )
        from ai.services.token_usage import collect_batch_slices
        report = collect_batch_slices(AIModelTestRun.objects.all())
        self.assertEqual(report["total"]["total_tokens"], 70)
        self.assertEqual(report["pairs"], 1)


class MskDateRangeTests(SimpleTestCase):
    """Границы периодов по МСК (единый хелпер ai/services/msk_time.py)."""

    def test_parse_date_formats(self):
        from ai.services.msk_time import parse_date
        self.assertIsNotNone(parse_date("2026-10-20"))
        self.assertIsNotNone(parse_date("20.10.2026"))
        self.assertIsNone(parse_date("мусор"))
        self.assertIsNone(parse_date(""))

    def test_day_bounds_are_msk_midnights_in_utc(self):
        from datetime import datetime, timezone as dt_timezone
        from ai.constants import MOSCOW_TZ
        from ai.services.msk_time import msk_day_bounds
        start, end = msk_day_bounds(datetime(2026, 10, 20, 15, 30, tzinfo=MOSCOW_TZ))
        # Границы отдаются в UTC (sent_at в БД хранится в UTC), но это
        # ПОЛНОЧИ МСК: 00:00 MSK = 21:00 UTC предыдущего дня.
        self.assertEqual(start.tzinfo, dt_timezone.utc)
        self.assertEqual(end.tzinfo, dt_timezone.utc)
        self.assertEqual(start.hour, 21)
        self.assertEqual(start.minute, 0)
        self.assertEqual(end.hour, 20)
        self.assertEqual(end.minute, 59)
        # Обратно в МСК — ровно полночь/конец суток.
        self.assertEqual(start.astimezone(MOSCOW_TZ).hour, 0)
        self.assertEqual(end.astimezone(MOSCOW_TZ).hour, 23)


class UserModelSecurityTests(SimpleTestCase):
    """SSRF-защита поля «адрес модели» (владелец просил защиту от атак)."""

    def test_http_is_rejected(self):
        from ai.services.user_models import ERR_NOT_HTTPS, validate_base_url
        _, error = validate_base_url("http://api.example.com/v1")
        self.assertEqual(error, ERR_NOT_HTTPS)

    def test_localhost_is_rejected(self):
        from ai.services.user_models import ERR_PRIVATE_HOST, validate_base_url
        _, error = validate_base_url("https://localhost/v1")
        self.assertEqual(error, ERR_PRIVATE_HOST)

    def test_private_ip_literal_is_rejected(self):
        from ai.services.user_models import ERR_PRIVATE_HOST, validate_base_url
        for url in ("https://127.0.0.1/v1", "https://10.0.0.5/v1",
                    "https://192.168.1.1/v1", "https://169.254.169.254/latest"):
            _, error = validate_base_url(url)
            self.assertEqual(error, ERR_PRIVATE_HOST, url)

    def test_credentials_in_url_are_rejected(self):
        from ai.services.user_models import ERR_CREDENTIALS_IN_URL, validate_base_url
        _, error = validate_base_url("https://user:pass@api.example.com/v1")
        self.assertEqual(error, ERR_CREDENTIALS_IN_URL)

    def test_non_standard_port_is_rejected(self):
        from ai.services.user_models import ERR_BAD_PORT, validate_base_url
        _, error = validate_base_url("https://api.example.com:8080/v1")
        self.assertEqual(error, ERR_BAD_PORT)

    def test_https_public_url_passes(self):
        from ai.services.user_models import validate_base_url
        url, error = validate_base_url("https://api.openai.com/v1")
        self.assertEqual(error, "")
        self.assertEqual(url, "https://api.openai.com/v1")

    def test_dns_rebinding_to_localhost_is_rejected(self):
        # Домен резолвится в приватный адрес — такой endpoint запрещён
        # (иначе «свой» домен → 127.0.0.1 обходит фильтр по литералу).
        from ai.services.user_models import ERR_PRIVATE_HOST, validate_base_url
        from unittest.mock import patch
        import socket
        fake = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]
        with patch("ai.services.user_models.socket.getaddrinfo", return_value=fake):
            _, error = validate_base_url("https://my-own-domain.example/v1")
        self.assertEqual(error, ERR_PRIVATE_HOST)

    def test_default_base_urls_are_https(self):
        from ai.services.user_models import PROVIDER_BASE_URLS
        for provider, url in PROVIDER_BASE_URLS.items():
            self.assertTrue(url.startswith("https://"), provider)


class UserModelIsolationTests(TestCase):
    """Личные модели видны ТОЛЬКО владельцу."""

    def setUp(self):
        from ai.models import AIUserModel
        self.user_model = get_user_model()
        self.owner = self.user_model.objects.create_user(username="um-owner", password="x")
        self.other = self.user_model.objects.create_user(username="um-other", password="x")
        self.model = AIUserModel.objects.create(
            user=self.owner,
            title="Моя модель",
            provider=AIUserModel.PROVIDER_OPENAI,
            model_id="gpt-4o-mini",
            base_url="https://api.openai.com/v1",
        )

    def test_options_only_for_owner(self):
        from ai.services.user_models import user_model_options
        self.assertEqual(len(user_model_options(self.owner)), 1)
        self.assertEqual(user_model_options(self.other), [])

    def test_find_user_model_is_owner_scoped(self):
        from ai.services.user_models import find_user_model
        key = f"User_{self.model.pk}"
        self.assertIsNotNone(find_user_model(self.owner, key))
        # Чужой пользователь ключ НЕ находит — вызов чужой модели невозможен.
        self.assertIsNone(find_user_model(self.other, key))



class UserModelCallerIsolationTests(TransactionTestCase):
    """ModelCaller не даёт вызвать ЧУЖУЮ личную модель.

    Отдельный TransactionTestCase: поиск модели идёт из другого потока
    (sync_to_async), а sqlite в TestCase держит транзакцию — таблица
    заблокирована для чужого соединения.
    """

    def test_foreign_key_is_rejected(self):
        import asyncio
        from ai.models import AIUserModel
        from ai.services.model_caller import ModelCaller
        user_model = get_user_model()
        owner = user_model.objects.create_user(username="mc-owner", password="x")
        other = user_model.objects.create_user(username="mc-other", password="x")
        model = AIUserModel.objects.create(
            user=owner, title="Моя", provider=AIUserModel.PROVIDER_OPENAI, model_id="m",
        )
        key = f"User_{model.pk}"
        result = asyncio.run(
            ModelCaller().call("привет", "client-1", key, language="ru", user=other)
        )
        self.assertTrue(result.is_error)
        self.assertIn(key, result.response_text)


class UserModelsAdminViewTests(_AdminViewRequestMixin, TestCase):
    """Страница «Мои AI-модели»: создание/удаление только своих записей."""

    def setUp(self):
        self.factory = RequestFactory()
        self.user_model = get_user_model()
        self.user = self.user_model.objects.create_user(username="um-admin", password="x")
        # Право на страницу «Мои AI-модели» (page.user_models) выдаём
        # группой доступа — ровно так, как это делает прод-миграция 0067.
        group = self._view_as_group("UM-группа", ["page.user_models"])
        group.users.add(self.user)
        from ai.models import AIUserModel
        self.foreign = AIUserModel.objects.create(
            user=self.user_model.objects.create_user(username="um-foreign", password="x"),
            title="Чужая",
            provider=AIUserModel.PROVIDER_OPENAI,
            model_id="m",
        )

    def _post(self, path, payload):
        """POST на create/delete — вьюхи вызываются напрямую.

        ai.admin.urls отдаёт get_ai_admin_urls() (без module-level
        urlpatterns), поэтому маршрут резолвим по имени view-функции.
        """
        request = self._admin_request(
            self.user, method="post", path=path,
            data=json.dumps(payload), content_type="application/json",
        )
        if path.endswith("/create/"):
            from ai.admin.user_models import admin_user_model_create_view
            return admin_user_model_create_view(request)
        from ai.admin.user_models import admin_user_model_delete_view
        return admin_user_model_delete_view(request)

    def test_list_shows_only_own_models(self):
        from ai.models import AIUserModel
        mine = AIUserModel.objects.create(
            user=self.user, title="Своя", provider=AIUserModel.PROVIDER_OPENAI, model_id="m",
        )
        request = self._admin_request(self.user, path="/ai/admin/user-models/")
        from ai.admin.user_models import admin_user_models_view
        response = admin_user_models_view(request)
        ids = [row["id"] for row in response.context_data["user_models"]]
        self.assertIn(mine.pk, ids)
        self.assertNotIn(self.foreign.pk, ids)

    def test_cannot_update_foreign_model(self):
        response = self._post("/ai/admin/user-models/create/", {
            "id": self.foreign.pk,
            "title": "Взлом",
            "provider": "openai",
            "model_id": "x",
        })
        self.assertEqual(response.status_code, 404)

    def test_cannot_delete_foreign_model(self):
        response = self._post("/ai/admin/user-models/delete/", {"id": self.foreign.pk})
        self.assertEqual(response.status_code, 404)
        from ai.models import AIUserModel
        self.assertTrue(AIUserModel.objects.filter(pk=self.foreign.pk).exists())

    def test_create_rejects_private_base_url(self):
        response = self._post("/ai/admin/user-models/create/", {
            "title": "Локальная",
            "provider": "custom",
            "model_id": "m",
            "base_url": "https://127.0.0.1/v1",
        })
        self.assertEqual(response.status_code, 400)


class MyModelsClientViewTests(TestCase):
    """Клиентская страница «Мои AI-модели» (/ai/my-models/).

    Обычный пользователь (не сотрудник админки) должен иметь доступ к
    своим моделям: страница клиентская (/ai/my-models/), гейт — как у
    страниц чата (_has_page_access), а не админский.
    """

    def setUp(self):
        self.factory = RequestFactory()
        self.user_model = get_user_model()
        self.user = self.user_model.objects.create_user(username="mm-user", password="x")

    def _client_request(self, user, method="get", path="/ai/my-models/", body=None):
        """Запрос клиентской страницы: тело — сырой JSON (content_type)."""
        if body is None:
            request = getattr(self.factory, method)(path)
        else:
            request = getattr(self.factory, method)(
                path, data=body, content_type="application/json",
            )
        request.user = user
        request.session = {}
        request.COOKIES = {}
        return request

    def test_page_renders_for_ordinary_user(self):
        from ai.views import my_models_view
        request = self._client_request(self.user)
        with patch("ai.views.get_external_user_id_from_request", return_value="777"):
            response = my_models_view(request)
        self.assertEqual(response.status_code, 200)

    def test_page_forbidden_without_external_id(self):
        from ai.views import my_models_view
        request = self._client_request(self.user)
        with patch("ai.views.get_external_user_id_from_request", return_value=""):
            response = my_models_view(request)
        self.assertEqual(response.status_code, 403)

    def test_save_creates_own_model(self):
        from ai.views import my_model_save_view
        request = self._client_request(
            self.user, method="post", path="/ai/my-models/create/",
            body=json.dumps({
                "title": "Моя модель",
                "provider": "openai",
                "model_id": "gpt-4o-mini",
            }),
        )
        with patch("ai.views.get_external_user_id_from_request", return_value="777"):
            response = my_model_save_view(request)
        self.assertEqual(response.status_code, 200)
        from ai.models import AIUserModel
        self.assertTrue(
            AIUserModel.objects.filter(user=self.user, model_id="gpt-4o-mini").exists()
        )

    def test_cannot_touch_foreign_model(self):
        from ai.models import AIUserModel
        from ai.views import my_model_delete_view
        foreign = AIUserModel.objects.create(
            user=self.user_model.objects.create_user(username="mm-other", password="x"),
            title="Чужая", provider=AIUserModel.PROVIDER_OPENAI, model_id="m",
        )
        request = self._client_request(
            self.user, method="post", path="/ai/my-models/delete/",
            body=json.dumps({"id": foreign.pk}),
        )
        with patch("ai.views.get_external_user_id_from_request", return_value="777"):
            response = my_model_delete_view(request)
        self.assertEqual(response.status_code, 404)
        self.assertTrue(AIUserModel.objects.filter(pk=foreign.pk).exists())


class DLApiRequestsTests(_AdminViewRequestMixin, TestCase):
    """Страница «Запросы по DL API»: вкладки «актуально/неактуально»."""

    def setUp(self):
        self.factory = RequestFactory()
        self.user_model = get_user_model()
        self.user = self.user_model.objects.create_user(
            username="dl-api-admin", password="x", is_staff=True,
        )
        from ai.models import AIDLApiRequest
        self.pending = AIDLApiRequest.objects.create(
            description="Нужен API к задачам",
            user=self.user,
            user_full_name="Петров Пётр",
            username="dl-api-admin",
        )
        self.done = AIDLApiRequest.objects.create(
            description="Уже сделано",
            user=self.user,
            status=AIDLApiRequest.STATUS_DONE,
        )

    def _get(self, path):
        from ai.admin.dl_api_requests import admin_dl_api_requests_view
        request = self._admin_request(self.user, path=path)
        return admin_dl_api_requests_view(request)

    def test_tabs_split_pending_and_done(self):
        from ai.models import AIDLApiRequest
        pending = self._get("/ai/admin/dl-api-requests/?tab=pending")
        done = self._get("/ai/admin/dl-api-requests/?tab=done")
        pending_ids = [row["id"] for row in pending.context_data["rows"]]
        done_ids = [row["id"] for row in done.context_data["rows"]]
        self.assertIn(self.pending.pk, pending_ids)
        self.assertNotIn(self.done.pk, pending_ids)
        self.assertIn(self.done.pk, done_ids)

    def test_row_has_description_who_and_when(self):
        response = self._get("/ai/admin/dl-api-requests/?tab=pending")
        row = response.context_data["rows"][0]
        self.assertEqual(row["description"], "Нужен API к задачам")
        self.assertEqual(row["who"], "Петров Пётр")
        self.assertTrue(row["created_label"])

    def test_mark_done_moves_row_between_tabs(self):
        import json as _json
        from ai.admin.dl_api_requests import admin_dl_api_request_update_view
        request = self._admin_request(
            self.user, method="post", path="/ai/admin/dl-api-requests/update/",
            data=_json.dumps({"id": self.pending.pk, "status": "done", "comment": "создано"}),
            content_type="application/json",
        )
        response = admin_dl_api_request_update_view(request, self.pending.pk) if False else admin_dl_api_request_update_view(request)
        self.assertEqual(response.status_code, 200)
        self.pending.refresh_from_db()
        self.assertEqual(self.pending.status, "done")
        self.assertIsNotNone(self.pending.resolved_at)
        self.assertEqual(self.pending.resolved_by_id, self.user.pk)


class DLApiRequestCreateTests(_AdminViewRequestMixin, TestCase):
    """Создание заявки на API к DL: форма в админке (POST JSON)."""

    def setUp(self):
        self.factory = RequestFactory()
        self.user_model = get_user_model()
        self.user = self.user_model.objects.create_user(
            username="dl-api-creator", password="x", is_staff=True,
        )

    def _post(self, payload, user=None):
        import json as _json
        from ai.admin.dl_api_requests import admin_dl_api_request_create_view
        request = self._admin_request(
            user or self.user, method="post", path="/ai/admin/dl-api-requests/create/",
            data=_json.dumps(payload), content_type="application/json",
        )
        return admin_dl_api_request_create_view(request)

    def test_create_makes_pending_request(self):
        import json as _json
        from ai.models import AIDLApiRequest
        response = self._post({"description": "Нужен API к списку задач"})
        self.assertEqual(response.status_code, 200)
        payload = _json.loads(response.content)
        self.assertTrue(payload["ok"])
        item = AIDLApiRequest.objects.get(pk=payload["id"])
        self.assertEqual(item.description, "Нужен API к списку задач")
        self.assertEqual(item.status, AIDLApiRequest.STATUS_PENDING)
        self.assertEqual(item.user_id, self.user.pk)
        self.assertEqual(item.username, "dl-api-creator")

    def test_create_requires_description(self):
        import json as _json
        from ai.models import AIDLApiRequest
        before = AIDLApiRequest.objects.count()
        response = self._post({"description": "   "})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(_json.loads(response.content)["ok"])
        self.assertEqual(AIDLApiRequest.objects.count(), before)

    def test_create_without_permission_is_forbidden(self):
        """Без прав на страницу (page.dl_api_requests) — 403, заявка не создана."""
        from ai.models import AIDLApiRequest
        before = AIDLApiRequest.objects.count()
        outsider = self.user_model.objects.create_user(username="outsider", password="x")
        response = self._post({"description": "Хочу API"}, user=outsider)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(AIDLApiRequest.objects.count(), before)

    def test_create_page_has_form(self):
        """На странице есть форма создания заявки (описание + кнопка)."""
        from django.template.loader import get_template
        src = get_template("admin/ai/dl_api_requests.html").template.source
        self.assertIn('id="ai-dlapi-create-form"', src)
        self.assertIn('id="ai-dlapi-description"', src)
        self.assertIn("ai-dlapi-create-submit", src)
        self.assertIn('data-create-url="/ai/admin/dl-api-requests/create/"', src)


class TokenUsageDateFilterTests(TestCase):
    """Фильтр по датам на «Контроле токенов» — регрессия.

    Раньше границы считались как ``date.astimezone(timezone.utc)``:
    * ``django.utils.timezone.utc`` удалён в Django 5.0 → AttributeError → 500
      на ЛЮБОМ фильтре по датам;
    * ``date_to`` превращался в полночь выбранного дня → записи ПОСЛЕ
      00:00 МСК отсекались (потеря целого дня).
    Теперь границы — через ``msk_day_bounds`` (единый владелец).
    """

    def setUp(self):
        self.now = timezone.now()

    def _log(self, *, sent_at, tokens=10, tokens_prompt=5, names=None):
        return AIRequestLog.objects.create(
            sent_at=sent_at,
            mode=AIRequestLog.MODE_CHAT,
            source=AIRequestLog.SOURCE_WEBSOCKET,
            status=AIRequestLog.STATUS_SUCCESS,
            model_names=names or ["Ollama Gemma"],
            tokens=tokens,
            tokens_prompt=tokens_prompt,
            user_full_name="Иванов Иван",
        )

    def test_date_filters_do_not_raise(self):
        from ai.services.msk_time import parse_date
        from ai.services.token_usage import collect_log_slices
        # Регрессия: раньше AttributeError («datetime.date» object has no
        # attribute «astimezone») — страница отдавала 500.
        report = collect_log_slices(
            AIRequestLog.objects.all(),
            date_from=parse_date("2026-10-01"),
            date_to=parse_date("2026-10-20"),
        )
        self.assertIn("total", report)

    def test_date_to_covers_whole_msk_day(self):
        from ai.services.msk_time import parse_date
        from ai.services.token_usage import collect_log_slices
        from ai.constants import MOSCOW_TZ
        # Запись в 23:30 МСК выбранного дня — попадает в диапазон.
        late = datetime(2026, 10, 20, 23, 30, tzinfo=MOSCOW_TZ)
        self._log(sent_at=late, tokens=100, tokens_prompt=40)
        report = collect_log_slices(
            AIRequestLog.objects.all(),
            date_from=parse_date("2026-10-20"),
            date_to=parse_date("2026-10-20"),
        )
        self.assertEqual(report["total"]["total_tokens"], 140)

    def test_date_range_excludes_outside_rows(self):
        from ai.services.msk_time import parse_date
        from ai.services.token_usage import collect_log_slices
        from ai.constants import MOSCOW_TZ
        inside = datetime(2026, 10, 20, 12, 0, tzinfo=MOSCOW_TZ)
        before = datetime(2026, 10, 19, 23, 59, tzinfo=MOSCOW_TZ)
        after = datetime(2026, 10, 21, 0, 1, tzinfo=MOSCOW_TZ)
        self._log(sent_at=inside, tokens=10, tokens_prompt=5)
        self._log(sent_at=before, tokens=1000, tokens_prompt=1000)
        self._log(sent_at=after, tokens=2000, tokens_prompt=2000)
        report = collect_log_slices(
            AIRequestLog.objects.all(),
            date_from=parse_date("2026-10-20"),
            date_to=parse_date("2026-10-20"),
        )
        self.assertEqual(report["total"]["total_tokens"], 15)

    def test_batch_pairs_respect_date_range(self):
        from ai.models import AIModelTestResult, AIModelTestRun
        from ai.services.msk_time import parse_date
        from ai.services.token_usage import collect_batch_slices
        from ai.constants import MOSCOW_TZ
        inside = datetime(2026, 10, 20, 12, 0, tzinfo=MOSCOW_TZ)
        outside = datetime(2026, 9, 1, 12, 0, tzinfo=MOSCOW_TZ)
        for started in (inside, outside):
            run = AIModelTestRun.objects.create(
                run_id=uuid.uuid4().hex,
                run_type=AIModelTestRun.RUN_TYPE_BATCH,
                started_at=started,
            )
            AIModelTestResult.objects.create(
                run=run,
                model_key="M1",
                model_title="Ollama Gemma",
                status=AIModelTestResult.STATUS_OK,
                verdict=AIModelTestResult.VERDICT_SOLVED,
                tokens=50,
                tokens_prompt=20,
            )
        report = collect_batch_slices(
            AIModelTestRun.objects.all(),
            date_from=parse_date("2026-10-20"),
            date_to=parse_date("2026-10-20"),
        )
        self.assertEqual(report["total"]["total_tokens"], 70)
        self.assertEqual(report["pairs"], 1)


class TokenUsageMultiModelTests(TestCase):
    """Сводная запись прогона (несколько моделей) — регрессия.

    Токены такой записи — СУММА по всем моделям прогона. Раньше сумма
    прибавлялась к срезу «по моделям» ПО ОДНОМУ РАЗУ НА КАЖДОЕ ИМЯ —
    расход завышался в N раз, и каждой модели приписывался весь расход
    прогона.
    """

    def setUp(self):
        self.now = timezone.now()
        AIRequestLog.objects.create(
            sent_at=self.now,
            mode=AIRequestLog.MODE_BATCH_SOLVE,
            source=AIRequestLog.SOURCE_ARM,
            status=AIRequestLog.STATUS_SUCCESS,
            model_names=["Ollama Gemma", "OpenRouter Qwen", "Groq Llama"],
            tokens=300,
            tokens_prompt=100,
            user_full_name="Иванов Иван",
        )

    def test_total_is_not_inflated(self):
        from ai.services.token_usage import collect_log_slices
        report = collect_log_slices(AIRequestLog.objects.all())
        # 300 + 100, а НЕ 3 × (300 + 100).
        self.assertEqual(report["total"]["total_tokens"], 400)
        self.assertEqual(report["total"]["requests"], 1)

    def test_model_slice_sums_to_total(self):
        from ai.services.token_usage import collect_log_slices
        report = collect_log_slices(AIRequestLog.objects.all())
        by_model_sum = sum(row["total_tokens"] for row in report["by_model"])
        self.assertEqual(by_model_sum, report["total"]["total_tokens"])
        # Ни одна ОТДЕЛЬНАЯ модель не должна получить весь расход прогона:
        # строка среза — это набор моделей прогона, а не одна модель.
        for row in report["by_model"]:
            self.assertNotIn(row["label"], ("Ollama Gemma", "OpenRouter Qwen", "Groq Llama"))
        # Модели прогона перечислены в строке.
        labels = " ".join(row["label"] for row in report["by_model"])
        self.assertIn("Groq Llama", labels)
        self.assertIn("Ollama Gemma", labels)

    def test_model_filter_does_not_credit_one_model(self):
        from ai.services.token_usage import (
            MULTI_MODEL_LABEL,
            collect_log_slices,
        )
        report = collect_log_slices(AIRequestLog.objects.all(), model="Groq")
        # Запись проходит фильтр (в прогоне есть Groq), но её расход не
        # приписывается Groq — иначе счётчик модели завышен в N раз.
        self.assertEqual(report["total"]["total_tokens"], 400)
        labels = [row["label"] for row in report["by_model"]]
        self.assertNotIn("Groq Llama", labels)
        # Расход прогона попадает в отдельную «корзину», а не в счётчик
        # одной из его моделей.
        self.assertIn(MULTI_MODEL_LABEL, labels)

    def test_user_and_day_slices_keep_full_sum(self):
        from ai.services.token_usage import collect_log_slices
        report = collect_log_slices(AIRequestLog.objects.all())
        self.assertEqual(report["by_user"][0]["total_tokens"], 400)
        self.assertEqual(report["by_day"][0]["total_tokens"], 400)
        self.assertEqual(report["by_mode"][0]["total_tokens"], 400)


class TokenUsageViewTests(_AdminViewRequestMixin, TestCase):
    """Страница «Контроль токенов»: рендер, фильтры, права."""

    def setUp(self):
        self.factory = RequestFactory()
        self.user_model = get_user_model()
        self.user = self.user_model.objects.create_user(
            username="tokens-admin", password="x", is_staff=True,
        )
        AIRequestLog.objects.create(
            sent_at=timezone.now(),
            mode=AIRequestLog.MODE_CHAT,
            source=AIRequestLog.SOURCE_WEBSOCKET,
            status=AIRequestLog.STATUS_SUCCESS,
            model_names=["Ollama Gemma"],
            tokens=100,
            tokens_prompt=40,
            user_full_name="Иванов Иван",
        )

    def _get(self, query="", user=None):
        from ai.admin.tokens import admin_token_usage_view
        path = "/ai/admin/ai/tokens/" + query
        request = self._admin_request(user or self.user, path=path)
        with patch("ai.admin.site.get_external_user_id_from_request", return_value="12345"):
            return admin_token_usage_view(request)

    def test_page_renders(self):
        response = self._get("")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Контроль токенов", response.content.decode("utf-8"))

    def test_date_filter_does_not_break_page(self):
        # Регрессия: фильтр по датам ронял страницу в 500.
        response = self._get("?date_from=2026-10-01&date_to=2026-10-20")
        self.assertEqual(response.status_code, 200)

    def test_bad_date_is_ignored(self):
        response = self._get("?date_from=мусор")
        self.assertEqual(response.status_code, 200)

    def test_non_staff_without_key_is_forbidden(self):
        # Обычный юзер (не staff) без ключа page.tokens — 403.
        from ai.admin.permissions import can_access_token_usage
        plain = self.user_model.objects.create_user(username="plain-user", password="x")
        request = self._admin_request(plain, path="/ai/admin/ai/tokens/")
        self.assertFalse(can_access_token_usage(request))

    def test_staff_has_access(self):
        from ai.admin.permissions import can_access_token_usage
        request = self._admin_request(self.user, path="/ai/admin/ai/tokens/")
        self.assertTrue(can_access_token_usage(request))

    def test_key_holder_has_access(self):
        from ai.admin.permissions import can_access_token_usage
        from ai.models import AIAccessGroup, AIAdminGroupPermission
        plain = self.user_model.objects.create_user(username="key-user", password="x")
        perm, _ = AIAdminGroupPermission.objects.get_or_create(
            key="page.tokens", defaults={"label": "page.tokens", "section": "page"},
        )
        group = AIAccessGroup.objects.create(name="Контроль токенов")
        group.permissions.add(perm)
        group.users.add(plain)
        request = self._admin_request(plain, path="/ai/admin/ai/tokens/")
        self.assertTrue(can_access_token_usage(request))


class FailureReasonEdgeCaseTests(SimpleTestCase):
    """Граничные случаи классификатора причин сбоя (регрессия)."""

    def test_successful_compile_is_not_compile_error(self):
        from ai.dl_api_client import dl_failure_reason_from_comment
        # Регрессия: «Компиляция успешна, все тесты пройдены» — это УСПЕХ,
        # а не «ошибка компиляции» (маркер «компиляц» срабатывал и на успехе).
        self.assertEqual(
            dl_failure_reason_from_comment("Компиляция успешна, все тесты пройдены"),
            "",
        )

    def test_successful_build_with_wrong_answer(self):
        from ai.dl_api_client import dl_failure_reason_from_comment
        # Сборка прошла, ответ неверный — это «неправильный ответ», а не
        # «ошибка компиляции».
        self.assertEqual(
            dl_failure_reason_from_comment("Сборка прошла успешно, но ответ неверный"),
            "wrong_answer",
        )
        self.assertEqual(
            dl_failure_reason_from_comment(
                "Компиляция прошла, ответ не совпадает с ожидаемым"
            ),
            "wrong_answer",
        )

    def test_real_compile_error_still_detected(self):
        from ai.dl_api_client import dl_failure_reason_from_comment
        self.assertEqual(
            dl_failure_reason_from_comment("Ошибка компиляции в строке 5"),
            "compile_error",
        )
        self.assertEqual(
            dl_failure_reason_from_comment("syntax error at line 3"),
            "compile_error",
        )
        self.assertEqual(
            dl_failure_reason_from_comment("Программа не компилируется"),
            "compile_error",
        )

    def test_wrong_variable_is_compile_error(self):
        """«Не правильная переменная» — это ОШИБКА КОМПИЛЯЦИИ.

        Регрессия: маркер «неправильн» из DL_WRONG_ANSWER_MARKERS матчился
        в «Неправильная переменная» → пара уезжала в «неправильный ответ».
        """
        from ai.dl_api_client import dl_failure_reason_from_comment
        self.assertEqual(
            dl_failure_reason_from_comment("Неправильная переменная 'x' не объявлена"),
            "compile_error",
        )
        self.assertEqual(
            dl_failure_reason_from_comment("Не правильная переменная"),
            "compile_error",
        )
        self.assertEqual(
            dl_failure_reason_from_comment("Переменная x не объявлена"),
            "compile_error",
        )
        self.assertEqual(
            dl_failure_reason_from_comment("Ошибка: неизвестный идентификатор foo"),
            "compile_error",
        )
        self.assertEqual(
            dl_failure_reason_from_comment("undeclared identifier 'y'"),
            "compile_error",
        )

    def test_wrong_answer_with_variable_word_stays_wrong_answer(self):
        """«Неправильный ответ: значение переменной …» — это НЕ компиляция.

        Регрессия к over-fix: слово «переменная» в комментарии про ответ
        не должно переводить причину в compile_error.
        """
        from ai.dl_api_client import dl_failure_reason_from_comment
        self.assertEqual(
            dl_failure_reason_from_comment(
                "Неправильный ответ: значение переменной не равно ожидаемому"
            ),
            "wrong_answer",
        )
        self.assertEqual(
            dl_failure_reason_from_comment("Неверный ответ на тесте 2"),
            "wrong_answer",
        )


class FailureReasonBackfillNoCodeTests(TestCase):
    """Бэкфилл причин: «не выкушен код» должен восстанавливаться."""

    def setUp(self):
        from ai.models import AIModelTestRun
        self.run = AIModelTestRun.objects.create(
            run_id=uuid.uuid4().hex,
            run_type=AIModelTestRun.RUN_TYPE_BATCH,
            started_at=timezone.now(),
        )
        self.single_run = AIModelTestRun.objects.create(
            run_id=uuid.uuid4().hex,
            run_type=AIModelTestRun.RUN_TYPE_SINGLE,
            started_at=timezone.now(),
        )

    def _result(self, **kwargs):
        from ai.models import AIModelTestResult
        defaults = {
            "run": self.run,
            "model_key": "M1",
            "model_title": "Model 1",
            "status": AIModelTestResult.STATUS_ERROR,
            "verdict": AIModelTestResult.VERDICT_FAILED,
        }
        defaults.update(kwargs)
        return AIModelTestResult.objects.create(**defaults)

    def test_no_code_service_comment_restored(self):
        from io import StringIO
        from django.core.management import call_command
        row = self._result(
            dl_comment="Модель не успела вывести код — ответ оборван лимитом генерации",
            code="",
        )
        out = StringIO()
        call_command("backfill_arm_failure_reasons", "--commit", stdout=out)
        row.refresh_from_db()
        # Регрессия: по служебному комментарию раннера общий классификатор
        # давал «причина не распознана» — целая категория терялась.
        self.assertEqual(row.failure_reason, "no_code")

    def test_junk_code_comment_restored(self):
        from io import StringIO
        from django.core.management import call_command
        row = self._result(
            dl_comment=(
                "Извлечённый текст не похож на программу языка курса "
                "(нет структурных маркеров) — DL не тестировал"
            ),
            code="",
        )
        out = StringIO()
        call_command("backfill_arm_failure_reasons", "--commit", stdout=out)
        row.refresh_from_db()
        self.assertEqual(row.failure_reason, "no_code")

    def test_single_run_is_not_touched(self):
        from io import StringIO
        from django.core.management import call_command
        from ai.models import AIModelTestResult
        row = AIModelTestResult.objects.create(
            run=self.single_run,
            model_key="M1",
            model_title="Model 1",
            status=AIModelTestResult.STATUS_ERROR,
            verdict=None,
            code="",
            dl_comment="",
        )
        out = StringIO()
        call_command("backfill_arm_failure_reasons", "--commit", stdout=out)
        row.refresh_from_db()
        # У одиночных прогонов вердикта нет by design — пара не тестировалась
        # на DL, «сбой ответа модели» был бы неверным основанием.
        self.assertEqual(row.failure_reason, "")


class ArmSolveFailureReasonsRenderTests(TestCase):
    """Секция «Сбои по причинам» должна рендериться на /arm/solve/.

    Регрессия: на странице пакетного решения не было контейнера
    reasonsWrap, и renderFailureReasons молча ничего не рисовала —
    таксономия сбоев была видна только в журнале и настройках.
    """

    def test_arm_solve_template_has_reasons_container(self):
        from pathlib import Path
        template = Path("ai/templates/admin/ai/arm_solve.html").read_text(encoding="utf-8")
        self.assertIn("armSolveReasonsWrap", template)

    def test_arm_solve_js_passes_reasons_element(self):
        from pathlib import Path
        js = Path("static/admin/ai/arm_solve/index.js").read_text(encoding="utf-8")
        self.assertIn("armSolveReasonsWrap", js)
        self.assertIn("reasons: reasonsWrap", js)

    def test_batch_results_js_has_failure_reasons_render(self):
        from pathlib import Path
        js = Path("static/admin/ai/batch_results/index.js").read_text(encoding="utf-8")
        self.assertIn("renderFailureReasons", js)
        self.assertIn("failure_reasons", js)

    def test_reason_badge_has_css_class(self):
        from pathlib import Path
        css = Path("static/admin/ai/batch_results/index.css").read_text(encoding="utf-8")
        self.assertIn(".arm-reason", css)


class ArmBatchFailureReasonRetryTests(TransactionTestCase):
    """Ветка «ретрай упал с ошибкой модели» → причина «сбой ответа модели».

    Регрессия: пара, упавшая с 500/429 на ретрае, классифицировалась как
    «не выкушен код» — таксономия врала (код не получен из-за СБОЯ модели).
    """

    def setUp(self):
        from ai.models import Task
        self.user = get_user_model().objects.create_user(username="retry-reason", password="x")
        self.lang = ProgrammingLanguage.objects.create(language_name="Pascal")
        self.topic = Topic.objects.create(
            topic_name_ru="Ретраи", programming_language=self.lang,
        )
        self.task = Task.objects.create(
            node_id=9301, task_id=9401, name="A", statement="Сложите a и b",
            topic=self.topic, programming_language=self.lang, file_extension=".pas",
        )

    def _seed_job(self, run_id, total_pairs):
        from ai import arm_runner
        now_ts = time.time()
        arm_runner._jobs[run_id] = {
            "run_id": run_id, "run_type": "batch", "status": "running",
            "error_message": "", "total_models": 1, "total_pairs": total_pairs,
            "completed_pairs": 0, "completed_models": 0,
            "current_model_key": "", "current_model_title": "",
            "current_task_node_id": "", "current_task_name": "",
            "model_progress": [], "results": [], "report": None,
            "created_at_ts": now_ts, "updated_at_ts": now_ts,
        }

    def _run(self, handler):
        from ai import arm_runner
        from ai.models import AIModelTestResult, AIModelTestRun
        run_id = "rr-" + uuid.uuid4().hex[:8]
        self._seed_job(run_id, 1)
        models = [{"key": "M1", "title": "Model 1", "handler": handler}]
        try:
            arm_runner._run_batch_job_worker(
                run_id, [self.task.node_id], models, self.user.id, "DLSID-1",
                ui_language="Русский", dl_test=True,
            )
        finally:
            arm_runner._jobs.pop(run_id, None)
        run = AIModelTestRun.objects.get(run_id=run_id)
        return AIModelTestResult.objects.get(run=run, model_key="M1")

    def test_retry_model_error_gives_model_error(self):
        calls = {"n": 0}

        async def handler(messages, conv_id):
            calls["n"] += 1
            if calls["n"] == 1:
                # Первый ответ — пустой/обрезанный: кода не извлечётся → ретрай.
                return ("", 0, False, True, 0)
            # Ретрай — ошибка провайдера (429/500).
            return ("Ошибка 429: превышен лимит запросов", 0, True, False, 0)

        row = self._run(handler)
        self.assertEqual(row.failure_reason, "model_error")

    def test_retry_without_code_gives_no_code(self):
        calls = {"n": 0}

        async def handler(messages, conv_id):
            calls["n"] += 1
            if calls["n"] == 1:
                return ("частичный ответ без кода", 5000, False, True, 100)
            # Ретрай — модель ответила, но кода всё равно нет.
            return ("всё равно без кода", 10, False, False, 10)

        row = self._run(handler)
        self.assertEqual(row.failure_reason, "no_code")


class ModelErrorAutoRetryTests(TransactionTestCase):
    """Автоповтор «Сбой ответа модели» ×5.

    ТЗ пользователя: сбой СНАЧАЛА выводится в журнал прогона, потом запрос
    СРАЗУ пересылается — и так до 5 раз.
    """

    def setUp(self):
        from ai import arm_runner
        self.arm_runner = arm_runner
        self.run_id = "mretry-" + uuid.uuid4().hex[:8]
        now_ts = time.time()
        arm_runner._jobs[self.run_id] = {
            "run_id": self.run_id, "run_type": "batch", "status": "running",
            "error_message": "", "total_models": 1, "total_pairs": 1,
            "completed_pairs": 0, "completed_models": 0,
            "current_model_key": "", "current_model_title": "",
            "current_task_node_id": "", "current_task_name": "",
            "model_progress": [], "results": [], "report": None,
            "log_lines": [],
            "created_at_ts": now_ts, "updated_at_ts": now_ts,
        }

    def tearDown(self):
        self.arm_runner._jobs.pop(self.run_id, None)

    def _ctx(self):
        return {"run_id": self.run_id, "user_id": 1}

    def _log_lines(self):
        job = self.arm_runner._jobs.get(self.run_id) or {}
        return [str(l.get("text") or "") for l in (job.get("log_lines") or [])]

    def _model(self, handler):
        return {"key": "M1", "title": "Model 1", "handler": handler}

    def test_five_attempts_and_log_lines(self):
        """Всегда сбой → ровно 5 попыток, 4 строки «повтор» + финальная."""
        calls = {"n": 0}

        async def handler(messages, conv_id):
            calls["n"] += 1
            return ("Ошибка 429: превышен лимит запросов", 0, True, False, 0)

        response, _gen = self.arm_runner._call_model_with_retries(
            self._ctx(), self._model(handler), "msg", "cid", "Задача А",
        )
        self.assertEqual(calls["n"], 5)
        self.assertTrue(self.arm_runner._is_model_error(response))
        lines = self._log_lines()
        self.assertEqual(len(lines), 5)
        self.assertIn("повтор 2/5", lines[0])
        self.assertIn("повтор 5/5", lines[3])
        self.assertIn("попыток 5", lines[4])
        self.assertIn("Сбой ответа модели (Model 1)", lines[0])

    def test_failure_logged_before_next_call(self):
        """Сбой печатается ДО следующего вызова модели (порядок ТЗ)."""
        seen = []

        async def handler(messages, conv_id):
            seen.append(list(self._log_lines()))
            return ("Ошибка 500: провайдер упал", 0, True, False, 0)

        self.arm_runner._call_model_with_retries(
            self._ctx(), self._model(handler), "msg", "cid", "Задача Б",
        )
        self.assertEqual(len(seen), 5)
        self.assertEqual(seen[0], [])
        self.assertEqual(len(seen[1]), 1)
        self.assertIn("повтор 2/5", seen[1][0])
        self.assertEqual(len(seen[4]), 4)

    def test_success_after_first_failure(self):
        """Сбой → повтор → успех: 2 вызова, одна строка лога."""
        calls = {"n": 0}

        async def handler(messages, conv_id):
            calls["n"] += 1
            if calls["n"] == 1:
                return ("Ошибка 503", 0, True, False, 0)
            return ("Готово", 5, False, False, 2)

        response, _gen = self.arm_runner._call_model_with_retries(
            self._ctx(), self._model(handler), "msg", "cid", "Задача В",
        )
        self.assertEqual(calls["n"], 2)
        self.assertFalse(self.arm_runner._is_model_error(response))
        lines = self._log_lines()
        self.assertEqual(len(lines), 1)
        self.assertIn("повтор 2/5", lines[0])

    def test_provider_exception_raised_after_attempts(self):
        """Провайдер бросил исключение → тоже повтор, потом исключение наружу."""
        calls = {"n": 0}

        async def handler(messages, conv_id):
            calls["n"] += 1
            raise RuntimeError("провайдер недоступен")

        with self.assertRaises(RuntimeError):
            self.arm_runner._call_model_with_retries(
                self._ctx(), self._model(handler), "msg", "cid", "Задача Г",
            )
        self.assertEqual(calls["n"], 5)
        self.assertEqual(len(self._log_lines()), 5)

    def test_worker_marks_pair_model_error(self):
        """Интеграция: пара с постоянным сбоем модели → вердикт/причина."""
        from ai.models import AIModelTestResult, AIModelTestRun, Task
        calls = {"n": 0}

        async def handler(messages, conv_id):
            calls["n"] += 1
            return ("Ошибка 429: превышен лимит запросов", 0, True, False, 0)

        user = get_user_model().objects.create_user(
            username="mretry-user", password="x",
        )
        lang = ProgrammingLanguage.objects.create(language_name="Pascal")
        topic = Topic.objects.create(
            topic_name_ru="Автоповтор", programming_language=lang,
        )
        task = Task.objects.create(
            node_id=9311, task_id=9411, name="A", statement="Сложите a и b",
            topic=topic, programming_language=lang, file_extension=".pas",
        )
        run_id = "mretry-run-" + uuid.uuid4().hex[:8]
        self.arm_runner._jobs[run_id] = dict(
            self.arm_runner._jobs[self.run_id], run_id=run_id, log_lines=[],
        )
        try:
            self.arm_runner._run_batch_job_worker(
                run_id, [task.node_id], [self._model(handler)], user.id, "DLSID-1",
                ui_language="Русский", dl_test=True,
            )
        finally:
            self.arm_runner._jobs.pop(run_id, None)
        run = AIModelTestRun.objects.get(run_id=run_id)
        row = AIModelTestResult.objects.get(run=run, model_key="M1")
        self.assertEqual(row.failure_reason, "model_error")
        self.assertEqual(row.verdict, "failed")
        self.assertEqual(calls["n"], 5)
        report = run.report or {}
        self.assertTrue((report.get("log_lines") or []))
        self.assertIn("Сбой ответа модели", report["log_lines"][0]["text"])


class CanUseAiGateTests(TestCase):
    """Флаг DL ``canUseAi`` (dl-rest-api.json, UserInfo.canUseAi).

    Правило: флага нет — разрешено («неизвестно» ≠ «запрещено»); явный
    ``False`` — запрещено. Серверный гейт — 403 на страницах чата и закрытие
    WS с кодом 4404 (не молчаливый 4403 «не аутентифицирован»).
    """

    def test_can_use_ai_semantics(self):
        from ai.services.auth import can_use_ai
        # Флага нет / пустой payload / None — разрешено.
        self.assertTrue(can_use_ai(None))
        self.assertTrue(can_use_ai({}))
        self.assertTrue(can_use_ai({"canUseAi": None}))
        self.assertTrue(can_use_ai({"userId": 1}))
        # Явные значения.
        self.assertFalse(can_use_ai({"canUseAi": False}))
        self.assertTrue(can_use_ai({"canUseAi": True}))
        # Строковые формы (в кеше сессии значение может сохраниться строкой).
        self.assertFalse(can_use_ai({"canUseAi": "false"}))
        self.assertFalse(can_use_ai({"canUseAi": "0"}))
        self.assertFalse(can_use_ai({"canUseAi": "no"}))
        self.assertTrue(can_use_ai({"canUseAi": "true"}))

    def test_chat_page_returns_403_with_reason(self):
        """Страница чата при canUseAi=false → 403 с ТЕКСТОМ (не пустая)."""
        from ai.views import chat_view

        factory = RequestFactory()
        request = factory.get("/ai/chat/")
        request.user = get_user_model().objects.create_user(
            username="student-canuseai", password="***")
        request.session = {}
        request.user_info = {"userId": "student", "canUseAi": False}
        request.COOKIES = {"userId": "student"}
        with patch("ai.views.AIAppSettings.get_solo", return_value=SimpleNamespace(is_enabled=True)), \
             patch("ai.views.get_available_model_options", return_value=[]), \
             patch("ai.views.trigger_model_health_refresh_async"):
            response = chat_view(request)
        self.assertEqual(response.status_code, 403)
        body = response.content.decode()
        self.assertIn("запрещено", body)

    def test_chat_page_allowed_without_flag(self):
        """Без флага страница открывается (гейт не блокирует)."""
        from ai.views import chat_view

        factory = RequestFactory()
        request = factory.get("/ai/chat/")
        request.user = get_user_model().objects.create_user(
            username="student-noflag", password="***")
        request.session = {}
        request.user_info = {"userId": "student"}
        request.COOKIES = {"userId": "student"}
        with patch("ai.views.AIAppSettings.get_solo", return_value=SimpleNamespace(is_enabled=True)), \
             patch("ai.views.get_available_model_options", return_value=[]), \
             patch("ai.views.trigger_model_health_refresh_async"), \
             patch("ai.views.render", return_value=HttpResponse("ok")):
            response = chat_view(request)
        self.assertEqual(response.status_code, 200)

    def test_consumer_closes_with_ai_disabled_code(self):
        """WS: canUseAi=false → закрытие с кодом 4404 (отдельным от 4403)."""
        from ai.consumers import AI_DISABLED_CLOSE_CODE, MyConsumer

        self.assertEqual(AI_DISABLED_CLOSE_CODE, 4404)

        consumer = MyConsumer()
        closed = {}

        async def fake_close(code=None, reason=None):
            closed["code"] = code
            closed["reason"] = reason

        consumer.close = fake_close
        consumer.user_info = {"userId": "1", "canUseAi": False}
        consumer.scope = {"session": {}}

        async def fake_authenticate(_consumer):
            return SimpleNamespace(pk=1), {"userId": "1", "canUseAi": False}

        consumer.auth_service.authenticate = fake_authenticate

        async def run():
            await consumer.connect()

        async_to_sync(run)()
        self.assertEqual(closed.get("code"), AI_DISABLED_CLOSE_CODE)
        self.assertIn("запрещён", closed.get("reason", ""))

    def test_consumer_allows_when_flag_absent(self):
        """Без флага WS подключается (accept вызывается)."""
        from ai.consumers import MyConsumer

        consumer = MyConsumer()
        accepted = {"value": False}

        async def fake_accept():
            accepted["value"] = True

        async def fake_close(code=None, reason=None):
            raise AssertionError(f"соединение закрыто: {code} {reason}")

        consumer.accept = fake_accept
        consumer.close = fake_close
        consumer.user_info = {"userId": "1"}
        consumer.scope = {"session": {}, "url_route": {"kwargs": {"client_id": "c1"}}}

        async def fake_authenticate(_consumer):
            return SimpleNamespace(pk=1), {"userId": "1"}

        consumer.auth_service.authenticate = fake_authenticate

        async def fake_resolve_account(_user):
            return None

        import ai.consumers as consumers_module
        with patch.object(consumers_module, "resolve_external_account", fake_resolve_account):
            async def run():
                await consumer.connect()

            async_to_sync(run)()
        self.assertTrue(accepted["value"])


class CanUseAiClientTests(SimpleTestCase):
    """Клиентская часть гейта: WS-код 4404 → внятное ТРЁХЯЗЫЧНОЕ сообщение."""

    def _js(self):
        from pathlib import Path

        base = Path(__file__).resolve().parent.parent / "static" / "admin" / "js"
        return {
            name: (base / name).read_text(encoding="utf-8")
            for name in ("ai-common.js", "find_error.js", "decide_task.js")
        }

    def test_localization_has_ai_disabled_in_three_languages(self):
        sources = self._js()
        common = sources["ai-common.js"]
        self.assertIn("aiDisabled:", common)
        # Три языка: RU/EN/FR — по одному ключу в каждой секции.
        self.assertEqual(common.count("aiDisabled:"), 3)
        for marker in ("В этом курсе использование ИИ запрещено",
                       "Using AI is not allowed in this course",
                       "L'utilisation de l'IA n'est pas autorisée"):
            self.assertIn(marker, common)

    def test_close_handlers_show_disabled_message(self):
        sources = self._js()
        for name, src in sources.items():
            self.assertIn("isAiDisabledClose(event)", src, name)
            self.assertIn("showAiDisabledMessage()", src, name)

    def test_disabled_close_code_matches_server(self):
        from ai.consumers import AI_DISABLED_CLOSE_CODE

        common = self._js()["ai-common.js"]
        self.assertIn(f"var AI_DISABLED_CLOSE_CODE = {AI_DISABLED_CLOSE_CODE};", common)


class BatchDeltaColumnsTests(TestCase):
    """Машина проверки (Дельта) в паре прогона: deltaId/deltaName из
    get-solution-result сохраняются в БД и отдаются в UI."""

    def _run_and_result(self):
        from ai import arm_runner
        from ai.models import AIModelTestResult, AIModelTestRun, Task

        run = AIModelTestRun.objects.create(run_id="delta-run", course_id=1)
        task = Task.objects.create(node_id=555, name="Задача", file_extension=".cpp")
        row = AIModelTestResult.objects.create(
            run=run, model_key="M1", model_title="Модель 1", task=task,
            status="done", verdict="solved", delta_id=5,
            delta_name="DelTA4 at NIT1 Win10 x64",
        )
        return run, row

    def test_delta_fields_are_persisted(self):
        from ai.models import AIModelTestResult

        _run, row = self._run_and_result()
        row.refresh_from_db()
        self.assertEqual(row.delta_id, 5)
        self.assertEqual(row.delta_name, "DelTA4 at NIT1 Win10 x64")

    def test_batch_results_from_db_exposes_delta(self):
        from ai import arm_runner

        run, _row = self._run_and_result()
        for light in (True, False):
            items = arm_runner._batch_results_from_db(run, light=light)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["delta_id"], 5)
            self.assertEqual(items[0]["delta_name"], "DelTA4 at NIT1 Win10 x64")

    def test_result_body_endpoint_exposes_delta(self):
        from ai.admin.arm import admin_arm_solve_result_body_view
        from ai.models import AIModelTestResult

        _run, row = self._run_and_result()
        factory = RequestFactory()
        request = factory.get(f"/ai/admin/arm/solve/result/{row.pk}/body/")
        request.user = get_user_model().objects.create_user(
            username="delta-admin", password="***", is_staff=True, is_superuser=True)
        response = admin_arm_solve_result_body_view(request, row.pk)
        self.assertEqual(response.status_code, 200)
        body = json.loads(response.content.decode())["result"]
        self.assertEqual(body["delta_id"], 5)
        self.assertEqual(body["delta_name"], "DelTA4 at NIT1 Win10 x64")

    def test_poll_reads_delta_from_dl_answer(self):
        """Поллинг get-solution-result отдаёт deltaId/deltaName — они
        попадают в результат пары."""
        from ai import arm_runner

        def fake_send(session_id, node_id, code, file_extension, course_id=0, **kw):
            return {"queueId": 42, "message": "ok"}

        def fake_poll(session_id, queue_id):
            return {
                "isFinished": True,
                "comment": "Все тесты успешно пройдены",
                "deltaId": 5,
                "deltaName": "DelTA4 at NIT1 Win10 x64",
            }

        with patch("ai.dl_api_client.send_solution_to_dl", fake_send), \
                patch("ai.dl_api_client.get_solution_result_from_dl", fake_poll):
            res = arm_runner._test_solution_on_dl(
                "SID", 2606747, "code...", ".mpc", max_polls=1, poll_interval=0,
            )
        self.assertEqual(res["delta_id"], 5)
        self.assertEqual(res["delta_name"], "DelTA4 at NIT1 Win10 x64")


class EducationNamesFromDLTests(TestCase):
    """Названия вуза/школы и группы приходят из DL (EducationInfo 1.4.2):
    get-user-info отдаёт их сразу, get-id-user-info дозабирает без sessionId."""

    def test_student_info_extraction_includes_names(self):
        from ai.external_account import _extract_student_info

        info = _extract_student_info({
            "email": "s@gsu.by",
            "education": {
                "form": "F-1", "schoolId": "S-1", "schoolKind": "Гимназия",
                "schoolNo": "5", "groupMaskId": "G-1", "formLetter": "А",
                "schoolName": "Гимназия №5", "formName": "10 класс",
                "groupName": "ИТП-31",
            },
        })
        self.assertEqual(info["education_school_name"], "Гимназия №5")
        self.assertEqual(info["education_form_name"], "10 класс")
        self.assertEqual(info["education_group_name"], "ИТП-31")
        self.assertEqual(info["education_school_id"], "S-1")
        # Без education — пустые строки (поля blank), без исключений.
        empty = _extract_student_info({})
        self.assertEqual(empty["education_school_name"], "")
        self.assertEqual(empty["education_group_name"], "")

    def test_fetch_user_education_reads_dl_answer(self):
        from ai import dl_api_client

        class _Response:
            status_code = 200

            @staticmethod
            def json():
                return {
                    "firstName": "Иван",
                    "education": {
                        "schoolId": "S-1", "groupMaskId": "G-1",
                        "schoolName": "ГГУ", "groupName": "ИТП-31",
                    },
                }

        with patch("ai.dl_api_client._dl_request", return_value=_Response()):
            education = dl_api_client.fetch_user_education("42")
        self.assertEqual(education["schoolName"], "ГГУ")
        self.assertEqual(education["groupName"], "ИТП-31")
        self.assertEqual(education["schoolId"], "S-1")

    def test_fetch_user_education_survives_dl_failure(self):
        from ai import dl_api_client

        with patch("ai.dl_api_client._dl_request", side_effect=dl_api_client.DLApiUnavailable("down")):
            education = dl_api_client.fetch_user_education("42")
        self.assertEqual(education["schoolName"], "")
        self.assertEqual(education["groupName"], "")

    def test_backfill_command_saves_names(self):
        """Команда backfill_education_names дозабирает названия из DL."""
        from io import StringIO

        from ai.management.commands.backfill_education_names import Command
        from ai.models import ExternalDLAccount

        user = get_user_model().objects.create_user(username="bf-user", password="***")
        account = ExternalDLAccount.objects.create(
            user=user, external_user_id="77", external_login="bf",
            education_school_id="S-1", education_group_mask_id="G-1",
        )
        education = {
            "form": "", "schoolId": "S-1", "schoolKind": "", "schoolNo": "",
            "groupMaskId": "G-1", "formLetter": "",
            "schoolName": "ГГУ", "formName": "", "groupName": "ИТП-31",
        }
        out = StringIO()
        with patch("ai.management.commands.backfill_education_names.fetch_user_education",
                   return_value=education):
            Command().handle(stdout=out)
        account.refresh_from_db()
        self.assertEqual(account.education_school_name, "ГГУ")
        self.assertEqual(account.education_group_name, "ИТП-31")

    def test_backfill_command_keeps_old_values_on_empty_answer(self):
        """Пустой ответ DL не затирает уже сохранённое название."""
        from io import StringIO

        from ai.management.commands.backfill_education_names import Command
        from ai.models import ExternalDLAccount

        user = get_user_model().objects.create_user(username="bf-user2", password="***")
        account = ExternalDLAccount.objects.create(
            user=user, external_user_id="78", external_login="bf2",
            education_school_name="ГГУ", education_group_name="ИТП-31",
        )
        empty = {key: "" for key in (
            "form", "schoolId", "schoolKind", "schoolNo", "groupMaskId",
            "formLetter", "schoolName", "formName", "groupName")}
        out = StringIO()
        with patch("ai.management.commands.backfill_education_names.fetch_user_education",
                   return_value=empty):
            Command().handle(stdout=out)
        account.refresh_from_db()
        self.assertEqual(account.education_school_name, "ГГУ")
        self.assertEqual(account.education_group_name, "ИТП-31")


class AccessKeysCacheTests(TestCase):
    """Кэш ключей доступа (admin/permissions.py) — инвалидация и экономия.

    Кэш живёт на инстансе request.user (request-scoped) + общий django-cache
    на весь набор ключей суперюзера. Оба обязаны инвалидироваться при правке
    прав/состава групп, иначе отзыв права «залипает».
    """

    def setUp(self):
        from django.contrib.auth import get_user_model

        self.user = get_user_model().objects.create_user(
            username="cache-user", password="***")
        self.superuser = get_user_model().objects.create_superuser(
            username="cache-super", password="***", email="s@example.com")

    def test_repeated_calls_hit_db_once(self):
        """Два вызова подряд — одна выборка: кэш на инстансе user."""
        from ai.admin.permissions import user_access_keys

        with self.assertNumQueries(1):
            user_access_keys(self.user)
            user_access_keys(self.user)

    def test_view_as_cache_keyed_by_group(self):
        """Кэш ключуется (user.pk, view_as_group_pk): эмуляция не ломается."""
        from ai.admin.permissions import user_access_keys
        from ai.models import AIAccessGroup, AIAdminGroupPermission

        perm_a, _ = AIAdminGroupPermission.objects.get_or_create(
            key="page.cache_a", defaults={"label": "А", "section": "page"})
        perm_b, _ = AIAdminGroupPermission.objects.get_or_create(
            key="page.cache_b", defaults={"label": "Б", "section": "page"})
        group_a = AIAccessGroup.objects.create(name="Кэш-А")
        group_b = AIAccessGroup.objects.create(name="Кэш-Б")
        group_a.permissions.add(perm_a)
        group_b.permissions.add(perm_b)
        group_a.users.add(self.user)
        group_b.users.add(self.user)

        setattr(self.user, "_ai_view_as_group_pk", group_a.pk)
        keys_a = user_access_keys(self.user)
        setattr(self.user, "_ai_view_as_group_pk", group_b.pk)
        keys_b = user_access_keys(self.user)
        self.assertIn("page.cache_a", keys_a)
        self.assertNotIn("page.cache_b", keys_a)
        self.assertIn("page.cache_b", keys_b)
        self.assertNotIn("page.cache_a", keys_b)

    def test_group_membership_change_invalidates_cache(self):
        """Добавление юзера в группу действует немедленно (без перелогина)."""
        from ai.admin.permissions import user_has_key
        from ai.models import AIAccessGroup, AIAdminGroupPermission

        group = AIAccessGroup.objects.get(
            name="Разработчик")
        perm, _ = AIAdminGroupPermission.objects.get_or_create(
            key="page.tokens", defaults={"label": "Токены", "section": "page"})
        group.permissions.add(perm)
        self.assertFalse(user_has_key(self.user, "page.tokens"))

        group.users.add(self.user)
        self.assertTrue(user_has_key(self.user, "page.tokens"))

        group.users.remove(self.user)
        self.assertFalse(user_has_key(self.user, "page.tokens"))

    def test_superuser_all_keys_refreshed_after_new_permission(self):
        """Новое право сразу видно суперюзеру (кэш сброшен сигналом)."""
        from ai.admin.permissions import user_access_keys
        from ai.models import AIAdminGroupPermission

        key = "page.cache_probe"
        self.assertNotIn(key, user_access_keys(self.superuser))
        AIAdminGroupPermission.objects.create(
            key=key, label="Проба", section=AIAdminGroupPermission.SECTION_PAGE)
        self.assertIn(key, user_access_keys(self.superuser))
        AIAdminGroupPermission.objects.filter(key=key).delete()
        self.assertNotIn(key, user_access_keys(self.superuser))


class TokenUsageSqlParityTests(TestCase):
    """Сверка SQL-агрегации с эталонной Python-версией.

    Срезы «Контроля токенов» переведены в SQL (GROUP BY) — цифры при этом
    обязаны совпадать с эталоном до конца: иначе страница начала бы врать
    про расход токенов.
    """

    def setUp(self):
        from ai.models import AIRequestLog

        self.now = timezone.now()
        self.logs = [
            # обычный запрос одной моделью
            AIRequestLog.objects.create(
                sent_at=self.now,
                mode=AIRequestLog.MODE_CHAT,
                source=AIRequestLog.SOURCE_WEBSOCKET,
                status=AIRequestLog.STATUS_SUCCESS,
                model_names=["Ollama Gemma"],
                tokens=100, tokens_prompt=40,
                user_full_name="Иванов Иван", username="ivanov",
                external_user_id="dl-1",
            ),
            # web-пул: usage не отдаёт
            AIRequestLog.objects.create(
                sent_at=self.now,
                mode=AIRequestLog.MODE_SOLVE,
                source=AIRequestLog.SOURCE_WEBSOCKET,
                status=AIRequestLog.STATUS_SUCCESS,
                model_names=["Web_DeepSeek"],
                tokens=0, tokens_prompt=0,
                user_full_name="Петров Пётр", username="petrov",
                external_user_id="dl-2",
            ),
            # сводная запись прогона: несколько моделей
            AIRequestLog.objects.create(
                sent_at=self.now,
                mode=AIRequestLog.MODE_BATCH_SOLVE,
                source=AIRequestLog.SOURCE_ARM,
                status=AIRequestLog.STATUS_SUCCESS,
                model_names=["Ollama Gemma", "OpenRouter Qwen", "Groq Llama"],
                tokens=300, tokens_prompt=100,
                user_full_name="Иванов Иван", username="ivanov",
                external_user_id="dl-1",
            ),
            # пустое имя пользователя → фолбэк на логин
            AIRequestLog.objects.create(
                sent_at=self.now,
                mode=AIRequestLog.MODE_CHAT,
                source=AIRequestLog.SOURCE_WEBSOCKET,
                status=AIRequestLog.STATUS_SUCCESS,
                model_names=["Ollama Kimi"],
                tokens=7, tokens_prompt=3,
                user_full_name="", username="siesta",
                external_user_id="",
            ),
            # без моделей вовсе
            AIRequestLog.objects.create(
                sent_at=self.now,
                mode=AIRequestLog.MODE_FIND_ERROR,
                source=AIRequestLog.SOURCE_WEBSOCKET,
                status=AIRequestLog.STATUS_SUCCESS,
                model_names=[],
                tokens=5, tokens_prompt=1,
                user_full_name="Сидоров С", username="sidorov",
                external_user_id="dl-3",
            ),
        ]

    def _compare(self, **kwargs):
        from ai.models import AIRequestLog
        from ai.services.token_usage import (
            _collect_log_slices_legacy,
            collect_log_slices,
        )

        sql_report = collect_log_slices(AIRequestLog.objects.all(), **kwargs)
        py_report = _collect_log_slices_legacy(AIRequestLog.objects.all(), **kwargs)
        for key in ("total", "by_model", "by_user", "by_mode", "by_day"):
            self.assertEqual(
                sql_report[key], py_report[key],
                msg=f"срез {key} разошёлся при фильтрах {kwargs}",
            )
        self.assertEqual(sql_report["incomplete"], py_report["incomplete"])

    def test_parity_without_filters(self):
        self._compare()

    def test_parity_with_model_filter(self):
        self._compare(model="Groq")

    def test_parity_with_mode_and_user_filters(self):
        self._compare(mode="chat")
        self._compare(user="Иванов")

    def test_by_user_falls_back_to_login(self):
        from ai.models import AIRequestLog
        from ai.services.token_usage import collect_log_slices

        report = collect_log_slices(AIRequestLog.objects.all())
        labels = [row["label"] for row in report["by_user"]]
        self.assertIn("siesta", labels)
        self.assertNotIn("", labels)

    def test_web_pool_slice_marked_incomplete(self):
        from ai.models import AIRequestLog
        from ai.services.token_usage import collect_log_slices

        report = collect_log_slices(AIRequestLog.objects.all())
        self.assertTrue(report["incomplete"])
        by_mode = {row["key"]: row for row in report["by_mode"]}
        self.assertTrue(by_mode["solve"]["incomplete"])
        self.assertFalse(by_mode["chat"]["incomplete"])

    def test_batch_slices_parity(self):
        from ai.models import AIModelTestResult, AIModelTestRun
        from ai.services.token_usage import (
            _collect_batch_slices_legacy,
            collect_batch_slices,
        )

        run = AIModelTestRun.objects.create(
            run_id=uuid.uuid4().hex,
            run_type=AIModelTestRun.RUN_TYPE_BATCH,
            started_at=self.now,
        )
        AIModelTestResult.objects.create(
            run=run, model_key="M1", model_title="Ollama Gemma",
            status=AIModelTestResult.STATUS_OK,
            verdict=AIModelTestResult.VERDICT_SOLVED,
            tokens=50, tokens_prompt=20,
        )
        AIModelTestResult.objects.create(
            run=run, model_key="M2", model_title="Web_Kimi",
            status=AIModelTestResult.STATUS_OK,
            verdict=AIModelTestResult.VERDICT_FAILED,
            tokens=0, tokens_prompt=0,
            topic_name_snapshot="Массивы",
        )
        runs = AIModelTestRun.objects.all()
        sql_report = collect_batch_slices(runs)
        py_report = _collect_batch_slices_legacy(runs)
        for key in ("total", "by_model", "by_topic", "pairs", "incomplete"):
            self.assertEqual(sql_report[key], py_report[key], msg=f"срез {key}")


class TokensAjaxPartialTests(_AdminViewRequestMixin, TestCase):
    """«Контроль токенов»: AJAX-обновление панелей + вёрстка таблиц.

    Фильтры применяются без перезагрузки страницы: сервер в XHR-режиме
    отдаёт только панели (карточки + срезы), клиент подменяет их на месте.
    Таблицы не «плывут» — фиксированная сетка в две колонки.
    """

    def setUp(self):
        self.factory = RequestFactory()
        self.user = get_user_model().objects.create_user(
            username="tokens-ajax", password="x", is_staff=True,
        )
        AIRequestLog.objects.create(
            sent_at=timezone.now(),
            mode=AIRequestLog.MODE_CHAT,
            source=AIRequestLog.SOURCE_WEBSOCKET,
            status=AIRequestLog.STATUS_SUCCESS,
            model_names=["Ollama Gemma"],
            tokens=100, tokens_prompt=40,
            user_full_name="Иванов Иван",
        )

    def _get(self, xhr=False):
        from ai.admin.tokens import admin_token_usage_view

        request = self._admin_request(self.user, path="/ai/admin/ai/tokens/")
        if xhr:
            request.META["HTTP_X_REQUESTED_WITH"] = "XMLHttpRequest"
        with patch("ai.admin.site.get_external_user_id_from_request", return_value="12345"):
            return admin_token_usage_view(request)

    def test_full_page_has_filters_and_panels(self):
        response = self._get()
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("ai-tokens-filters", html)
        self.assertIn("ai-tokens-grid", html)
        self.assertIn("ai-tokens-panel", html)

    def test_xhr_returns_panels_only(self):
        response = self._get(xhr=True)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        # Панели — да, форма фильтров — нет (клиент её не перерисовывает).
        self.assertIn("ai-tokens-grid", html)
        self.assertNotIn("ai-tokens-filters", html)

    def test_xhr_and_full_page_have_same_numbers(self):
        full = self._get().content.decode()
        xhr = self._get(xhr=True).content.decode()
        for html in (full, xhr):
            self.assertIn("140", html)  # 100 ответа + 40 промпта

    def test_report_computed_once_per_request(self):
        from ai.services import token_usage as token_usage_module

        with patch(
            "ai.admin.tokens.token_usage_report",
            wraps=token_usage_module.token_usage_report,
        ) as spy:
            self._get()
        self.assertEqual(spy.call_count, 1)

    def test_grid_is_two_fixed_columns(self):
        css = (
            Path("static/admin/ai/tokens/index.css")
            .read_text(encoding="utf-8")
        )
        self.assertIn("grid-template-columns: repeat(2, minmax(0, 1fr))", css)
        self.assertNotIn("repeat(auto-fit, minmax(430px", css)

    def test_table_layout_fixed_and_label_truncated(self):
        css = (
            Path("static/admin/ai/tokens/index.css")
            .read_text(encoding="utf-8")
        )
        self.assertIn("table-layout: fixed", css)
        self.assertIn(".ai-tokens-label", css)
        template = (
            Path("ai/templates/admin/ai/_tokens_table.html")
            .read_text(encoding="utf-8")
        )
        self.assertIn("ai-tokens-label", template)
        self.assertIn("title=", template)

    def test_index_js_uses_debounce_and_ajax(self):
        js = (
            Path("static/admin/ai/tokens/index.js")
            .read_text(encoding="utf-8")
        )
        self.assertIn("setTimeout", js)
        self.assertIn("X-Requested-With", js)
        self.assertIn("AbortController", js)
        self.assertNotIn("form.submit()", js)


class CanUseAiHttpGateTests(TestCase):
    """Гейт DL «canUseAi» на HTTP-эндпоинтах отправки/опроса решения.

    Флаг запрета AI в курсе (dl-rest-api.json, UserInfo.canUseAi) должен
    закрывать не только страницу чата и WS, но и отправку решения на
    проверку: код сгенерировать нельзя, проверять нечего.
    """

    def _request(self, path, body):
        factory = RequestFactory()
        request = factory.post(path, data=json.dumps(body),
                               content_type="application/json")
        request.user = get_user_model().objects.create_user(
            username="dl-gate-user", password="***")
        request.session = {}
        request.user_info = {"userId": "student", "canUseAi": False}
        request.COOKIES = {"userId": "student"}
        return request

    def test_send_solution_blocked_when_ai_disabled(self):
        from ai.views import send_solution_view

        request = self._request(
            "/ai/api/send-solution/",
            {"nodeId": 9101, "code": "program a;", "fileExtension": ".pas"},
        )
        response = send_solution_view(request)
        self.assertEqual(response.status_code, 403)
        payload = json.loads(response.content.decode())
        self.assertIn("запрещено", payload.get("error", ""))

    def test_send_solution_blocked_by_session_cache_flag(self):
        """Флаг в кеше сессии (external_user_info) — тоже блокирует."""
        from ai.views import send_solution_view

        factory = RequestFactory()
        request = factory.post(
            "/ai/api/send-solution/",
            data=json.dumps({"nodeId": 9101, "code": "x", "fileExtension": ".pas"}),
            content_type="application/json",
        )
        request.user = get_user_model().objects.create_user(
            username="dl-gate-user2", password="***")
        request.session = {"external_user_info": {"userId": "s", "canUseAi": False}}
        request.COOKIES = {"userId": "student"}
        response = send_solution_view(request)
        self.assertEqual(response.status_code, 403)

    def test_get_solution_result_blocked_when_ai_disabled(self):
        from ai.views import get_solution_result_view

        request = self._request("/ai/api/get-solution-result/", {"queueId": 5})
        response = get_solution_result_view(request)
        self.assertEqual(response.status_code, 403)

    def test_allowed_without_flag(self):
        """Без флага эндпоинты работают как раньше (гейт не блокирует)."""
        from ai.views import send_solution_view

        factory = RequestFactory()
        request = factory.post(
            "/ai/api/send-solution/",
            data=json.dumps({"nodeId": 9101, "code": "x", "fileExtension": ".pas"}),
            content_type="application/json",
        )
        request.user = get_user_model().objects.create_user(
            username="dl-gate-ok", password="***")
        request.session = {}
        request.user_info = {"userId": "student"}
        request.session = {"external_session_id": "SID-1"}
        request.COOKIES = {"userId": "student"}
        with patch("ai.dl_api_client.send_solution_to_dl",
                   return_value={"queueId": 7}) as send:
            response = send_solution_view(request)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(send.called)

    def test_ai_disabled_reason_single_source(self):
        """Текст отказа — один на странице, WS и HTTP (общий хелпер)."""
        from ai.services.auth import ai_disabled_reason

        factory = RequestFactory()
        request = factory.get("/ai/chat/")
        request.session = {}
        request.user_info = {"userId": "s", "canUseAi": False}
        reason = ai_disabled_reason(request)
        self.assertIsNotNone(reason)
        self.assertIn("запрещено", reason)
        # Тот же текст отдаёт и views-обёртка.
        from ai.views import _ai_disabled_reason

        self.assertEqual(_ai_disabled_reason(request), reason)


class DlFileNameContractTests(TestCase):
    """Имя временного файла решения по контракту DL v1.4.2.

    ``SendSolutionRequest.fileName``: ``^[A-Za-z0-9_-]+\\.[A-Za-z0-9]+$``,
    ≤ 200 символов, расширение ТОЧНО как в ``fileExtension``.
    """

    def test_valid_name_kept(self):
        from ai.dl_api_client import build_dl_file_name

        self.assertEqual(
            build_dl_file_name("Deepseek-4-1-flash_12345", ".mpc"),
            "Deepseek-4-1-flash_12345.mpc",
        )

    def test_extension_taken_from_file_extension(self):
        from ai.dl_api_client import build_dl_file_name

        self.assertEqual(build_dl_file_name("solution", ".pas"), "solution.pas")
        # Без точки в расширении — добавляем.
        self.assertEqual(build_dl_file_name("solution", "cpp"), "solution.cpp")

    def test_non_latin_and_spaces_replaced(self):
        from ai.dl_api_client import build_dl_file_name

        name = build_dl_file_name("Моё решение 1", ".cpp")
        self.assertIsNotNone(name)
        self.assertRegex(name, r"^[A-Za-z0-9_-]+\.[A-Za-z0-9]+$")
        self.assertNotIn(" ", name)

    def test_cyrillic_is_transliterated_readably(self):
        """Русское имя → читаемое латинское (не «_» и не пустое)."""
        from ai.dl_api_client import build_dl_file_name

        name = build_dl_file_name("Проверка новых задач", ".pas")
        self.assertEqual(name, "Proverka_novyh_zadach.pas")

    def test_existing_extension_in_base_is_replaced(self):
        from ai.dl_api_client import build_dl_file_name

        self.assertEqual(build_dl_file_name("sol.cpp", ".pas"), "sol.pas")

    def test_reserved_windows_name_escaped(self):
        from ai.dl_api_client import build_dl_file_name

        name = build_dl_file_name("CON", ".txt")
        self.assertIsNotNone(name)
        self.assertNotEqual(name.split(".")[0].upper(), "CON")

    def test_empty_base_returns_none(self):
        from ai.dl_api_client import build_dl_file_name

        self.assertIsNone(build_dl_file_name("", ".cpp"))
        self.assertIsNone(build_dl_file_name("   ", ".cpp"))
        self.assertIsNone(build_dl_file_name("///", ".cpp"))

    def test_long_name_truncated_to_limit(self):
        from ai.dl_api_client import build_dl_file_name

        name = build_dl_file_name("a" * 500, ".cpp")
        self.assertIsNotNone(name)
        self.assertLessEqual(len(name), 200)
        self.assertTrue(name.endswith(".cpp"))

    def test_bad_extension_returns_none(self):
        from ai.dl_api_client import build_dl_file_name

        self.assertIsNone(build_dl_file_name("sol", ""))
        self.assertIsNone(build_dl_file_name("sol", ".c++"))

    def test_send_solution_sends_file_name(self):
        from ai.dl_api_client import send_solution_to_dl

        with patch("ai.dl_api_client._dl_request") as request:
            request.return_value = _fake_response(200, {"queueId": 3})
            send_solution_to_dl("SID", 100, "code", ".cpp", course_id=5,
                                file_name="my_solution")
        payload = request.call_args.kwargs["json"]
        self.assertEqual(payload.get("fileName"), "my_solution.cpp")
        self.assertEqual(payload.get("courseId"), 5)

    def test_send_solution_omits_invalid_file_name(self):
        """Невалидное имя → поле не отправляется (DL назовёт файл сам)."""
        from ai.dl_api_client import send_solution_to_dl

        with patch("ai.dl_api_client._dl_request") as request:
            request.return_value = _fake_response(200, {"queueId": 3})
            send_solution_to_dl("SID", 100, "code", ".cpp", course_id=5,
                                file_name="")
        payload = request.call_args.kwargs["json"]
        self.assertNotIn("fileName", payload)

    def test_send_solution_sends_delta(self):
        from ai.dl_api_client import send_solution_to_dl

        with patch("ai.dl_api_client._dl_request") as request:
            request.return_value = _fake_response(200, {"queueId": 3})
            send_solution_to_dl("SID", 100, "code", ".cpp", course_id=5,
                                delta="48")
        payload = request.call_args.kwargs["json"]
        self.assertEqual(payload.get("delta"), "48")

    def test_send_solution_omits_empty_delta(self):
        from ai.dl_api_client import send_solution_to_dl

        with patch("ai.dl_api_client._dl_request") as request:
            request.return_value = _fake_response(200, {"queueId": 3})
            send_solution_to_dl("SID", 100, "code", ".cpp", course_id=5)
        payload = request.call_args.kwargs["json"]
        self.assertNotIn("delta", payload)


class FetchDeltasTests(TestCase):
    """Список тестирующих машин (Дельт) — GET /restapi/get-deltas."""

    def test_parses_deltas(self):
        from ai.dl_api_client import fetch_deltas

        payload = {"deltas": [
            {"id": 16, "name": "DelTA_NONE", "displayName": "DelTA никакая",
             "isWorking": False},
            {"id": 48, "name": "DelTA4_NIT0_W10_x64",
             "displayName": "DelTA4 at NIT0 Win10 x64", "isWorking": True},
        ]}
        with patch("ai.dl_api_client._dl_request") as request:
            request.return_value = _fake_response(200, payload)
            deltas = fetch_deltas()
        self.assertEqual(len(deltas), 2)
        self.assertEqual(deltas[0]["id"], 16)
        self.assertEqual(deltas[1]["display_name"], "DelTA4 at NIT0 Win10 x64")
        self.assertTrue(deltas[1]["is_working"])

    def test_network_error_returns_empty_list(self):
        """Список машин — справочный: его недоступность не ломает страницу."""
        from ai.dl_api_client import fetch_deltas

        from ai.dl_api_client import DLApiUnavailable

        with patch("ai.dl_api_client._dl_request",
                   side_effect=DLApiUnavailable("нет сети")):
            self.assertEqual(fetch_deltas(), [])

    def test_malformed_entries_skipped(self):
        from ai.dl_api_client import fetch_deltas

        with patch("ai.dl_api_client._dl_request") as request:
            request.return_value = _fake_response(
                200, {"deltas": [{"id": "не число"}, "строка", {"id": 5}]})
            deltas = fetch_deltas()
        self.assertEqual([d["id"] for d in deltas], [5])


def _fake_response(status_code, payload):
    """Подделка requests.Response для тестов клиента DL."""
    response = requests.Response()
    response.status_code = status_code
    response._content = json.dumps(payload).encode("utf-8")
    response.headers["Content-Type"] = "application/json"
    return response


class ArmDeltaSelectionTests(TestCase):
    """Выбор тестирующей машины (Дельты) в форме запуска ARM.

    Контракт DL v1.4.2: машина передаётся как ID ("5") или ТОЧНОЕ название
    ("DelTA4 at NIT1 Win10 x64"); пусто — машину назначает DL.
    """

    def setUp(self):
        self.factory = RequestFactory()
        self.superuser = get_user_model().objects.create_superuser(
            username="delta_admin", password="x", email="d@t.com",
        )
        self.lang = ProgrammingLanguage.objects.create(language_name="Pascal")

    def _post(self, payload, deltas=None):
        from ai.admin.arm import admin_arm_solve_start_view

        request = self.factory.post(
            "/ai/admin/arm/solve/start/", data=json.dumps(payload),
            content_type="application/json",
        )
        request.user = self.superuser
        request.session = {}
        options = deltas if deltas is not None else [
            {"id": 48, "name": "DelTA4_NIT0_W10_x64",
             "display_name": "DelTA4 at NIT0 Win10 x64", "is_working": True},
        ]
        with (
            patch("ai.admin.arm._resolve_session_id", return_value="sess-1"),
            patch("ai.admin.arm._resolve_active_course_id", return_value=(1450, "")),
            patch("ai.admin.arm._fetch_delta_options", return_value=options),
            patch("ai.admin.arm.start_batch_solve_run", return_value=("run-x", None)) as start_mock,
        ):
            response = admin_arm_solve_start_view(request)
        return response, start_mock

    def _payload(self, **extra):
        payload = {
            "node_ids": [101], "models": ["FakeModel"],
            "language_id": self.lang.id, "dl_test": True,
        }
        payload.update(extra)
        return payload

    def test_delta_id_passed_to_run(self):
        response, start_mock = self._post(self._payload(delta="48"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(start_mock.call_args.kwargs["delta"], "48")

    def test_delta_display_name_accepted(self):
        response, start_mock = self._post(
            self._payload(delta="DelTA4 at NIT0 Win10 x64"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            start_mock.call_args.kwargs["delta"], "DelTA4 at NIT0 Win10 x64")

    def test_unknown_delta_rejected(self):
        response, start_mock = self._post(self._payload(delta="999"))
        self.assertEqual(response.status_code, 400)
        self.assertFalse(start_mock.called)

    def test_empty_delta_allows_automatic_assignment(self):
        response, start_mock = self._post(self._payload(delta=""))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(start_mock.call_args.kwargs["delta"], "")

    def test_delta_stored_in_run_params(self):
        """Машина сохраняется в снимке формы запуска (run_params)."""
        from ai.arm_runner import start_batch_solve_run

        with (
            patch("ai.arm_runner.get_runtime_model_handlers",
                  return_value={"Web_DeepSeek": {"title": "Web DS", "handler": None}}),
            patch("ai.arm_runner.is_arm_solve_model", return_value=True),
            patch("ai.arm_runner.threading.Thread") as thread_mock,
        ):
            run_id, error = start_batch_solve_run(
                [101], ["Web_DeepSeek"], 1, "sess-1",
                delta="48", file_name="my_run",
            )
        self.assertEqual(error, "")
        self.assertTrue(run_id)
        job = thread_mock.call_args.kwargs.get("kwargs", {})
        self.assertEqual(job.get("delta"), "48")
        self.assertEqual(job.get("file_name"), "my_run")


class ArmDeltaFormTests(_AdminViewRequestMixin, TestCase):
    """Селект тестирующей машины на странице ARM."""

    def setUp(self):
        self.factory = RequestFactory()
        self.user = get_user_model().objects.create_user(
            username="delta_page", password="x", is_staff=True,
        )

    def test_page_has_delta_select(self):
        from ai.admin.arm import admin_arm_solve_view

        request = self._admin_request(self.user, path="/ai/admin/arm/solve/")
        options = [
            {"id": 48, "name": "DelTA4_NIT0_W10_x64",
             "display_name": "DelTA4 at NIT0 Win10 x64", "is_working": True},
        ]
        with (
            patch("ai.admin.arm._resolve_active_course_id", return_value=(1450, "")),
            patch("ai.admin.arm._fetch_delta_options", return_value=options),
            patch("ai.admin.arm.get_arm_solve_model_options", return_value=[]),
        ):
            response = admin_arm_solve_view(request)
        html = response.render().content.decode()
        self.assertIn('id="armDeltaSelect"', html)
        self.assertIn('name="delta"', html)
        self.assertIn("DelTA4 at NIT0 Win10 x64", html)

    def test_page_without_delta_list_still_renders(self):
        """Список машин недоступен — форма работает (машина назначается DL)."""
        from ai.admin.arm import admin_arm_solve_view

        request = self._admin_request(self.user, path="/ai/admin/arm/solve/")
        with (
            patch("ai.admin.arm._resolve_active_course_id", return_value=(1450, "")),
            patch("ai.admin.arm._fetch_delta_options", return_value=[]),
            patch("ai.admin.arm.get_arm_solve_model_options", return_value=[]),
        ):
            response = admin_arm_solve_view(request)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('id="armDeltaSelect"', response.render().content.decode())
