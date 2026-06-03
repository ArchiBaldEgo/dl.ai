import json
import logging
import os
from datetime import datetime, timedelta
from urllib.parse import unquote

import django
from channels.generic.websocket import AsyncWebsocketConsumer

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'DjangoTest.settings')
django.setup()

from .model_health import MODEL_ALIASES
from .models import ProgrammingLanguage, Prompt
from .utils import *
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

@sync_to_async
def getPromptText(prompt_id):
    try:
        return Prompt.objects.get(id=prompt_id).prompt_text
    except Prompt.DoesNotExist:
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
                # Очищаем историю в utils.py
                if self.client_id in hist:
                    hist[self.client_id] = []
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
                language_instruction = ""
                if language == "Русский":
                    language_instruction = ". Разговаривай со мной только по-русски"
                elif language == "Français":
                    language_instruction = ". Communiquez avec moi uniquement en français"
                elif language == "English":
                    language_instruction = ". Communicate with me only in English"
                
                if language_instruction:
                    message += language_instruction

            self.old_language = language

            # Обработка специальных типов сообщений
            if type == "2":
                progLng = await getProgLng(data.get('progLng'))
                promptText = await getPromptText(data.get('preprompt'))
                message = f"У меня есть задача по программированию, решай ее на языке {progLng}\n{message}"
                if promptText and (not hasattr(self, 'last_prompt') or self.last_prompt != promptText):
                    message += f". Препромпт: {promptText}"
                    self.last_prompt = promptText
            elif type == "3":
                progLng = await getProgLng(data.get('progLng'))
                code = data.get('code', '')
                promptText = await getPromptText(data.get('preprompt'))
                message = f"У меня есть задача по программированию, я написал для нее код на языке {progLng}, код не работает, найди пожалуйста ошибку. Задача: {message}. Код: {code}."
                if promptText and (not hasattr(self, 'last_prompt') or self.last_prompt != promptText):
                    message += f". Препромпт: {promptText}"
                    self.last_prompt = promptText

            
            # Время отправки запроса
            start_time = datetime.now()
            start_str = (start_time + timedelta(hours=3)).strftime("%H:%M:%S")

            # Отправляем сообщение пользователю
            await self.send(text_data=f"<think> {start_str} Обрабатываю запрос пользователя</think> Вы: {message}")
            # Обработка модели AI
            response = "Что-то пошло не так. Попробуйте еще раз."
            modell = value

            normalized_value = MODEL_ALIASES.get(value, value)

            try:
                model_dispatch = {
                    "DeepSeek_R1_Distill_Llama_70B": (
                        ask_DeepSeek_R1_Distill_Llama_70B_async,
                        "DeepSeek-R1-Distill-Llama-70B",
                    ),
                    "DeepSeek_V3_1": (
                        ask_DeepSeek_V3_1_async,
                        "DeepSeek-V3.1",
                    ),
                    "DeepSeek_V3_1_cb": (
                        ask_DeepSeek_V3_1_cb_async,
                        "DeepSeek-V3.1-cb",
                    ),
                    "DeepSeek_V3_2": (
                        ask_DeepSeek_V3_2_async,
                        "DeepSeek-V3.2",
                    ),
                    "Llama_4_Maverick_17B_128E_Instruct": (
                        ask_Llama_4_Maverick_17B_128E_Instruct_async,
                        "Llama-4-Maverick-17B-128E-Instruct",
                    ),
                    "Meta_Llama_3_3_70B_Instruct": (
                        ask_Meta_Llama_3_3_70B_Instruct_async,
                        "Meta-Llama-3.3-70B-Instruct",
                    ),
                    "MiniMax_M2_5": (
                        ask_MiniMax_M2_5_async,
                        "MiniMax-M2.5",
                    ),
                    "MiniMax_M2_7": (
                        ask_MiniMax_M2_7_async,
                        "MiniMax-M2.7",
                    ),
                    "Gemma_3_12b_it": (
                        ask_Gemma_3_12b_it_async,
                        "gemma-3-12b-it",
                    ),
                    "Gpt_oss_120b": (
                        ask_Gpt_oss_120b_async,
                        "gpt-oss-120b",
                    ),
                    "Web_DeepSeek": (
                        ask_Web_DeepSeek_async,
                        "Web DeepSeek",
                    ),
                    "Web_DeepSeek_Thinking": (
                        ask_Web_DeepSeek_Thinking_async,
                        "Web DeepSeek Thinking",
                    ),
                }

                model_handler = model_dispatch.get(normalized_value)
                if model_handler:
                    handler, model_title = model_handler
                    response = await handler(message, self.client_id)
                    modell = model_title
                else:
                    response = f"Модель {value} не найдена. Используйте доступные модели."
                
                        
            except Exception as e:
                print(f"Error in AI model processing: {str(e)}")
                response = f"Ошибка при обработке запроса: {str(e)}"


            # Время отправки ответа
            end_time = datetime.now()
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
