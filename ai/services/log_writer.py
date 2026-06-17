"""AIRequestLog writing service for the WebSocket consumer."""

from asgiref.sync import sync_to_async
from django.utils import timezone


_ERROR_MARKERS = (
    "ошибка",
    "error",
    "таймаут",
    "timeout",
    "не удалось",
    "failed",
    "недоступ",
    "unavailable",
    "превышен лимит",
    "rate limit",
)


class LogWriter:
    """Create and update AIRequestLog records from the WebSocket consumer."""

    @sync_to_async
    def create(
        self,
        *,
        user,
        username: str,
        external_user_id: str,
        user_full_name: str,
        client_id: str,
        source: str,
        mode: str,
        sent_at,
        model_names: list,
        message: str,
        programming_language_id: int | None,
        programming_language_name: str,
        topic_id: int | None,
        topic_name: str,
        prompt_id: int | None,
        prompt_name: str,
    ):
        from ..models import AIRequestLog

        return AIRequestLog.objects.create(
            user=user,
            username=username,
            external_user_id=external_user_id,
            user_full_name=user_full_name,
            client_id=client_id,
            source=source,
            mode=mode,
            sent_at=sent_at,
            model_names=model_names,
            message=message,
            programming_language_id=programming_language_id,
            programming_language_name=programming_language_name,
            topic_id=topic_id,
            topic_name=topic_name,
            prompt_id=prompt_id,
            prompt_name=prompt_name,
        )

    @sync_to_async
    def update_success(self, log, response_text: str, tokens: int, model_title: str, end_time=None) -> None:
        from ..models import AIRequestLog

        if end_time is None:
            end_time = timezone.now()

        log.received_at = end_time
        log.duration_seconds = (end_time - log.sent_at).total_seconds() if log.sent_at else None
        log.model_names = [model_title] if model_title else log.model_names
        log.response_text = str(response_text or "")[:5000]
        log.tokens = tokens or 0

        text_sample = str(response_text or "").lower()[:100]
        if any(marker in text_sample for marker in _ERROR_MARKERS):
            log.status = AIRequestLog.STATUS_ERROR
        else:
            log.status = AIRequestLog.STATUS_SUCCESS

        log.save(
            update_fields=[
                "received_at",
                "duration_seconds",
                "model_names",
                "response_text",
                "tokens",
                "status",
            ]
        )

    @sync_to_async
    def update_error(
        self,
        log,
        friendly: str,
        detailed: str,
        end_time=None,
    ) -> None:
        from ..models import AIRequestLog

        if end_time is None:
            end_time = timezone.now()

        log.received_at = end_time
        log.duration_seconds = (end_time - log.sent_at).total_seconds() if log.sent_at else None
        log.status = AIRequestLog.STATUS_ERROR
        log.error_message = str(detailed or "")[:2000]
        log.response_text = str(friendly or "")[:5000]
        log.save(
            update_fields=[
                "received_at",
                "duration_seconds",
                "status",
                "error_message",
                "response_text",
            ]
        )
