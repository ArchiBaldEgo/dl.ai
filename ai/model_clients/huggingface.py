"""HuggingFace InferenceClient model clients."""

from huggingface_hub import InferenceClient

from .config import HF_TOKEN
from .history import conversation_history


async def ask_Mistral_Nemo_Instruct_async(messages: str, user_id: int) -> str:
    history = conversation_history.get(user_id)
    client = InferenceClient("mistralai/Mistral-Nemo-Instruct-2407", token=HF_TOKEN)
    history.append({"role": "user", "content": messages})
    answer = ""
    async for message in client.chat_completion(
        messages=history,
        max_tokens=9000,
        stream=True,
    ):
        answer += message.choices[0].delta.content
    history.append({"role": "assistant", "content": answer})
    return answer


async def ask_Gemma_7b_async(messages: str, user_id: int) -> str:
    """Legacy Groq-hosted Gemma-7b client kept for backward compatibility."""
    import json

    import requests

    from .config import GROQ_TOKEN, proxies

    history = conversation_history.get(user_id)
    history.append({"role": "user", "content": messages})
    try:
        response = await __import__("asyncio").to_thread(
            requests.post,
            "https://api.groq.com/openai/v1/chat/completions",
            json={
                "model": "gemma-7b-it",
                "messages": history,
                "max_tokens": 8192,
            },
            headers={"Authorization": f"Bearer {GROQ_TOKEN}"},
            proxies=proxies,
        )
        response_content = response.content.decode("utf-8")
        if not response_content:
            raise ValueError("Пустой ответ от сервера.")
        obj = json.loads(response_content)
        assistant_content = obj["choices"][0]["message"]["content"]
        history.append({"role": "assistant", "content": assistant_content})
        return assistant_content
    except json.JSONDecodeError as e:
        print(f"Ошибка при декодировании JSON: {e}")
        print(f"Содержимое ответа: {response_content}")
        return "Что-то пошло не так с обработкой JSON."
    except Exception as e:
        print(f"Общая ошибка: {e}")
        return "Что-то пошло не так."
