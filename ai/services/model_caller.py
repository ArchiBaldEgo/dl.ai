"""Сервис вызова AI-моделей из WebSocket consumer."""

import re
from typing import Any

from asgiref.sync import sync_to_async

from ..i18n import ws_text
from ..model_clients import registry, supports_think
from ..model_clients.exceptions import humanize_model_error


# Регион/разделители не значимы: «Web_DeepSeek», «Web DeepSeek», «web-deepseek»
# — одно и то же. Клиент пишет в куку ai_state ключ опции селектора, но у
# пользователей со старыми куками (живут 1 год) там могут быть переименованные
# ключи или title — резолв-цепочка ниже сводит всё к актуальному ключу.
def _normalize_model_id(value: str) -> str:
    return re.sub(r"[-_ ]+", "", str(value or "").strip().lower())


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


def _resolve_model_key(registry_instance, model_key: str) -> str:
    """Свести входное имя модели к актуальному ключу реестра.

    Цепочка: точный ключ → legacy-алиас → нечувствительное к регистру и
    разделителям совпадение по ключам → вход пришёл title'ом (точно или с
    той же нормализацией). Пустая строка = не опознано.
    """
    if model_key and registry_instance.get(model_key):
        return model_key

    legacy = _resolve_legacy_alias(model_key)
    if legacy != model_key and registry_instance.get(legacy):
        return legacy

    if not model_key:
        return ""

    target = _normalize_model_id(model_key)
    for key in sorted(registry_instance.keys()):
        if _normalize_model_id(key) == target:
            return key

    title_to_key = registry_instance.title_to_key()
    if model_key in title_to_key:
        return title_to_key[model_key]
    for title, key in title_to_key.items():
        if _normalize_model_id(title) == target:
            return key
    return ""


class ModelCallResult:
    """Result of a model invocation."""

    def __init__(
        self,
        response_text: str = "",
        tokens: Any = 0,
        model_title: str = "",
        error_message: str = "",
        is_error: bool = False,
        prompt_tokens: Any = 0,
    ):
        self.response_text = response_text
        # tokens — токены ОТВЕТА (completion); prompt_tokens — токены ВХОДА.
        # Итог (prompt + completion) считается на странице «Контроль токенов».
        self.tokens = tokens
        self.prompt_tokens = prompt_tokens
        self.model_title = model_title
        self.error_message = error_message
        self.is_error = is_error


class ModelCaller:
    """Resolve a model key (including legacy aliases) and invoke its handler."""

    def __init__(self, registry_instance=registry):
        self.registry = registry_instance

    async def call(self, message: str, client_id: str, model_key: str,
                   language: str = "", think: bool | None = None,
                   user=None) -> ModelCallResult:
        # ЛИЧНАЯ модель пользователя (ai/services/user_models.py). Ключ вида
        # ``User_<pk>``: резолвится ТОЛЬКО владельцем — чужой ключ не
        # находится и уходит в общий «model_not_found». Личные модели НЕ
        # в глобальном реестре: health-check/ARM/статистика их не видят.
        if user is not None and str(model_key or "").strip().startswith("User_"):
            from .user_models import call_user_model, find_user_model

            personal = await sync_to_async(find_user_model, thread_sensitive=True)(
                user, model_key
            )
            if personal is None:
                error_text = ws_text(language, "model_not_found", model_key=model_key)
                return ModelCallResult(
                    response_text=error_text,
                    model_title=model_key,
                    error_message=error_text,
                    is_error=True,
                )
            response = await call_user_model(personal, message, client_id)
            return self._build_result(response, personal.title, language)

        normalized_key = _resolve_model_key(self.registry, model_key) or _resolve_legacy_alias(model_key)
        handler = self.registry.handler(normalized_key)
        title = self.registry.title(normalized_key)

        if handler is None:
            error_text = ws_text(language, "model_not_found", model_key=model_key)
            return ModelCallResult(
                response_text=error_text,
                model_title=model_key,
                error_message=error_text,
                is_error=True,
            )

        try:
            # ``think`` — клиентский переключатель рассуждений (кнопка Think):
            # kwarg в хендлер уходит ТОЛЬКО у провайдера с API-переключателем
            # (ollama, docs.ollama.com/capabilities/thinking) и только когда
            # клиент его прислал; None / прочие провайдеры — вызов без kwarg
            # (поле в запрос не подставляется, поведение модели по умолчанию).
            if think is not None and supports_think(handler):
                response = await handler(message, client_id, think=bool(think))
            else:
                response = await handler(message, client_id)
        except Exception as exc:
            friendly, detailed = humanize_model_error(str(exc), include_detail=True, language=language)
            return ModelCallResult(
                response_text=ws_text(language, "error_prefix", friendly=friendly),
                model_title=title,
                error_message=detailed,
                is_error=True,
            )

        return self._build_result(response, title, language)

    def _build_result(self, response, title: str, language: str = "") -> ModelCallResult:
        """Разбор ответа хендлера в ``ModelCallResult`` (контракт 5-элементного
        кортежа: content, completion_tokens, is_error, cut_flag, prompt_tokens).

        Общий разбор для глобальных моделей и личных моделей пользователя —
        одинаковый контракт, одинаковая обработка пустого ответа.
        """
        is_error = False
        prompt_tokens = 0
        tokens = 0
        if isinstance(response, tuple):
            response_text = response[0] if len(response) > 0 else ""
            tokens = response[1] if len(response) > 1 else 0
            # 5-й элемент кортежа — токены промпта (учёт prompt+completion).
            if len(response) > 4:
                prompt_tokens = response[4] or 0
            # Clients signal a 200-OK-but-error body (rate limit, «все боты
            # заняты», parse failure, …) via a 3rd tuple element = True. This
            # is the structured signal that replaces log_writer's text-marker
            # heuristic (which mis-flagged legit answers discussing «ошибка»).
            if len(response) > 2:
                is_error = bool(response[2])
        else:
            response_text = response

        response_text = str(response_text or "")

        # Guard: response must NEVER be empty — neither error nor success.
        # If the handler returned (content="", is_error=True), the error text
        # itself might be empty (e.g. bot pool returned 200 with empty content
        # and a generic error). Substitute a clear message in all cases.
        if not response_text.strip():
            response_text = ws_text(language, "empty_response")
            is_error = True

        return ModelCallResult(
            response_text=response_text,
            tokens=tokens,
            prompt_tokens=prompt_tokens,
            model_title=title,
            is_error=is_error,
        )
