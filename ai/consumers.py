"""WebSocket consumer for AI chat."""

import asyncio
import json
import logging

from asgiref.sync import sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.utils import timezone

from .constants import MOSCOW_TZ
from .model_clients import registry
from .model_clients.exceptions import humanize_model_error
from .services import (
    LogWriter,
    MessageComposer,
    ModelCaller,
    PromptResolver,
    WebSocketAuthService,
    conversation_history,
    ensure_task,
    resolve_external_account,
)
from .throttling import rate_limiter

logger = logging.getLogger(__name__)


class ResponseFormatter:
    """Format outgoing WebSocket messages."""

    def format_think(self, timestamp_str: str, text: str) -> str:
        return f"<think>{timestamp_str} {text}</think>"

    def format_user_processing(self, timestamp_str: str, message: str) -> str:
        return f"<think>{timestamp_str} Обрабатываю запрос пользователя. Вы: {message}</think>"

    def format_success(
        self,
        timestamp_str: str,
        model_title: str,
        duration: str,
        response_text: str,
        tokens: int | str = 0,
    ) -> str:
        return (
            f"<think>{timestamp_str} Запрос успешно обработан</think>\n"
            f"Модель: {model_title}\n"
            f"Время обработки запроса: {duration}\n"
            f"Потрачено токенов: {tokens}\n"
            f"{response_text}"
        )

    def format_simple_success(self, timestamp_str: str, response_text: str) -> str:
        return f"<think>{timestamp_str} Запрос успешно обработан</think>\n{response_text}"

    def format_duration(self, total_seconds: float) -> str:
        if total_seconds < 60:
            return f"{total_seconds:.3f} сек"
        minutes = int(total_seconds // 60)
        seconds = total_seconds % 60
        return f"{minutes} мин {seconds:.3f} сек"


class MyConsumer(AsyncWebsocketConsumer):
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
        logger.debug("WebSocket connected for client %s, user_id=%s", self.client_id, self.user_id)

    def _extract_user_id(self, user, user_info, external_account=None) -> str:
        if isinstance(user, str):
            return user
        if external_account is not None:
            return external_account.external_user_id
        return str(getattr(user, "pk", "") or getattr(user, "username", "") or "")

    def _get_identity_for_log(self) -> dict:
        user = self.user
        user_info = self.user_info
        result = {
            "user": None,
            "username": "",
            "external_user_id": "",
            "user_full_name": "",
        }
        if user is None:
            return result

        if isinstance(user, str):
            result["external_user_id"] = user
            result["username"] = user
            if user_info:
                first = (user_info.get("firstName") or "").strip()
                last = (user_info.get("lastName") or "").strip()
                result["user_full_name"] = f"{first} {last}".strip() or user
            return result

        if getattr(user, "is_authenticated", False):
            result["user"] = user
            result["username"] = getattr(user, "username", "") or ""
            result["user_full_name"] = (user.get_full_name() or "").strip() or result["username"]
            if self.external_account is not None:
                result["external_user_id"] = self.external_account.external_user_id
            else:
                result["external_user_id"] = result["username"]

        return result

    async def disconnect(self, close_code):
        logger.debug("Connection closed for client %s", getattr(self, "client_id", "unknown"))

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
        except json.JSONDecodeError:
            await self.send(text_data="Ошибка: Неверный формат JSON")
            return

        logger.debug("Received data: %s", data)

        if data.get("action") == "clear_context":
            conversation_history.reset(self.client_id)
            self.old_language = None
            await self.send(text_data="Контекст очищен")
            return

        if not self._check_rate_limit():
            await self.send(text_data="Слишком много сообщений. Попробуйте позже.")
            return

        try:
            await self._handle_message(data)
        except Exception as exc:
            logger.exception("Unexpected error handling WebSocket message")
            friendly, _ = humanize_model_error(str(exc), include_detail=False)
            await self.send(text_data=f"Что-то пошло не так. {friendly}")

    def _check_rate_limit(self) -> bool:
        if not self.user_id:
            return True
        return rate_limiter.is_allowed_ws(self.user_id)

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
            ))

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
        await self.send(text_data=self.formatter.format_user_processing(start_str, message))

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

        result = await self.caller.call(message, self.client_id, model_key)
        end_time = timezone.now()

        if result.is_error:
            await self.log_writer.update_error(log, result.response_text, result.error_message, end_time)
        else:
            await self.log_writer.update_success(
                log, result.response_text, result.tokens, result.model_title, end_time
            )

        end_str = timezone.localtime(end_time, MOSCOW_TZ).strftime("%H:%M:%S")
        duration = self.formatter.format_duration((end_time - start_time).total_seconds())

        if result.is_error:
            await self.send(text_data=self.formatter.format_simple_success(end_str, result.response_text))
        else:
            await self.send(
                text_data=self.formatter.format_success(
                    end_str,
                    result.model_title,
                    duration,
                    result.response_text,
                    result.tokens,
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
