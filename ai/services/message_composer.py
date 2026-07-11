"""Message composition for WebSocket chat modes."""

from ..i18n import get_language_instruction
from .prompt_resolver import PromptResolver, get_default_shared_prompt


class ModeMessageBuilder:
    """Base class for composing a user message for a specific chat mode."""

    mode: str = ""

    async def build(self, data: dict, resolver: PromptResolver) -> str:
        raise NotImplementedError


class ChatModeBuilder(ModeMessageBuilder):
    async def build(self, data: dict, resolver: PromptResolver) -> str:
        from ..models import AIRequestLog

        self.mode = AIRequestLog.MODE_CHAT
        message = data.get("message", "")
        language = data.get("language", "Russian")
        preprompt = data.get("preprompt", "")
        topic_name = data.get("topic_name", "")

        if preprompt:
            prompt_text = await resolver.resolve_text(preprompt, language, "", topic_name)
            if prompt_text:
                prefix = "Preprompt" if language not in ("Русский", "Russian") else "Препромпт"
                return f"{message}\n\n{prefix}: {prompt_text}"
        return message


class SolveModeBuilder(ModeMessageBuilder):
    async def build(self, data: dict, resolver: PromptResolver) -> str:
        from ..models import AIRequestLog

        self.mode = AIRequestLog.MODE_SOLVE
        language = data.get("language", "Russian")
        message = data.get("message", "")
        prog_lng_name = data.get("programming_language_name", "")
        topic_name = data.get("topic_name", "")
        prompt_id = data.get("preprompt")

        default_prompt = await get_default_shared_prompt("solve")
        if default_prompt:
            base = default_prompt.get_effective_text(language, prog_lng_name, topic_name, message, "")
        else:
            base = self._build_default_message(language, prog_lng_name, topic_name, message)

        prompt_text = await resolver.resolve_text(prompt_id, language, prog_lng_name, topic_name, message)
        if prompt_text:
            prefix = "Preprompt" if language not in ("Русский", "Russian") else "Препромпт"
            base += f"\n\n{prefix}: {prompt_text}"
        return base

    def _build_default_message(self, ui_language, prog_lng_name, topic_name, message):
        if ui_language == "English":
            return f"I have a programming problem in {prog_lng_name}, topic {topic_name}. Solve it.\n\nProblem:\n{message}"
        if ui_language == "Français":
            return f"J'ai un problème de programmation en {prog_lng_name}, sujet {topic_name}. Résolvez-le.\n\nProblème:\n{message}"
        return f"У меня есть задача по программированию на языке {prog_lng_name}, тема {topic_name}. Реши задачу.\n\nЗадача:\n{message}"


class FindErrorModeBuilder(ModeMessageBuilder):
    async def build(self, data: dict, resolver: PromptResolver) -> str:
        from ..models import AIRequestLog

        self.mode = AIRequestLog.MODE_FIND_ERROR
        language = data.get("language", "Russian")
        message = data.get("message", "")
        code = data.get("code", "")
        prog_lng_name = data.get("programming_language_name", "")
        topic_name = data.get("topic_name", "")
        prompt_id = data.get("preprompt")

        default_prompt = await get_default_shared_prompt("find_error")
        if default_prompt:
            base = default_prompt.get_effective_text(language, prog_lng_name, topic_name, message, code)
        else:
            base = self._build_default_message(language, prog_lng_name, topic_name, message, code)

        prompt_text = await resolver.resolve_text(prompt_id, language, prog_lng_name, topic_name, message, code)
        if prompt_text:
            prefix = "Preprompt" if language not in ("Русский", "Russian") else "Препромпт"
            base += f"\n\n{prefix}: {prompt_text}"
        return base

    def _build_default_message(self, ui_language, prog_lng_name, topic_name, message, code):
        if ui_language == "English":
            return (
                f"I have a programming problem in {prog_lng_name}, topic {topic_name}. I wrote code, but it does not work. "
                f"Find the error.\n\nProblem:\n{message}\n\nCode:\n{code}"
            )
        if ui_language == "Français":
            return (
                f"J'ai un problème de programmation en {prog_lng_name}, sujet {topic_name}. J'ai écrit du code, mais il ne fonctionne pas. "
                f"Trouvez l'erreur.\n\nProblème:\n{message}\n\nCode:\n{code}"
            )
        return (
            f"У меня есть задача по программированию на языке {prog_lng_name}, тема {topic_name}. Я написал код, но он не работает. "
            f"Найди ошибку.\n\nЗадача:\n{message}\n\nКод:\n{code}"
        )


_MODE_BUILDERS: dict[str, type[ModeMessageBuilder]] = {
    "1": ChatModeBuilder,
    "2": SolveModeBuilder,
    "3": FindErrorModeBuilder,
}


class MessageComposer:
    """Compose the final message sent to the AI model based on request data."""

    def __init__(self, resolver: PromptResolver | None = None):
        self.resolver = resolver or PromptResolver()
        self._builders: dict[str, ModeMessageBuilder] = {
            key: cls() for key, cls in _MODE_BUILDERS.items()
        }

    def register_builder(self, mode_key: str, builder: ModeMessageBuilder) -> None:
        self._builders[mode_key] = builder

    async def compose(self, data: dict, previous_language: str | None = None) -> tuple[str, str]:
        """Return (message, log_mode)."""
        message_type = str(data.get("type", "1"))
        message = data.get("message", "")
        language = data.get("language", "Russian")

        # Always add a language instruction for non-Russian UI languages, so the
        # AI replies in the user's selected language. The previous-language gate
        # only suppressed the instruction when the language was unchanged — which
        # meant the very first message in EN/FR got a Russian reply.
        if language and language not in ("Russian", "Русский", ""):
            message += get_language_instruction(language)
            data["message"] = message  # propagate to builders

        builder = self._builders.get(message_type)
        if builder is None:
            return message, ""

        composed = await builder.build(data, self.resolver)
        log_mode = builder.mode
        return composed, log_mode

    def mode_from_message_type(self, message_type) -> str:
        builder = self._builders.get(str(message_type))
        return builder.mode if builder else ""
