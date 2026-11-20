"""Проверка текста через Pangram REST API."""

import os
from time import monotonic, sleep
from urllib.parse import quote

import requests

_TASK_URL = "https://text.external-api.pangram.com/task"
_WAIT_SECONDS = 25
_API_ERRORS = {
    400: (422, "Pangram не принял текст для проверки."),
    401: (503, "Pangram отклонил API-ключ."),
    402: (402, "На балансе Pangram недостаточно API-кредитов."),
    403: (503, "Для ключа Pangram нет доступа к выбранной модели."),
    422: (422, "Pangram не смог принять текст или выбранную модель."),
    429: (429, "Достигнут лимит запросов Pangram. Попробуйте позже."),
}


class PangramError(Exception):
    """Сообщение, которое можно показать пользователю."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def _request(method, url, api_key, deadline, **kwargs):
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise PangramError(504, "Pangram не завершил проверку вовремя.")

    try:
        response = requests.request(
            method, url, headers={"x-api-key": api_key},
            timeout=min(remaining, 10), allow_redirects=False, **kwargs,
        )
    except requests.Timeout as exc:
        raise PangramError(504, "Pangram не ответил вовремя.") from exc
    except requests.RequestException as exc:
        raise PangramError(502, "Не удалось связаться с Pangram.") from exc

    if not 200 <= response.status_code < 300:
        status, message = _API_ERRORS.get(
            response.status_code, (502, "Pangram вернул ошибку при проверке.")
        )
        raise PangramError(status, message)

    try:
        payload = response.json()
    except ValueError as exc:
        raise PangramError(502, "Pangram вернул некорректный ответ.") from exc
    if not isinstance(payload, dict):
        raise PangramError(502, "Pangram вернул некорректный ответ.")
    return payload


def _percent(result, field):
    value = result.get(field)
    if type(value) not in (int, float) or not 0 <= value <= 1:
        raise PangramError(502, "Pangram вернул некорректную оценку.")
    return round(value * 100, 1)


def check_text(text):
    api_key = os.getenv("PANGRAM_API_KEY", "").strip()
    if not api_key:
        raise PangramError(503, "На сервере не задан PANGRAM_API_KEY.")

    # Pangram сначала выдаёт номер задачи. Ждём результат в пределах одного таймаута.
    deadline = monotonic() + _WAIT_SECONDS
    task = _request(
        "POST", _TASK_URL, api_key, deadline,
        json={"text": text, "model": "pangram-4", "public_dashboard_link": False},
    )
    task_id = task.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise PangramError(502, "Pangram не вернул номер проверки.")

    task_url = f"{_TASK_URL}/{quote(task_id, safe='')}"
    while True:
        result = _request("GET", task_url, api_key, deadline)
        stage = result.get("stage")
        if stage == "STAGE_SUCCESS":
            return {
                "prediction": result.get("prediction_short"),
                "model_version": result.get("version"),
                "ai_percent": _percent(result, "fraction_ai"),
                "ai_assisted_percent": _percent(result, "fraction_ai_assisted"),
                "human_percent": _percent(result, "fraction_human"),
            }
        if stage == "STAGE_FAILED":
            raise PangramError(422, "Pangram не смог проанализировать этот текст.")
        if not isinstance(stage, str) or not stage:
            raise PangramError(502, "Pangram не вернул статус проверки.")
        sleep(min(0.5, max(0, deadline - monotonic())))
