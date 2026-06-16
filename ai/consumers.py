import json
import logging
import os
from datetime import timedelta
from urllib.parse import unquote

import django
from channels.generic.websocket import AsyncWebsocketConsumer
from django.utils import timezone

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'DjangoTest.settings')
django.setup()

from .i18n import get_language_instruction, get_localized_name
from .model_clients import registry
from .model_clients.history import conversation_history
from .models import AIRequestLog, ExternalDLAccount, ProgrammingLanguage, Prompt, SharedPrompt, Topic
from asgiref.sync import sync_to_async
from .external_auth import (
    ExternalAuthMisconfigured,
    ExternalAuthUnauthorized,
    ExternalAuthUnavailable,
    fetch_external_user_info,
    get_external_session_cookie_name,
)

current_tokens = 0
logger = logging.getLogger(__name__)


_LEGACY_ALIASES = {
    "DeepSeek_R1": "DeepSeek_R1_Distill_Llama_70B",
    "DeepSeek-R1": "DeepSeek_R1_Distill_Llama_70B",
    "DeepSeek-R1-Distill-Llama-70B": "DeepSeek_R1_Distill_Llama_70B",
    "DeepSeek-V3.1": "DeepSeek_V3_1",
    "DeepSeek-V3.1-cb": "DeepSeek_V3_1_cb",
    "DeepSeek-V3.2": "DeepSeek_V3_2",
    "Llama_3_1_Tulu_3_405B": "Meta_Llama_3_3_70B_Instruct",
    "Meta_Llama_3_1_70B_Instruct": "Meta_Llama_3_3_70B_Instruct",
    "Meta-Llama-3.3-70B-Instruct": "Meta_Llama_3_3_70B_Instruct",
    "Llama-4-Maverick-17B-128E-Instruct": "Llama_4_Maverick_17B_128E_Instruct",
    "MiniMax-M2.5": "MiniMax_M2_5",
    "MiniMax-M2.7": "MiniMax_M2_7",
    "gemma-3-12b-it": "Gemma_3_12b_it",
    "gpt-oss-120b": "Gpt_oss_120b",
    "QwQ_32B": "DeepSeek_R1_Distill_Llama_70B",
    "Mixtral_8x7B": "Llama_4_Maverick_17B_128E_Instruct",
    "Mixtral_8x22b": "Llama_4_Maverick_17B_128E_Instruct",
}


def _resolve_legacy_alias(value: str) -> str:
    return _LEGACY_ALIASES.get(value, value)


@sync_to_async
def _resolve_user_for_log(user):
    """Return (user, username, external_user_id, full_name) for logging."""
    if not user or not getattr(user, "is_authenticated", False):
        return None, "", "", ""

    username = getattr(user, "username", "") or ""
    full_name = (user.get_full_name() or "").strip() or username
    external_id = username
    try:
        external_id = user.external_dl_account.external_user_id
    except (ExternalDLAccount.DoesNotExist, AttributeError):
        pass
    return user, username, external_id, full_name


def _parse_shared_prompt_id(prompt_id):
    """Return shared prompt pk if prompt_id is 'shared_<pk>', else None."""
    if not isinstance(prompt_id, str):
        return None
    if not prompt_id.startswith("shared_"):
        return None
    try:
        return int(prompt_id.split("_", 1)[1])
    except (ValueError, IndexError):
        return None


def _resolve_prompt_id_for_log(prompt_id):
    """Return an integer id for logging from either a shared or regular prompt id."""
    if not prompt_id:
        return None
    shared_pk = _parse_shared_prompt_id(prompt_id)
    if shared_pk is not None:
        return shared_pk
    try:
        return int(prompt_id)
    except (ValueError, TypeError):
        return None


def _mode_from_message_type(message_type):
    """Map WebSocket message type to AIRequestLog mode."""
    mapping = {
        "1": AIRequestLog.MODE_CHAT,
        "2": AIRequestLog.MODE_SOLVE,
        "3": AIRequestLog.MODE_FIND_ERROR,
    }
    return mapping.get(str(message_type), "")


@sync_to_async
def _resolve_context_names(prog_lng_id, topic_id, prompt_id, ui_language=""):
    """Return (programming_language_name, topic_name, prompt_name) for logging."""
    prog_lng_name = ""
    topic_name = ""
    prompt_name = ""
    try:
        if prog_lng_id:
            prog_lng_name = ProgrammingLanguage.objects.values_list("language_name", flat=True).get(id=prog_lng_id)
    except ProgrammingLanguage.DoesNotExist:
        pass
    try:
        if topic_id:
            topic = Topic.objects.get(id=topic_id)
            topic_name = get_localized_name(topic, ui_language, "topic_name")
    except Topic.DoesNotExist:
        pass
    try:
        shared_pk = _parse_shared_prompt_id(prompt_id)
        if shared_pk is not None:
            prompt = SharedPrompt.objects.get(id=shared_pk)
        elif prompt_id:
            prompt = Prompt.objects.select_related("shared_prompt").get(id=prompt_id)
        else:
            prompt = None
        if prompt is not None:
            prompt_name = get_localized_name(prompt, ui_language, "prompt_name")
    except (Prompt.DoesNotExist, SharedPrompt.DoesNotExist):
        pass
    return prog_lng_name, topic_name, prompt_name


@sync_to_async
def _update_log_after_response(log, end_time, response, modell):
    """Update AIRequestLog after a model response."""
    if isinstance(response, tuple):
        response_text = response[0] if len(response) > 0 else ""
        tokens = response[1] if len(response) > 1 else 0
    else:
        response_text = response
        tokens = 0

    log.received_at = end_time
    log.duration_seconds = (end_time - log.sent_at).total_seconds() if log.sent_at else None
    log.model_names = [modell] if modell else log.model_names
    log.response_text = str(response_text or "")[:5000]
    log.tokens = tokens or 0

    error_markers = (
        "ошибка", "error", "таймаут", "timeout", "не удалось", "failed",
        "недоступ", "unavailable", "превышен лимит", "rate limit",
    )
    text_sample = str(response_text or "").lower()[:100]
    if any(marker in text_sample for marker in error_markers):
        log.status = AIRequestLog.STATUS_ERROR
    else:
        log.status = AIRequestLog.STATUS_SUCCESS
    log.save(update_fields=["received_at", "duration_seconds", "model_names", "response_text", "tokens", "status"])


@sync_to_async
def getPromptText(prompt_id, ui_language="", programming_language_name=""):
    try:
        shared_pk = _parse_shared_prompt_id(prompt_id)
        if shared_pk is not None:
            prompt = SharedPrompt.objects.get(id=shared_pk)
            return prompt.get_effective_text(ui_language, programming_language_name)
        prompt = Prompt.objects.select_related('shared_prompt').get(id=prompt_id)
        return prompt.get_effective_text(ui_language, programming_language_name)
    except (Prompt.DoesNotExist, SharedPrompt.DoesNotExist):
        return None
    except Exception as e:
        print(f"Database error: {str(e)}")
        return None

@sync_to_async
def getProgLng(language_id):
    try:
        return ProgrammingLanguage.objects.get(id=language_id).language_name
    except ProgrammingLanguage.DoesNotExist:
        return None
    except Exception as e:
        print(f"Database error: {str(e)}")
        return None


@sync_to_async
def is_ai_app_enabled():
    from .models import AIAppSettings
    return AIAppSettings.get_solo().is_enabled

#функция проверки сессии через внешний API
def check_session(session_id):
    try:
        return fetch_external_user_info(session_id)
    except (ExternalAuthMisconfigured, ExternalAuthUnauthorized, ExternalAuthUnavailable) as exc:
        logger.error(f"Session check error: {exc}")
        return None


class MyConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        cookies = self.scope.get('cookies', {})
        user = self.scope.get("user")
        if user is not None and getattr(user, "is_authenticated", False):
            self.user_id = getattr(user, "username", None) or str(getattr(user, "pk", ""))
            if not await is_ai_app_enabled():
                await self.close(code=4403)
                return
            self.client_id = self.scope['url_route']['kwargs']['client_id']
            await self.accept()
            print(f"WebSocket connected for client {self.client_id}, user_id={self.user_id}")
            return

        session_cookie_name = get_external_session_cookie_name()
        raw_session_id = cookies.get(session_cookie_name)
        if not raw_session_id:
            await self.close(code=4403)
            return

        session_id = unquote(raw_session_id)

        session = self.scope.get("session")
        user_info = None
        if session is not None:
            cached_session_id = session.get("external_session_id")
            cached_user_info = session.get("external_user_info")
            if cached_session_id == session_id and isinstance(cached_user_info, dict) and cached_user_info:
                user_info = cached_user_info

        if user_info is None:
            user_info = await sync_to_async(check_session)(session_id)
            if user_info and session is not None:
                session["external_session_id"] = session_id
                session["external_user_info"] = user_info
                session.modified = True
                await sync_to_async(session.save)()

        if not user_info:
            await self.close(code=4403)
            return

        # Сохраняем user_id для возможного использования
        self.user_id = user_info.get('userId')

        if not await is_ai_app_enabled():
            await self.close(code=4403)
            return

        self.client_id = self.scope['url_route']['kwargs']['client_id']
        await self.accept()
        print(f"WebSocket connected for client {self.client_id}, user_id={self.user_id}")

    async def disconnect(self, close_code):
        print(f"Connection closed for client {self.client_id}")

    async def receive(self, text_data):

        try:
            data = json.loads(text_data)
            print(f"Received data: {data}")

            # Обработка нажатия кнопки Clear Context
            if data.get('action') == 'clear_context':
                conversation_history.reset(self.client_id)
                self.old_language = None
                # Отправляем простое сообщение без тегов think
                await self.send(text_data="Контекст очищен")
                return

            type = data.get('type', '1')
            message = data.get('message', '')
            language = data.get('language', 'Russian')
            value = data.get("value", "DeepSeek_R1")

            print(f"Processing message: type={type}, language={language}, model={value}")

            # Обработка языка
            if hasattr(self, 'old_language') and self.old_language != language:
                message += get_language_instruction(language)

            self.old_language = language

            # Обработка специальных типов сообщений
            if type == "1":
                preprompt = data.get('preprompt', '')
                if preprompt:
                    promptText = await getPromptText(preprompt, language, "")
                    if promptText and (not hasattr(self, 'last_prompt') or self.last_prompt != promptText):
                        message = f"{message}\n\nПрепромпт: {promptText}"
                        self.last_prompt = promptText
            elif type == "2":
                progLng = await getProgLng(data.get('progLng'))
                promptText = await getPromptText(data.get('preprompt'), language, progLng or "")
                message = f"У меня есть задача по программированию, решай ее на языке {progLng}\n{message}"
                if promptText and (not hasattr(self, 'last_prompt') or self.last_prompt != promptText):
                    message += f". Препромпт: {promptText}"
                    self.last_prompt = promptText
            elif type == "3":
                progLng = await getProgLng(data.get('progLng'))
                code = data.get('code', '')
                promptText = await getPromptText(data.get('preprompt'), language, progLng or "")
                message = f"У меня есть задача по программированию, я написал для нее код на языке {progLng}, код не работает, найди пожалуйста ошибку. Задача: {message}. Код: {code}."
                if promptText and (not hasattr(self, 'last_prompt') or self.last_prompt != promptText):
                    message += f". Препромпт: {promptText}"
                    self.last_prompt = promptText

            
            # Время отправки запроса
            start_time = timezone.now()
            start_str = (start_time + timedelta(hours=3)).strftime("%H:%M:%S")

            # Отправляем сообщение пользователю
            await self.send(text_data=f"<think> {start_str} Обрабатываю запрос пользователя</think> Вы: {message}")

            # Создаём запись лога перед вызовом модели
            log_user, log_username, log_external_id, log_full_name = await _resolve_user_for_log(
                self.scope.get("user")
            )

            prog_lng_id = data.get("progLng") if type in ("2", "3") else None
            topic_id = data.get("topic") if type in ("2", "3") else None
            prompt_id = data.get("preprompt")
            prog_lng_name, topic_name, prompt_name = await _resolve_context_names(
                prog_lng_id, topic_id, prompt_id, language
            )

            log = await sync_to_async(AIRequestLog.objects.create)(
                user=log_user,
                username=log_username,
                external_user_id=log_external_id,
                user_full_name=log_full_name,
                client_id=self.client_id,
                source=AIRequestLog.SOURCE_WEBSOCKET,
                mode=_mode_from_message_type(type),
                sent_at=start_time,
                model_names=[value],
                message=message,
                programming_language_id=int(prog_lng_id) if prog_lng_id else None,
                programming_language_name=prog_lng_name,
                topic_id=int(topic_id) if topic_id else None,
                topic_name=topic_name,
                prompt_id=_resolve_prompt_id_for_log(prompt_id),
                prompt_name=prompt_name,
            )
            # Обработка модели AI
            response = "Что-то пошло не так. Попробуйте еще раз."
            modell = value

            normalized_value = registry.get(value) and value or _resolve_legacy_alias(value)

            try:
                handler = registry.handler(normalized_value)
                if handler:
                    modell = registry.title(normalized_value)
                    response = await handler(message, self.client_id)
                    await _update_log_after_response(log, timezone.now(), response, modell)
                else:
                    response = f"Модель {value} не найдена. Используйте доступные модели."
                    end_time = timezone.now()
                    log.status = AIRequestLog.STATUS_ERROR
                    log.error_message = response[:2000]
                    log.received_at = end_time
                    log.duration_seconds = (end_time - start_time).total_seconds()
                    await sync_to_async(log.save)(update_fields=["status", "error_message", "received_at", "duration_seconds"])

            except Exception as e:
                print(f"Error in AI model processing: {str(e)}")
                error_text = str(e)
                response = f"Ошибка при обработке запроса: {error_text}"
                end_time = timezone.now()
                log.status = AIRequestLog.STATUS_ERROR
                log.error_message = error_text[:2000]
                log.received_at = end_time
                log.duration_seconds = (end_time - start_time).total_seconds()
                await sync_to_async(log.save)(update_fields=["status", "error_message", "received_at", "duration_seconds"])


            # Время отправки ответа
            end_time = timezone.now()
            end_str = (end_time + timedelta(hours=3)).strftime("%H:%M:%S")

            # Время обработки
            time_diff = end_time - start_time
            total_seconds = time_diff.total_seconds()

            if total_seconds < 60:
                duration = f"{total_seconds:.3f} сек"
            else:
                minutes = int(total_seconds // 60)
                seconds = total_seconds % 60
                duration = f"{minutes} мин {seconds:.3f} сек"

            # Отправляем ответ
            if isinstance(response, tuple):
                response_text = response[0] if len(response) > 0 else "Пустой ответ от модели."
                response_tokens = response[1] if len(response) > 1 else '0'
                await self.send(text_data=f'''<think> {end_str} Запрос успешно обработан</think>
                Модель: {modell}
                Время обработки запроса: {duration}
                Потрачено токенов: {response_tokens}
                {response_text}''')
            else:
                await self.send(text_data=f'''<think> {end_str} Запрос успешно обработан</think>
                {response}''')

        except json.JSONDecodeError as e:
            await self.send(text_data="Ошибка: Неверный формат JSON")
        except Exception as e:
            print(f"Unexpected error: {str(e)}")
            await self.send(text_data="Что-то пошло не так. Очистка контекста, введите новый запрос.")
