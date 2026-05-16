import json
from channels.generic.websocket import AsyncWebsocketConsumer
import os
from pathlib import Path
import uuid
import asyncio
import copy
#from chat.database import insert_into_bd,start_bd
from http import cookies
import requests
from requests.auth import HTTPBasicAuth
from huggingface_hub import InferenceClient
from dotenv import load_dotenv
from typing import Tuple, Optional
import time
from asyncio import TimeoutError as AsyncTimeoutError

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")
CLIENT_ID = os.getenv("CLIENT_ID")
SECRET = os.getenv("SBER_SECRET")
HF_TOKEN = os.getenv("HF_TOKEN")
SC_TOKEN=os.getenv("SC_TOKEN")
MIST_TOKEN = os.getenv("MIST_TOKEN")
GROQ_TOKEN = os.getenv("GROQ_TOKEN")
DEEPSEEK_API_TOKEN = os.getenv("DEEPSEEK_API_TOKEN") or os.getenv("DEEPSEEK_API_KEY")

# Centralized SambaCloud model IDs to avoid code edits on every deprecation.
SAMBANOVA_MODEL_DEEPSEEK_R1_DISTILL_LLAMA_70B = os.getenv(
    "SAMBANOVA_MODEL_DEEPSEEK_R1_DISTILL_LLAMA_70B",
    "DeepSeek-R1-Distill-Llama-70B",
)
SAMBANOVA_MODEL_DEEPSEEK_V3_1 = os.getenv("SAMBANOVA_MODEL_DEEPSEEK_V3_1", "DeepSeek-V3.1")
SAMBANOVA_MODEL_DEEPSEEK_V3_1_CB = os.getenv("SAMBANOVA_MODEL_DEEPSEEK_V3_1_CB", "DeepSeek-V3.1-cb")
SAMBANOVA_MODEL_DEEPSEEK_V3_2 = os.getenv("SAMBANOVA_MODEL_DEEPSEEK_V3_2", "DeepSeek-V3.2")
SAMBANOVA_MODEL_LLAMA_4_MAVERICK_17B_128E_INSTRUCT = os.getenv(
    "SAMBANOVA_MODEL_LLAMA_4_MAVERICK_17B_128E_INSTRUCT",
    "Llama-4-Maverick-17B-128E-Instruct",
)
SAMBANOVA_MODEL_META_LLAMA_3_3_70B_INSTRUCT = os.getenv(
    "SAMBANOVA_MODEL_META_LLAMA_3_3_70B_INSTRUCT",
    "Meta-Llama-3.3-70B-Instruct",
)
SAMBANOVA_MODEL_MINIMAX_M2_5 = os.getenv("SAMBANOVA_MODEL_MINIMAX_M2_5", "MiniMax-M2.5")
SAMBANOVA_MODEL_GEMMA_3_12B_IT = os.getenv("SAMBANOVA_MODEL_GEMMA_3_12B_IT", "gemma-3-12b-it")
SAMBANOVA_MODEL_GPT_OSS = os.getenv("SAMBANOVA_MODEL_GPT_OSS", "gpt-oss-120b")

# Backward-compatible env names used in older code paths.
SAMBANOVA_MODEL_DEEPSEEK = os.getenv("SAMBANOVA_MODEL_DEEPSEEK", SAMBANOVA_MODEL_DEEPSEEK_V3_1)
SAMBANOVA_MODEL_META = os.getenv("SAMBANOVA_MODEL_META", SAMBANOVA_MODEL_META_LLAMA_3_3_70B_INSTRUCT)
SAMBANOVA_MODEL_MIXTRAL_ALIAS = os.getenv(
    "SAMBANOVA_MODEL_MIXTRAL_ALIAS",
    SAMBANOVA_MODEL_LLAMA_4_MAVERICK_17B_128E_INSTRUCT,
)

timeout = 0

hist=dict([])

proxies = {
            'http': os.getenv("PROXY"),
            'https': os.getenv("PROXY")
        }

BOT_POOL_URL = os.getenv("BOT_POOL_URL", "http://localhost:3000").rstrip("/")


def _post_to_bot_pool(payload: dict, timeout_seconds: int = 120) -> requests.Response:
    # Internal service call must bypass env proxies (HTTP_PROXY/HTTPS_PROXY).
    with requests.Session() as session:
        session.trust_env = False
        return session.post(
            f"{BOT_POOL_URL}/api/send",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=timeout_seconds,
        )


async def _ask_web_deepseek_common(msg: str, user_id: int, thinking: bool) -> Tuple[str, int]:
    '''if DEEPSEEK_API_TOKEN:
        model_name = "deepseek-reasoner" if thinking else "deepseek-chat"
        response = await asyncio.to_thread(
            requests.post,
            'https://api.deepseek.com/chat/completions',
            json={
                "model": model_name,
                "messages": [{"role": "user", "content": msg}],
                "stream": False,
            },
            headers={
                "Authorization": f"Bearer {DEEPSEEK_API_TOKEN}",
                "Content-Type": "application/json",
            },
            proxies=proxies,
            timeout=35,
        )

        print(f"DeepSeek API status: {response.status_code}")
        if response.status_code != 200:
            if response.status_code == 400:
                return 'Неправильный запрос', 0
            if response.status_code == 401:
                return 'DeepSeek API не авторизован. Проверьте DEEPSEEK_API_TOKEN.', 0
            if response.status_code == 429:
                return 'Превышен лимит запросов DeepSeek. Попробуйте позже.', 0
            if response.status_code >= 500:
                return 'Сервер DeepSeek временно недоступен. Попробуйте позже.', 0
            return f'Ошибка сервиса DeepSeek (код {response.status_code}).', 0

        response_content = response.text
        if not response_content:
            raise ValueError("Пустой ответ от DeepSeek API.")

        try:
            obj = json.loads(response_content)
        except json.JSONDecodeError as e:
            print(f"Ошибка при декодировании JSON DeepSeek: {e}")
            return 'Что-то пошло не так с обработкой JSON.', 0

        choices = obj.get('choices') or []
        if not choices:
            return 'Неожиданный формат ответа от DeepSeek.', 0

        first_message = choices[0].get('message', {})
        assistant_content = first_message.get('content')
        if not assistant_content:
            assistant_content = first_message.get('reasoning_content') or ''
        if not assistant_content:
            assistant_content = 'Пустой ответ от модели.'

        completion_tokens = obj.get('usage', {}).get('completion_tokens', 0)
        return assistant_content, completion_tokens'''
    #боту не нужен токен

    payload = {
        "model": "deepseek",
        "user_id": user_id,
        "thinking": thinking,
        "message": msg,
    }

    max_attempts = 4
    for attempt in range(1, max_attempts + 1):
        response = await asyncio.to_thread(_post_to_bot_pool, payload, 120)
        print(f"Response Status: {response.status_code} (attempt {attempt}/{max_attempts})")
        print(f"Response Content: {response.text}")

        if response.status_code == 200:
            response_content = response.text
            if not response_content:
                raise ValueError("Пустой ответ от сервера.")

            try:
                obj = json.loads(response_content)
            except json.JSONDecodeError as e:
                print(f"Ошибка при декодировании JSON: {e}")
                print(f"Содержимое ответа: {response_content}")
                return 'Что-то пошло не так с обработкой JSON.', 0

            return obj['data']["content"], 0

        # bot-pool может вернуть 503/504 во время инициализации; делаем несколько ретраев
        if response.status_code in (503, 504) and attempt < max_attempts:
            await asyncio.sleep(attempt * 2)
            continue

        if response.status_code == 400:
            return 'Неправильный запрос', 0
        if response.status_code == 401:
            return 'Бот не авторизован. Проверьте логин/пароль', 0
        if response.status_code == 429:
            return 'Все боты заняты', 0
        if response.status_code >= 503:
            return 'Бот инициализируется слишком долго. Попробуйте позже.', 0

        return f'Ошибка сервиса Web DeepSeek (код {response.status_code}).', 0


def _extract_sambanova_answer(obj: dict) -> str:
    choices = obj.get('choices') or []
    if not choices:
        return ''

    first_message = choices[0].get('message', {})
    assistant_content = first_message.get('content')
    if not assistant_content:
        assistant_content = first_message.get('reasoning_content') or ''
    if not assistant_content:
        assistant_content = first_message.get('reasoning') or ''
    if not assistant_content:
        assistant_content = 'Пустой ответ от модели.'
    return assistant_content


async def _ask_sambanova_model_async(
    messages: str,
    user_id: int,
    model_name: str,
    *,
    max_tokens: int = 9000,
    temperature: Optional[float] = None,
) -> Tuple[str, Optional[int]]:
    if user_id not in hist:
        hist[user_id] = []
    hist[user_id].append({"role": "user", "content": messages})

    payload = {
        "model": model_name,
        "messages": hist[user_id],
        "max_tokens": max_tokens,
    }
    if temperature is not None:
        payload["temperature"] = temperature

    try:
        response = await asyncio.to_thread(
            requests.post,
            'https://api.sambanova.ai/v1/chat/completions',
            json=payload,
            headers={
                'Authorization': f'Bearer {SC_TOKEN}',
                'Content-Type': 'application/json',
            },
            proxies=proxies,
            timeout=30,
        )

        print(f"Response Status: {response.status_code}")
        if response.status_code != 200 or len(response.text) > 500:
            print(f"Response Content (truncated): {response.text[:500]}...")
        else:
            print(f"Response Content: {response.text}")

        if response.status_code != 200:
            if response.status_code == 429:
                return 'Превышен лимит запросов. Попробуйте позже.', '0'
            if response.status_code >= 500:
                return 'Ошибка сервера API. Попробуйте позже.', '0'
            return f'Ошибка API (код {response.status_code}).', '0'

        response_content = response.text
        if not response_content:
            raise ValueError("Пустой ответ от сервера.")

        try:
            obj = json.loads(response_content)
        except json.JSONDecodeError as e:
            print(f"Ошибка при декодировании JSON: {e}")
            return 'Что-то пошло не так с обработкой JSON.', '0'

        if 'choices' not in obj or not obj['choices']:
            print(f"Неожиданная структура ответа: {obj}")
            return 'Неожиданный формат ответа от сервера.', '0'

        completion_tokens = obj.get('usage', {}).get('completion_tokens', 0)
        assistant_content = _extract_sambanova_answer(obj)

        hist[user_id].append({"role": "assistant", "content": assistant_content})
        if len(hist[user_id]) > 20:
            hist[user_id] = hist[user_id][-20:]

        return assistant_content, completion_tokens

    except requests.exceptions.ConnectionError as e:
        print(f"Ошибка соединения: {e}")
        if "NameResolutionError" in str(e) or "Failed to resolve" in str(e):
            return 'Отсутствует подключение к интернету.', '0'
        if "Max retries exceeded" in str(e):
            return 'Отсутствует интернет-соединение.', '0'
        return 'Отсутствует интернет-соединение.', '0'

    except requests.exceptions.Timeout:
        print("Таймаут при подключении к API")
        return 'Таймаут при подключении к серверу. Попробуйте позже.', '0'

    except requests.exceptions.RequestException as e:
        print(f"Ошибка при выполнении запроса: {e}")
        return 'Ошибка при подключении к серверу API.', '0'

    except Exception as e:
        print(f"Общая ошибка: {type(e).__name__}: {e}")
        if "ConnectionError" in str(type(e).__name__) or "timeout" in str(e).lower():
            return 'Ошибка подключения. Ваш контекст сохранен, попробуйте позже.', '0'
        hist[user_id] = []
        return 'Что-то пошло не так. Контекст очищен, введите новый запрос.', '0'


async def ask_DeepSeek_R1_async(messages: str, user_id: int, timeout: float = 25.0) -> Tuple[str, Optional[int]]:

    if user_id not in hist:
        hist[user_id] = []
    hist[user_id].append({"role": "user", "content": messages})
    
    try:
        # Используем asyncio.wait_for для ограничения времени выполнения
        response = await asyncio.wait_for(
            asyncio.to_thread(
                requests.post,
                'https://api.sambanova.ai/v1/chat/completions',
                json={
                    "model": SAMBANOVA_MODEL_DEEPSEEK,
                    "messages": hist[user_id],
                    "max_tokens": 9000,
                    "temperature": 0.7,
                    "stream": False  # Убедимся, что stream выключен
                },
                headers={
                    'Authorization': f'Bearer {SC_TOKEN}',
                    'Content-Type': 'application/json'
                },
                proxies=proxies,
                timeout=30
            ),
            timeout=timeout
        )

        print(f"Response Status: {response.status_code}")
        
        # Ограничиваем вывод логов
        if response.status_code != 200 or len(response.text) > 500:
            print(f"Response Content (truncated): {response.text[:500]}...")
        else:
            print(f"Response Content: {response.text}")

        if response.status_code != 200:
            if response.status_code == 429:
                return 'Превышен лимит запросов. Попробуйте позже.', '0'
            elif response.status_code >= 500:
                return 'Ошибка сервера API. Попробуйте позже.', '0'
            else:
                return f'Ошибка API (код {response.status_code}).', '0'
        
        response_content = response.text
        if not response_content:
            raise ValueError("Пустой ответ от сервера.")

        try:
            obj = json.loads(response_content)
        except json.JSONDecodeError as e:
            print(f"Ошибка при декодировании JSON: {e}")
            return 'Что-то пошло не так с обработкой JSON.', '0'

        # Проверяем структуру ответа
        if 'choices' not in obj or not obj['choices']:
            print(f"Неожиданная структура ответа: {obj}")
            return 'Неожиданный формат ответа от сервера.', '0'
        
        completion_tokens = obj.get('usage', {}).get('completion_tokens', 0)
        assistant_content = obj['choices'][0]['message']['content']
        hist[user_id].append({"role": "assistant", "content": assistant_content})
        
        # Ограничиваем размер истории
        if len(hist[user_id]) > 20:
            hist[user_id] = hist[user_id][-20:]
            
        return assistant_content, completion_tokens
    
    except AsyncTimeoutError:
        print(f"Таймаут запроса к DeepSeek-R1 (превышено {timeout} сек)")
        # При таймауте не очищаем историю - пользователь может повторить
        return f'Таймаут запроса ({timeout} сек). Сервер долго не отвечает. Попробуйте позже или уменьшите запрос.', '0'
    
    except requests.exceptions.Timeout:
        print("Таймаут при подключении к API (requests timeout)")
        return 'Таймаут при подключении к серверу. Попробуйте позже.', '0'
    
    except Exception as e:
        error_str = str(e)
        error_type = type(e).__name__
        print(f"Ошибка при запросе к DeepSeek-R1 ({error_type}): {error_str[:200]}")
        
        # Единая проверка для всех сетевых ошибок
        if any(phrase in error_str for phrase in [
            "NameResolutionError",
            "Failed to resolve",
            "Max retries exceeded",
            "HTTPSConnectionPool",
            "Name or service not known",
            "ConnectionError",
            "timeout",
            "Timeout"
        ]):
            # Это сетевая ошибка, не очищаем историю
            return 'Отсутствует подключение к интернету.', '0'
        
        # Проверяем KeyError
        if "KeyError" in error_type and ("'choices'" in error_str or "choices" in error_str):
            print(f"Ошибка в структуре ответа AI модели: {e}")
            return 'Ошибка в ответе от сервера AI.', '0'
        
        # Очищаем историю для всех других ошибок
        hist[user_id] = []
        return 'Что-то пошло не так. Контекст очищен, введите новый запрос.', '0'


async def ask_DeepSeek_R1_Distill_Llama_70B_async(messages: str, user_id: int) -> Tuple[str, Optional[int]]:
    return await _ask_sambanova_model_async(
        messages,
        user_id,
        SAMBANOVA_MODEL_DEEPSEEK_R1_DISTILL_LLAMA_70B,
        max_tokens=9000,
        temperature=0.7,
    )


async def ask_DeepSeek_V3_1_async(messages: str, user_id: int) -> Tuple[str, Optional[int]]:
    return await _ask_sambanova_model_async(
        messages,
        user_id,
        SAMBANOVA_MODEL_DEEPSEEK_V3_1,
        max_tokens=9000,
        temperature=0.7,
    )


async def ask_DeepSeek_V3_1_cb_async(messages: str, user_id: int) -> Tuple[str, Optional[int]]:
    return await _ask_sambanova_model_async(
        messages,
        user_id,
        SAMBANOVA_MODEL_DEEPSEEK_V3_1_CB,
        max_tokens=9000,
    )


async def ask_DeepSeek_V3_2_async(messages: str, user_id: int) -> Tuple[str, Optional[int]]:
    return await _ask_sambanova_model_async(
        messages,
        user_id,
        SAMBANOVA_MODEL_DEEPSEEK_V3_2,
        max_tokens=9000,
    )


async def ask_Llama_4_Maverick_17B_128E_Instruct_async(messages: str, user_id: int) -> Tuple[str, Optional[int]]:
    return await _ask_sambanova_model_async(
        messages,
        user_id,
        SAMBANOVA_MODEL_LLAMA_4_MAVERICK_17B_128E_INSTRUCT,
        max_tokens=9000,
    )


async def ask_Meta_Llama_3_3_70B_Instruct_async(messages: str, user_id: int) -> Tuple[str, Optional[int]]:
    return await _ask_sambanova_model_async(
        messages,
        user_id,
        SAMBANOVA_MODEL_META_LLAMA_3_3_70B_INSTRUCT,
        max_tokens=9000,
    )


async def ask_MiniMax_M2_5_async(messages: str, user_id: int) -> Tuple[str, Optional[int]]:
    return await _ask_sambanova_model_async(
        messages,
        user_id,
        SAMBANOVA_MODEL_MINIMAX_M2_5,
        max_tokens=9000,
    )


async def ask_Gemma_3_12b_it_async(messages: str, user_id: int) -> Tuple[str, Optional[int]]:
    return await _ask_sambanova_model_async(
        messages,
        user_id,
        SAMBANOVA_MODEL_GEMMA_3_12B_IT,
        max_tokens=9000,
    )

async def ask_Meta_Llama_3_1_70B_Instruct_async(messages: str, user_id: int) -> str:
    if user_id not in hist:
        hist[user_id] = []
    hist[user_id].append({"role": "user", "content": messages})
    try:
        response = await asyncio.to_thread(requests.post, 'https://api.sambanova.ai/v1/chat/completions', json={
            "model": SAMBANOVA_MODEL_META,
            "messages": hist[user_id],
            "max_tokens": 9000
        }, headers={
            'Authorization': f'Bearer {SC_TOKEN}',
        },
        proxies=proxies,
        timeout=30
        )

        print(f"Response Status: {response.status_code}")
        
        # Ограничиваем вывод логов
        if response.status_code != 200 or len(response.text) > 500:
            print(f"Response Content (truncated): {response.text[:500]}...")
        else:
            print(f"Response Content: {response.text}")

        if response.status_code != 200:
            if response.status_code == 429:
                return 'Превышен лимит запросов. Попробуйте позже.', '0'
            elif response.status_code >= 500:
                return 'Ошибка сервера API. Попробуйте позже.', '0'
            else:
                return f'Ошибка API (код {response.status_code}).', '0'
        
        response_content = response.text
        if not response_content:
            raise ValueError("Пустой ответ от сервера.")

        try:
            obj = json.loads(response_content)
        except json.JSONDecodeError as e:
            print(f"Ошибка при декодировании JSON: {e}")
            return 'Что-то пошло не так с обработкой JSON.', '0'

        # Проверяем структуру ответа
        if 'choices' not in obj or not obj['choices']:
            print(f"Неожиданная структура ответа: {obj}")
            return 'Неожиданный формат ответа от сервера.', '0'
        
        completion_tokens = obj.get('usage', {}).get('completion_tokens', 0)
        assistant_content = obj['choices'][0]['message']['content']
        hist[user_id].append({"role": "assistant", "content": assistant_content})
        
        # Ограничиваем размер истории
        if len(hist[user_id]) > 20:
            hist[user_id] = hist[user_id][-20:]
            
        return assistant_content, completion_tokens
    
    except AsyncTimeoutError:
        print(f"Таймаут запроса к DeepSeek-R1 (превышено {timeout} сек)")
        # При таймауте не очищаем историю - пользователь может повторить
        return f'Таймаут запроса ({timeout} сек). Сервер долго не отвечает. Попробуйте позже или уменьшите запрос.', '0'
    
    except requests.exceptions.Timeout:
        print("Таймаут при подключении к API (requests timeout)")
        return 'Таймаут при подключении к серверу. Попробуйте позже.', '0'
    
    except Exception as e:
        error_str = str(e)
        error_type = type(e).__name__
        print(f"Ошибка при запросе к DeepSeek-R1 ({error_type}): {error_str[:200]}")
        
        # Единая проверка для всех сетевых ошибок
        if any(phrase in error_str for phrase in [
            "NameResolutionError",
            "Failed to resolve",
            "Max retries exceeded",
            "HTTPSConnectionPool",
            "Name or service not known",
            "ConnectionError",
            "timeout",
            "Timeout"
        ]):
            # Это сетевая ошибка, не очищаем историю
            return 'Отсутствует подключение к интернету.', '0'
        
        # Проверяем KeyError
        if "KeyError" in error_type and ("'choices'" in error_str or "choices" in error_str):
            print(f"Ошибка в структуре ответа AI модели: {e}")
            return 'Ошибка в ответе от сервера AI.', '0'
        
        # Очищаем историю для всех других ошибок
        hist[user_id] = []
        return 'Что-то пошло не так. Контекст очищен, введите новый запрос.', '0'



async def ask_Mixtral_8x22b_async(messages: str, user_id: int) -> Tuple[str, Optional[int]]:
    if user_id not in hist:
        hist[user_id] = []
    hist[user_id].append({"role": "user", "content": messages})

    try:
        response = await asyncio.to_thread(
            requests.post,
            'https://api.sambanova.ai/v1/chat/completions',
            json={
                # Mixtral alias routes to a currently available Samba model.
                "model": SAMBANOVA_MODEL_MIXTRAL_ALIAS,
                "messages": hist[user_id],
                "max_tokens": 9000
            },
            headers={
                'Authorization': f'Bearer {SC_TOKEN}',
                'Content-Type': 'application/json'
            },
            proxies=proxies,
            timeout=30
        )

        print(f"Response Status: {response.status_code}")
        if response.status_code != 200 or len(response.text) > 500:
            print(f"Response Content (truncated): {response.text[:500]}...")
        else:
            print(f"Response Content: {response.text}")

        if response.status_code != 200:
            if response.status_code == 429:
                return 'Превышен лимит запросов. Попробуйте позже.', '0'
            elif response.status_code >= 500:
                return 'Ошибка сервера API. Попробуйте позже.', '0'
            else:
                return f'Ошибка API (код {response.status_code}).', '0'

        response_content = response.text
        if not response_content:
            raise ValueError("Пустой ответ от сервера.")

        try:
            obj = json.loads(response_content)
        except json.JSONDecodeError as e:
            print(f"Ошибка при декодировании JSON: {e}")
            return 'Что-то пошло не так с обработкой JSON.', '0'

        if 'choices' not in obj or not obj['choices']:
            print(f"Неожиданная структура ответа: {obj}")
            return 'Неожиданный формат ответа от сервера.', '0'

        completion_tokens = obj.get('usage', {}).get('completion_tokens', 0)
        assistant_content = obj['choices'][0]['message']['content']
        hist[user_id].append({"role": "assistant", "content": assistant_content})

        if len(hist[user_id]) > 20:
            hist[user_id] = hist[user_id][-20:]

        return assistant_content, completion_tokens

    except requests.exceptions.Timeout:
        print("Таймаут при подключении к API (requests timeout)")
        return 'Таймаут при подключении к серверу. Попробуйте позже.', '0'

    except Exception as e:
        error_str = str(e)
        error_type = type(e).__name__
        print(f"Ошибка при запросе к Mixtral ({error_type}): {error_str[:200]}")

        if any(phrase in error_str for phrase in [
            "NameResolutionError",
            "Failed to resolve",
            "Max retries exceeded",
            "HTTPSConnectionPool",
            "Name or service not known",
            "ConnectionError",
            "timeout",
            "Timeout"
        ]):
            return 'Отсутствует подключение к интернету.', '0'

        if "KeyError" in error_type and ("'choices'" in error_str or "choices" in error_str):
            return 'Ошибка в ответе от сервера AI.', '0'

        hist[user_id] = []
        return 'Что-то пошло не так. Контекст очищен, введите новый запрос.', '0'


async def ask_Mistral_Nemo_Instruct_async(messages: str, user_id: int) -> str:
    if user_id not in hist:
        hist[user_id] = []
    client = InferenceClient(
        "mistralai/Mistral-Nemo-Instruct-2407",
        token=HF_TOKEN,
    )
    answer = ""
    hist[user_id].append({"role": "user", "content": messages})
    async for message in client.chat_completion(
        messages=hist[user_id],
        max_tokens=9000,
        stream=True,
    ):
        answer += message.choices[0].delta.content
    hist[user_id].append({"role": "assistant", "content": answer})
    return answer


async def ask_Gemma_7b_async(messages: str, user_id: int) -> str:
    if user_id not in hist:
        hist[user_id] = []
    hist[user_id].append({"role": "user", "content": messages})
    try:
        response = await asyncio.to_thread(requests.post,'https://api.groq.com/openai/v1/chat/completions', json={
            "model": "gemma-7b-it",
                "messages": hist[user_id],
            "max_tokens": 8192
        }, headers={
            'Authorization': f'Bearer {GROQ_TOKEN}',
        }, proxies=proxies
        )
        response_content = response.content.decode('utf-8')
        if not response_content:
            raise ValueError("Пустой ответ от сервера.")
        obj = json.loads(response_content)
        hist[user_id].append({"role": "assistant", "content": obj['choices'][0]['message']['content']})
        return obj['choices'][0]['message']['content']
    except json.JSONDecodeError as e:
        print(f"Ошибка при декодировании JSON: {e}")
        print(f"Содержимое ответа: {response_content}")
        return 'Что-то пошло не так с обработкой JSON.'
    except Exception as e:
        print(f"Общая ошибка: {e}")
        return 'Что-то пошло не так.'

async def send_prompt_async(msg: str, access_token: str) -> str:
    url = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"
    payload = json.dumps({
        "model": "GigaChat-Pro",
        "messages": [
            {
                "role": "user",
                "content": msg,
            }
        ],
    })
    headers = {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        'Authorization': f'Bearer {access_token}'
    }
    response = await asyncio.to_thread(requests.post, url, headers=headers, data=payload, verify=False)
    response_content = response.content.decode('utf-8')
    try:
        return response.json()["choices"][0]["message"]["content"]
    except json.JSONDecodeError as e:
        print(f"Ошибка при декодировании JSON: {e}")
        print(f"Содержимое ответа: {response_content}")
        return 'Что-то пошло не так с обработкой JSON.'
    except Exception as e:
        print(f"Общая ошибка: {e}")
        return 'Что-то пошло не так.'

async def ask_Gpt_oss_120b_async(messages: str, user_id: int) -> Tuple[str, Optional[int]]:
    if user_id not in hist:
        hist[user_id] = []
    hist[user_id].append({"role": "user", "content": messages})
    
    try:
        response = await asyncio.to_thread(
            requests.post,
            'https://api.sambanova.ai/v1/chat/completions',
            json={
                "model": SAMBANOVA_MODEL_GPT_OSS,
                "messages": hist[user_id],
                "max_tokens": 8192
            },
            headers={
                'Authorization': f'Bearer {SC_TOKEN}',
                'Content-Type': 'application/json',
            },
            proxies=proxies,
            timeout=30  # Добавляем таймаут для запроса
        )

        # Логирование статуса ответа и содержимого
        print(f"Response Status: {response.status_code}")
        print(f"Response Content: {response.text}")

        if response.status_code != 200:
            if response.status_code == 429:
                return 'Превышен лимит запросов. Попробуйте позже.', '0'
            elif response.status_code >= 500:
                return 'Ошибка сервера API. Попробуйте позже.', '0'
            else:
                return f'Ошибка API (код {response.status_code}).', '0'
        
        response_content = response.text  # Используем text вместо content.decode()
        if not response_content:
            raise ValueError("Пустой ответ от сервера.")

        try:
            obj = json.loads(response_content)
        except json.JSONDecodeError as e:
            print(f"Ошибка при декодировании JSON: {e}")
            print(f"Содержимое ответа: {response_content}")
            return 'Что-то пошло не так с обработкой JSON.', '0'

        completion_tokens = obj.get('usage', {}).get('completion_tokens', 0)
        first_message = obj['choices'][0].get('message', {})
        assistant_content = first_message.get('content')

        # gpt-oss-120b can return reasoning with content=null on short completions.
        if not assistant_content:
            assistant_content = first_message.get('reasoning') or ''

        if not assistant_content:
            assistant_content = 'Пустой ответ от модели.'

        hist[user_id].append({"role": "assistant", "content": assistant_content})
        return assistant_content, completion_tokens
    
    except requests.exceptions.ConnectionError as e:
        print(f"Ошибка соединения: {e}")
        
        # Проверяем конкретные типы ошибок соединения
        if "NameResolutionError" in str(e) or "Failed to resolve" in str(e):
            return 'Отсутствует подключение к интернету.', '0'
        elif "Max retries exceeded" in str(e):
            return 'Отсутствует интернет-соединение.', '0'
        else:
            return 'Отсутствует интернет-соединение.', '0'
    
    except requests.exceptions.Timeout:
        print("Таймаут при подключении к API")
        return 'Таймаут при подключении к серверу. Попробуйте позже.', '0'
    
    except requests.exceptions.RequestException as e:
        print(f"Ошибка при выполнении запроса: {e}")
        return 'Ошибка при подключении к серверу API.', '0'
    
    except KeyError as e:
        if "'choices'" in str(e) or "choices" in str(e):
            print(f"Ошибка в структуре ответа AI модели: {e}")
            # Не очищаем историю при этой ошибке
            return 'Ошибка в ответе от сервера AI.', '0'
        else:
            print(f"Ключевая ошибка: {e}")
            raise
    
    except Exception as e:
        print(f"Общая ошибка: {type(e).__name__}: {e}")
        # Очищаем историю только при определенных ошибках
        if "ConnectionError" in str(type(e).__name__) or "timeout" in str(e).lower():
            # Не очищаем историю при сетевых ошибках
            return 'Ошибка подключения. Ваш контекст сохранен, попробуйте позже.', '0'
        else:
            # Очищаем историю при других ошибках
            hist[user_id] = []
            return 'Что-то пошло не так. Контекст очищен, введите новый запрос.', '0'
        
async def ask_Web_DeepSeek_Thinking_async(msg: str, user_id: int) -> str:
    #проверка на hist делается на стороне сервера. пользователю достаточно просто отправить промпт
    try:
        return await _ask_web_deepseek_common(msg, user_id, thinking=True)
    
    except requests.exceptions.ConnectionError as e:
        print(f"Ошибка соединения: {e}")
        
        # Проверяем конкретные типы ошибок соединения
        if "NameResolutionError" in str(e) or "Failed to resolve" in str(e):
            return 'Отсутствует подключение к интернету.', '0'
        elif "Max retries exceeded" in str(e):
            return 'Отсутствует интернет-соединение.', '0'
        else:
            return 'Отсутствует интернет-соединение.', '0'
    
    except requests.exceptions.Timeout:
        print("Таймаут при подключении к API")
        return 'Таймаут при подключении к серверу. Попробуйте позже.', '0'
    
    except requests.exceptions.RequestException as e:
        print(f"Ошибка при выполнении запроса: {e}")
        return 'Ошибка при подключении к серверу API.', '0'
    
    except KeyError as e:
        if "'choices'" in str(e) or "choices" in str(e):
            print(f"Ошибка в структуре ответа AI модели: {e}")
            # Не очищаем историю при этой ошибке
            return 'Ошибка в ответе от сервера AI.', '0'
        else:
            print(f"Ключевая ошибка: {e}")
            raise
    
    except Exception as e:
        print(f"Общая ошибка: {type(e).__name__}: {e}")
        # Очищаем историю только при определенных ошибках
        if "ConnectionError" in str(type(e).__name__) or "timeout" in str(e).lower():
            # Не очищаем историю при сетевых ошибках
            return 'Ошибка подключения. Ваш контекст сохранен, попробуйте позже.', '0'
        return 'Что-то пошло не так при обработке запроса.', '0'

async def ask_Web_DeepSeek_async(msg: str, user_id: int) -> str:
    #проверка на hist делается на стороне сервера. пользователю достаточно просто отправить промпт
    try:
        return await _ask_web_deepseek_common(msg, user_id, thinking=False)
    
    except requests.exceptions.ConnectionError as e:
        print(f"Ошибка соединения: {e}")
        
        # Проверяем конкретные типы ошибок соединения
        if "NameResolutionError" in str(e) or "Failed to resolve" in str(e):
            return 'Отсутствует подключение к интернету.', '0'
        elif "Max retries exceeded" in str(e):
            return 'Отсутствует интернет-соединение.', '0'
        else:
            return 'Отсутствует интернет-соединение.', '0'
    
    except requests.exceptions.Timeout:
        print("Таймаут при подключении к API")
        return 'Таймаут при подключении к серверу. Попробуйте позже.', '0'
    
    except requests.exceptions.RequestException as e:
        print(f"Ошибка при выполнении запроса: {e}")
        return 'Ошибка при подключении к серверу API.', '0'
    
    except KeyError as e:
        if "'choices'" in str(e) or "choices" in str(e):
            print(f"Ошибка в структуре ответа AI модели: {e}")
            # Не очищаем историю при этой ошибке
            return 'Ошибка в ответе от сервера AI.', '0'
        else:
            print(f"Ключевая ошибка: {e}")
            raise
    
    except Exception as e:
        print(f"Общая ошибка: {type(e).__name__}: {e}")
        # Очищаем историю только при определенных ошибках
        if "ConnectionError" in str(type(e).__name__) or "timeout" in str(e).lower():
            # Не очищаем историю при сетевых ошибках
            return 'Ошибка подключения. Ваш контекст сохранен, попробуйте позже.', '0'
        return 'Что-то пошло не так при обработке запроса.', '0'
