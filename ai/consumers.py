"""WebSocket consumer для AI-чата на Django Channels.

Обрабатывает входящие WebSocket-сообщения: аутентификацию, rate limiting,
композицию промпта, вызов AI-модели и форматирование ответа.
Поддерживает режимы chat, solve (с node_id из dl.gsu.by) и find_error.
"""

import asyncio
import json
import logging

from asgiref.sync import sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.utils import timezone

from .constants import MOSCOW_TZ
from .i18n import ws_text
from .model_clients import registry
from .model_clients.exceptions import humanize_model_error
from .services import (
    LogWriter,
    MessageComposer,
    ModelCaller,
    PromptResolver,
    WebSocketAuthService,
    CACHE_SERVE_LOG_MESSAGE,
    conversation_history,
    ensure_task,
    find_passed_solution,
    get_user_identity_for_log,
    mark_cache_used,
    resolve_external_account,
)
from .throttling import rate_limiter

logger = logging.getLogger(__name__)

# Структурированный статус терминальных WS-сообщений: трейлинг-строка
# «[[ai-status:<status>]]» (processing/success/error/cleared/limit). Протокол
# остаётся plain-text (совместимость с персистенцией localStorage), маркер
# фронтенд срезает перед рендером, но читает для распознавания терминальности
# независимо от языка UI (ai-common.js getAiWsStatus/isTerminalAiMessage).
AI_WS_STATUS_MARKER = "\n[[ai-status:{status}]]"


def _with_status(text: str, status: str) -> str:
    return f"{text}{AI_WS_STATUS_MARKER.format(status=status)}"


class ResponseFormatter:
    """Форматирование исходящих WebSocket-сообщений для пользователя.

    Методы формируют текстовые сообщения с временными метками,
    информацией о модели, времени обработки и потраченных токенах.
    Формат: «⏱timestamp текст» — единый протокол с фронтендом. Язык текстов —
    язык UI из payload (data["language"], кука ai_state — фолбэк); каждый
    терминальный payload несёт trailing-маркер статуса (_with_status).
    """

    def format_think(self, timestamp_str: str, text: str) -> str:
        return "<think>" + f"{timestamp_str} {text}" + "</think>"

    def format_user_processing(self, timestamp_str: str, message: str, language: str = "") -> str:
        return _with_status(
            "<think>" + f"{timestamp_str} "
            + ws_text(language, "processing", message=message) + "</think>",
            "processing",
        )

    def format_success(
        self,
        timestamp_str: str,
        model_title: str,
        duration: str,
        response_text: str,
        tokens: int | str = 0,
        language: str = "",
    ) -> str:
        return _with_status(
            "<think>" + f"{timestamp_str} {ws_text(language, 'success_head')}" + "</think>\n"
            + ws_text(language, "success_full", model=model_title, duration=duration,
                      tokens=tokens, response=response_text),
            "success",
        )

    def format_simple_success(self, timestamp_str: str, response_text: str, language: str = "", status: str = "success") -> str:
        return _with_status(
            "<think>" + f"{timestamp_str} {ws_text(language, 'success_head')}" + "</think>\n"
            + str(response_text),
            status,
        )

    def format_duration(self, total_seconds: float, language: str = "") -> str:
        if total_seconds < 60:
            return ws_text(language, "dur_sec", value=f"{total_seconds:.3f}")
        minutes = int(total_seconds // 60)
        seconds = total_seconds % 60
        return ws_text(language, "dur_min_sec", minutes=minutes, seconds=f"{seconds:.3f}")


class MyConsumer(AsyncWebsocketConsumer):
    """Основной WebSocket consumer для AI-чата.

    Поток обработки:
    1. connect() — аутентификация через WebSocketAuthService, извлечение user_id.
    2. receive() — парсинг JSON, проверка rate limit, маршрутизация в _handle_message.
    3. _handle_message() — композиция промпта через MessageComposer, вызов модели
       через ModelCaller, логирование через LogWriter, форматирование ответа.

    Поддерживаемые действия:
    - clear_context: сброс истории диалога (conversation_history).
    - type 1: обычный чат.
    - type 2: решение задачи (с progLng, topic, nodeId).
    - type 3: поиск ошибки в коде.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.auth_service = WebSocketAuthService()
        self.prompt_resolver = PromptResolver()
        self.composer = MessageComposer(self.prompt_resolver)
        self.caller = ModelCaller(registry)
        self.log_writer = LogWriter()
        self.formatter = ResponseFormatter()

    async def connect(self):
        user, user_info = await self.auth_service.authenticate(self)
        if user is None:
            await self.close(code=4403)
            return

        self.user = user
        self.user_info = user_info
        self.external_account = await resolve_external_account(user) if user and not isinstance(user, str) else None
        self.user_id = self._extract_user_id(user, user_info, self.external_account)
        self.client_id = self.scope["url_route"]["kwargs"]["client_id"]
        await self.accept()
        # Сбрасываем серверный контекст модели при входе на страницу, чтобы
        # conversation_history (Redis, привязка по client_id) не копился между
        # визитами и не раздувал расход токенов. По факту им пользуется только
        # OpenRouter — Groq stateless (шлёт одно сообщение), Web DeepSeek держит
        # контекст на своей стороне, SambaNova выключен по умолчанию. Кнопка
        # «Очистить контекст» делает то же самое в любой момент (receive()).
        # Фронтенд-localStorage при этом НЕ трогается — сохранённый диалог на
        # экране остаётся, просто модель начинает сессию с чистого контекста.
        conversation_history.reset(self.client_id)
        logger.debug("WebSocket connected for client %s, user_id=%s", self.client_id, self.user_id)

    def _extract_user_id(self, user, user_info, external_account=None) -> str:
        if isinstance(user, str):
            return user
        if external_account is not None:
            return external_account.external_user_id
        return str(getattr(user, "pk", "") or getattr(user, "username", "") or "")

    def _get_identity_for_log(self) -> dict:
        # Единственная реализация — services/auth.get_user_identity_for_log (DRY);
        # здесь только адаптация к полям consumer'а.
        return get_user_identity_for_log(self.user, self.user_info, self.external_account)

    def _cookie_ui_language(self) -> str:
        """Язык UI пользователя из куки ai_state (пишет ai-common.js).

        payload «Обрабатываю запрос» несёт data["language"] только для сообщений
        диалога; служебные фазы (clear_context, rate limit, некорректный JSON)
        язык не шлют — читаем селект языка из куки напрямую.
        """
        try:
            raw = (self.scope.get("cookies") or {}).get("ai_state") or ""
            data = json.loads(raw) if raw else {}
            return data.get("lang") if isinstance(data, dict) else ""
        except (ValueError, TypeError):
            return ""

    async def disconnect(self, close_code):
        logger.debug("Connection closed for client %s", getattr(self, "client_id", "unknown"))

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
        except json.JSONDecodeError:
            await self.send(text_data=_with_status(
                ws_text(self._cookie_ui_language(), "invalid_json"), "error"))
            return

        logger.debug("Received data: %s", data)

        if data.get("action") == "clear_context":
            conversation_history.reset(self.client_id)
            self.old_language = None
            await self.send(text_data=_with_status(
                ws_text(self._cookie_ui_language(), "cleared"), "cleared"))
            return

        if not self._check_rate_limit():
            await self.send(text_data=_with_status(
                ws_text(self._cookie_ui_language(), "rate_limit"), "limit"))
            return

        try:
            await self._handle_message(data)
        except Exception as exc:
            logger.exception("Unexpected error handling WebSocket message")
            friendly, _ = humanize_model_error(
                str(exc), include_detail=False, language=self._cookie_ui_language())
            await self.send(text_data=_with_status(
                ws_text(self._cookie_ui_language(), "something_wrong", friendly=friendly), "error"))

    def _check_rate_limit(self) -> bool:
        if not self.user_id:
            return True
        return rate_limiter.is_allowed_ws(self.user_id)

    def _resolve_course_id(self) -> int | None:
        """courseID текущего курса пользователя — для DL get-task-info.

        dl.gsu.by требует courseId в get-task-info (иначе 400). Источники:
        user_info из аутентификации WS (обычно None для Django-auth юзера —
        scope["user_info"] никто не заполняет), затем кеш external_user_info
        в Django-сессии (кладут и HTTP-, и WS-аутентификация)."""
        candidates = [self.user_info or {}]
        session = self.scope.get("session")
        if session is not None:
            candidates.append(session.get("external_user_info") or {})
        for info in candidates:
            course = str(info.get("courseID") or "").strip()
            if course.isdigit():
                return int(course)
        return None

    @staticmethod
    def _parse_node_id(raw) -> int | None:
        """Coerce a WS message nodeId into a positive int, or None."""
        try:
            text = str(raw or "").strip()
            if not text:
                return None
            value = int(text)
            return value if value > 0 else None
        except (ValueError, TypeError):
            return None

    async def _handle_message(self, data: dict):
        message_type = str(data.get("type", "1"))
        language = data.get("language", "Russian")
        model_key = data.get("value", "DeepSeek_R1")

        prog_lng_id = data.get("progLng") if message_type in ("2", "3") else None
        topic_id = data.get("topic") if message_type in ("2", "3") else None
        prompt_id = data.get("preprompt")

        # Auto-register a DL Task row when a solve request carries a nodeId
        # (the /ai/solve-problem/ page). Fire-and-forget — never blocks the chat
        # and never raises into the message flow.
        node_id = self._parse_node_id(data.get("nodeId"))
        if node_id:
            session_id = self.auth_service.get_session_id(self.scope)
            asyncio.create_task(sync_to_async(ensure_task)(
                node_id,
                programming_language_id=int(prog_lng_id) if prog_lng_id else None,
                topic_id=int(topic_id) if topic_id else None,
                session_id=session_id,
                course_id=self._resolve_course_id(),
            ))

        # Кэш решённых задач: если эта задача на выбранном языке уже прошла
        # тестирование, отдаём сохранённый код сразу, без вызова модели.
        if message_type == "2" and node_id and prog_lng_id:
            cached_solution = None
            try:
                cached_solution = await sync_to_async(find_passed_solution)(
                    node_id, int(prog_lng_id),
                )
            except (ValueError, TypeError):
                cached_solution = None
            if cached_solution is not None:
                await self._serve_cached_solution(cached_solution)
                return

        prog_lng_name, topic_name, prompt_name = await self.prompt_resolver.resolve_context_names(
            prog_lng_id, topic_id, prompt_id, language
        )

        compose_data = {
            **data,
            "topic_name": topic_name,
            "programming_language_name": prog_lng_name or "",
        }
        message, log_mode = await self.composer.compose(compose_data, getattr(self, "old_language", None))
        self.old_language = language

        start_time = timezone.now()
        start_str = timezone.localtime(start_time, MOSCOW_TZ).strftime("%H:%M:%S")
        await self.send(text_data=self.formatter.format_user_processing(start_str, message, language))

        identity = self._get_identity_for_log()
        log = await self.log_writer.create(
            user=identity["user"],
            username=identity["username"],
            external_user_id=identity["external_user_id"],
            user_full_name=identity["user_full_name"],
            client_id=self.client_id,
            source="websocket",
            mode=log_mode,
            sent_at=start_time,
            model_names=[model_key],
            message=message,
            programming_language_id=int(prog_lng_id) if prog_lng_id else None,
            programming_language_name=prog_lng_name,
            topic_id=int(topic_id) if topic_id else None,
            topic_name=topic_name,
            prompt_id=self._resolve_prompt_id_for_log(prompt_id),
            prompt_name=prompt_name,
            task_node_id=node_id,
            task_name="",  # filled by resolve_context_names if available
        )

        result = await self.caller.call(message, self.client_id, model_key, language=language)
        end_time = timezone.now()

        # Treat empty response as an error, not a success.
        if not result.is_error and not (result.response_text or "").strip():
            empty_text = ws_text(language, "empty_response")
            result.is_error = True
            result.error_message = empty_text
            result.response_text = empty_text

        if result.is_error:
            await self.log_writer.update_error(log, result.response_text, result.error_message, end_time)
        else:
            await self.log_writer.update_success(
                log, result.response_text, result.tokens, result.model_title, end_time
            )

        end_str = timezone.localtime(end_time, MOSCOW_TZ).strftime("%H:%M:%S")
        duration = self.formatter.format_duration((end_time - start_time).total_seconds(), language)

        if result.is_error:
            # Исторически ошибки шли через format_simple_success (think-заголовок
            # «Запрос успешно обработан» + текст ошибки); статус честно error.
            await self.send(text_data=self.formatter.format_simple_success(
                end_str, result.response_text, language, status="error"))
        else:
            await self.send(
                text_data=self.formatter.format_success(
                    end_str,
                    result.model_title,
                    duration,
                    result.response_text,
                    result.tokens,
                    language,
                )
            )

    def _resolve_prompt_id_for_log(self, prompt_id):
        if not prompt_id:
            return None
        shared_pk = self.prompt_resolver.parse_shared_prompt_id(prompt_id)
        if shared_pk is not None:
            return shared_pk
        try:
            return int(prompt_id)
        except (ValueError, TypeError):
            return None

    async def _serve_cached_solution(self, solution):
        """Выдача сохранённого решения из кэша решённых задач.

        Ответ форматируется как обычный ответ модели (format_success, маркер
        «Запрос успешно обработан»), чтобы фронтенд персистировал его в
        localStorage и extractLastAiCode() смог забрать код для
        «Тестирования» без генерации.
        """
        from .models import AIRequestLog

        language = self._cookie_ui_language()
        start_time = timezone.now()
        start_str = timezone.localtime(start_time, MOSCOW_TZ).strftime("%H:%M:%S")
        await self.send(text_data=self.formatter.format_user_processing(
            start_str, ws_text(language, "cached_processing"), language))

        identity = self._get_identity_for_log()
        response = ws_text(language, "cached_note", code=solution.code)
        log = await self.log_writer.create(
            user=identity["user"],
            username=identity["username"],
            external_user_id=identity["external_user_id"],
            user_full_name=identity["user_full_name"],
            client_id=self.client_id,
            source="websocket",
            mode=AIRequestLog.MODE_SOLVE,
            sent_at=start_time,
            model_names=[solution.model_key] if solution.model_key else [],
            message=CACHE_SERVE_LOG_MESSAGE,
            programming_language_id=solution.programming_language_id,
            programming_language_name="",
            topic_id=None,
            topic_name="",
            prompt_id=None,
            prompt_name="",
            task_node_id=solution.task_node_id,
        )

        end_time = timezone.now()
        model_title = solution.model_title or ws_text(language, "cached_title")
        await self.log_writer.update_success(
            log, f"{response}\n{ws_text(language, 'cached_log_note')}", 0, model_title, end_time
        )
        end_str = timezone.localtime(end_time, MOSCOW_TZ).strftime("%H:%M:%S")
        duration = self.formatter.format_duration((end_time - start_time).total_seconds())
        await self.send(
            text_data=self.formatter.format_success(
                end_str,
                ws_text(language, "cached_title"),
                duration,
                response,
                0,
                language,
            )
        )
        await sync_to_async(mark_cache_used)(solution)
