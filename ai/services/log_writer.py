"""Сервис записи логов AIRequestLog из WebSocket consumer."""

from asgiref.sync import sync_to_async
from django.utils import timezone


class LogWriter:
    """Создание и обновление записей AIRequestLog из WebSocket consumer.

    Методы обёрнуты в sync_to_async для безопасного вызова из async-кода.
    Ошибки определяются структурно (is_error из ModelCaller/клиентов);
    update_success вызывается только для успешных ответов.
    """

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
        task_node_id: int | None = None,
        task_name: str = "",
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
            task_node_id=task_node_id,
            task_name=task_name,
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

        # Defense: an empty response is an error, not a success. The consumer
        # already routes empties to update_error via is_error, but keep the guard.
        # Real API errors are signalled structurally (is_error from clients) and
        # also go to update_error — so a non-empty answer here is a genuine success,
        # even if it happens to discuss «ошибка» in the code being explained.
        if not (response_text or "").strip():
            log.status = AIRequestLog.STATUS_ERROR
            log.error_message = "Модель вернула пустой ответ"
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
                "error_message",
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
