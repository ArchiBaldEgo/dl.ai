"""GigaChat (Sber) client used by the legacy audio/chat flow."""

import json
import logging
import os
from typing import Optional

import requests

from .config import SECRET

logger = logging.getLogger(__name__)


def _gigachat_verify_ssl() -> bool:
    """Return False only when ``SKIP_SSL_VERIFICATION`` is explicitly enabled for dev."""
    return not os.getenv("SKIP_SSL_VERIFICATION", "").strip().lower() in ("1", "true", "yes", "on")


def get_gigachat_token() -> Optional[str]:
    """Obtain a GigaChat OAuth access token."""
    if not SECRET:
        return None
    import uuid

    from requests.auth import HTTPBasicAuth

    url = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    payload = {"scope": "GIGACHAT_API_PERS"}
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "RqUID": str(uuid.uuid4()),
    }
    response = requests.post(
        url,
        headers=headers,
        data=payload,
        auth=HTTPBasicAuth("client_id", SECRET),
        verify=_gigachat_verify_ssl(),
        timeout=30,
    )
    if response.status_code == 200:
        return response.json().get("access_token")
    return None


async def send_prompt_async(msg: str, access_token: str) -> str:
    """Send a single-turn prompt to GigaChat-Pro."""
    url = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"
    payload = json.dumps(
        {
            "model": "GigaChat-Pro",
            "messages": [{"role": "user", "content": msg}],
        }
    )
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {access_token}",
    }
    response = await __import__("asyncio").to_thread(
        requests.post,
        url,
        headers=headers,
        data=payload,
        verify=_gigachat_verify_ssl(),
    )
    response_content = response.content.decode("utf-8")
    try:
        return response.json()["choices"][0]["message"]["content"]
    except json.JSONDecodeError as e:
        logger.warning("JSON decode error: %s", e)
        logger.debug("Response content: %s", response_content)
        return "Что-то пошло не так с обработкой JSON."
    except Exception as e:
        logger.warning("Unexpected error in GigaChat call: %s", e)
        return "Что-то пошло не так."
