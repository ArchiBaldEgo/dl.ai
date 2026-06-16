"""Prompt resolution service for WebSocket consumer."""

from asgiref.sync import sync_to_async

from ..i18n import get_localized_name
from ..models import ProgrammingLanguage, Prompt, SharedPrompt, Topic


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
    """Resolve prompt text and related names for a WebSocket request."""

    parse_shared_prompt_id = staticmethod(parse_shared_prompt_id)

    async def resolve_text(
        self,
        prompt_id,
        ui_language: str = "",
        programming_language_name: str = "",
        topic_name: str = "",
        message: str = "",
        code: str = "",
    ) -> str | None:
        """Return effective prompt text or None if prompt not found."""
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
        prog_lng_name = ""
        topic_name = ""
        prompt_name = ""

        if prog_lng_id:
            try:
                prog_lng_name = await self._get_programming_language_name(prog_lng_id)
            except ProgrammingLanguage.DoesNotExist:
                pass

        if topic_id:
            try:
                topic = await self._get_topic(topic_id)
                topic_name = get_localized_name(topic, ui_language, "topic_name")
            except Topic.DoesNotExist:
                pass

        try:
            prompt = await self._resolve_prompt(prompt_id)
            if prompt is not None:
                prompt_name = get_localized_name(prompt, ui_language, "prompt_name")
        except (Prompt.DoesNotExist, SharedPrompt.DoesNotExist):
            pass

        return prog_lng_name, topic_name, prompt_name

    @sync_to_async
    def _get_programming_language_name(self, prog_lng_id) -> str:
        return ProgrammingLanguage.objects.values_list("language_name", flat=True).get(id=prog_lng_id)

    @sync_to_async
    def _get_topic(self, topic_id):
        return Topic.objects.get(id=topic_id)

    @sync_to_async
    def _get_shared_prompt(self, shared_pk: int):
        return SharedPrompt.objects.get(id=shared_pk)

    @sync_to_async
    def _get_prompt(self, pk: int):
        return Prompt.objects.select_related("shared_prompt").get(id=pk)

    async def _resolve_prompt(self, prompt_id):
        shared_pk = parse_shared_prompt_id(prompt_id)
        if shared_pk is not None:
            return await self._get_shared_prompt(shared_pk)
        if prompt_id:
            try:
                return await self._get_prompt(int(prompt_id))
            except (ValueError, TypeError):
                return None
        return None


@sync_to_async
def get_default_shared_prompt(mode: str) -> SharedPrompt | None:
    try:
        return SharedPrompt.objects.get(mode=mode)
    except SharedPrompt.DoesNotExist:
        return None
