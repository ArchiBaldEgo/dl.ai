import json
from channels.generic.websocket import AsyncWebsocketConsumer
import os
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

load_dotenv()
CLIENT_ID = os.getenv("CLIENT_ID")
SECRET = os.getenv("SBER_SECRET")
HF_TOKEN = os.getenv("HF_TOKEN")
SC_TOKEN=os.getenv("SC_TOKEN")
MIST_TOKEN = os.getenv("MIST_TOKEN")
GROQ_TOKEN = os.getenv("GROQ_TOKEN")
timeout = 0

hist=dict([])

proxies = {
            'http': os.getenv("PROXY"),
            'https': os.getenv("PROXY")
        }


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
                    "model": "DeepSeek-R1",
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
                return 'Превышен лимит запросов. Попробуйте позже.', 
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

async def ask_DeepSeek_R1_Distill_Llama_70B_async(messages: str, user_id: int) -> str:
    if user_id not in hist:
        hist[user_id] = []
    hist[user_id].append({"role": "user", "content": messages})
    try:
        response = await asyncio.to_thread(requests.post, 'https://api.sambanova.ai/v1/chat/completions', json={
            "model": "DeepSeek-R1-Distill-Llama-70B",
            "messages": hist[user_id],
            "max_tokens": 9000
        }, headers={
            'Authorization': f'Bearer {SC_TOKEN}',
            'Content-Type': 'application/json'
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
        '0'
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

async def ask_Llama_3_1_Tulu_3_405B_async(messages: str, user_id: int) -> str:
    if user_id not in hist:
        hist[user_id] = []
    hist[user_id].append({"role": "user", "content": messages})
    try:
        response = await asyncio.to_thread(requests.post, 'https://api.sambanova.ai/v1/chat/completions', json={
            "model": "Meta-Llama-3.1-405B-Instruct",
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


async def ask_Meta_Llama_3_1_70B_Instruct_async(messages: str, user_id: int) -> str:
    if user_id not in hist:
        hist[user_id] = []
    hist[user_id].append({"role": "user", "content": messages})
    try:
        response = await asyncio.to_thread(requests.post, 'https://api.sambanova.ai/v1/chat/completions', json={
            "model": "Meta-Llama-3.3-70B-Instruct",
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


async def ask_QwQ_32B_async(messages: str, user_id: int) -> str:
    if user_id not in hist:
        hist[user_id] = []
    hist[user_id].append({"role": "user", "content": messages})
    try:
        response = await asyncio.to_thread(requests.post, 'https://api.sambanova.ai/v1/chat/completions', json={
            "model": "QwQ-32B",
            "messages": hist[user_id],
            "max_tokens": 4000
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

async def ask_Mixtral_8x22b_async(messages: str, user_id: int) -> str:
    if user_id not in hist:
        hist[user_id] = []
    hist[user_id].append({"role": "user", "content": messages})
    try:
        response = await asyncio.to_thread(requests.post, 'https://api.mistral.ai/v1/chat/completions', json={
            "model": "open-mixtral-8x22b",
            "messages": hist[user_id],
            "max_tokens": 9000
        }, headers={
            'Authorization': f'Bearer {MIST_TOKEN}',
        }, proxies=proxies,
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
            'https://api.groq.com/openai/v1/chat/completions',
            json={
                "model": "openai/gpt-oss-120b",
                "messages": hist[user_id],
                "max_tokens": 8192
            },
            headers={
                'Authorization': f'Bearer {GROQ_TOKEN}',
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
        hist[user_id].append({"role": "assistant", "content": obj['choices'][0]['message']['content']})
        return obj['choices'][0]['message']['content'], completion_tokens
    
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
        