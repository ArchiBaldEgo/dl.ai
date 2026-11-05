"""Сервис резолвинга промптов для WebSocket consumer."""

from asgiref.sync import sync_to_async

from ..i18n import get_localized_name


def parse_shared_prompt_id(prompt_id) -> int | None:
    """Return shared prompt pk if prompt_id is 'shared_<pk>', else None."""
    if not isinstance(prompt_id, str):
        return None
    if not prompt_id.startswith("shared_"):
        return None
    try:
        return int(prompt_id.split("_", 1)[1])
    except (ValueError, IndexError):
        return None


class PromptResolver:
    """Резолвит текст промпта и связанные имена (язык, тема) для WebSocket-запроса."""

    parse_shared_prompt_id = staticmethod(parse_shared_prompt_id)

    async def resolve_text(
        self,
        prompt_id,
        ui_language: str = "",
        programming_language_name: str = "",
        topic_name: str = "",
        message: str = "",
        code: str = "",
        allowed_mode: str = "",
    ) -> str | None:
        """Return effective prompt text or None if prompt not found.

        allowed_mode — режим страницы ('solve'/'find_error'): обычный промпт
        чужого режима не применяется (страховка от протухшего ai_state /
        ручной подделки WS-пейлоада). Общие промпты ('shared_<pk>') не
        фильтруются.
        """
        from ..models import Prompt, SharedPrompt

        shared_pk = parse_shared_prompt_id(prompt_id)
        try:
            if shared_pk is not None:
                prompt = await self._get_shared_prompt(shared_pk)
            elif prompt_id:
                prompt = await self._get_prompt(int(prompt_id))
            else:
                return None
        except (Prompt.DoesNotExist, SharedPrompt.DoesNotExist, ValueError, TypeError):
            return None

        if prompt is None:
            return None
        if shared_pk is None and allowed_mode and prompt.mode and prompt.mode != allowed_mode:
            return None
        return prompt.get_effective_text(
            ui_language,
            programming_language_name,
            topic_name,
            message,
            code,
        )

    async def resolve_context_names(
        self,
        prog_lng_id,
        topic_id,
        prompt_id,
        ui_language: str = "",
    ) -> tuple[str, str, str]:
        """Return (programming_language_name, topic_name, prompt_name) for logging."""
        # Один thread-hop на все три запроса вместо трёх sync_to_async
        # (каждый — переключение потока и, на некоторых бэкендах, работа в
        # отдельном воркере). Семантика резолва та же.
        return await self._load_context_names(
            prog_lng_id, topic_id, prompt_id, ui_language,
        )

    @sync_to_async
    def _load_context_names(
        self, prog_lng_id, topic_id, prompt_id, ui_language,
    ) -> tuple[str, str, str]:
        from ..models import ProgrammingLanguage, Prompt, SharedPrompt, Topic

        prog_lng_name = ""
        if prog_lng_id:
            prog_lng_name = (
                ProgrammingLanguage.objects
                .filter(pk=prog_lng_id)
                .values_list("language_name", flat=True)
                .first()
            ) or ""

        topic_name = ""
        if topic_id:
            topic = Topic.objects.filter(pk=topic_id).first()
            if topic is not None:
                topic_name = get_localized_name(topic, ui_language, "topic_name")

        prompt_name = ""
        prompt = None
        shared_pk = parse_shared_prompt_id(prompt_id)
        try:
            if shared_pk is not None:
                prompt = SharedPrompt.objects.get(pk=shared_pk)
            elif prompt_id:
                prompt = Prompt.objects.select_related("shared_prompt").get(
                    pk=int(prompt_id),
                )
        except (Prompt.DoesNotExist, SharedPrompt.DoesNotExist, ValueError, TypeError):
            prompt = None
        if prompt is not None:
            prompt_name = get_localized_name(prompt, ui_language, "prompt_name")

        return prog_lng_name, topic_name, prompt_name

    @sync_to_async
    def _get_shared_prompt(self, shared_pk: int):
        from ..models import SharedPrompt
        return SharedPrompt.objects.get(id=shared_pk)

    @sync_to_async
    def _get_prompt(self, pk: int):
        from ..models import Prompt
        return Prompt.objects.select_related("shared_prompt").get(id=pk)


@sync_to_async
def get_default_shared_prompt(mode: str):
    """Общий промпт режима (solve/find_error) с кэшем на 60 с.

    Вызывается на каждом WS-сообщении; текст меняется редко. Ключ включает
    эпоху ``prompt_meta``, которую бампают сигналы SharedPrompt/Prompt, —
    правка промпта в админке видна сразу, без ожидания TTL. Возвращается
    копия, чтобы вызывающий код не мог мутировать закэшированный объект.
    """
    import copy

    from ..models import SharedPrompt
    from .cache_epochs import cache_key, cached_call, epoch

    def _load():
        try:
            return SharedPrompt.objects.get(mode=mode)
        except SharedPrompt.DoesNotExist:
            return None

    prompt = cached_call(
        cache_key("prompt", "default_shared", epoch("prompt_meta"), mode),
        _load,
        60,
    )
    return copy.copy(prompt) if prompt is not None else None
