# src/ai/db/AIDLApiRequest.py

from django.conf import settings
from django.db import models
from django.utils import timezone

class AIDLApiRequest(models.Model):
    """Запрос пользователя на создание API к DL (dl.gsu.by).

    Страница «Запросы по DL API»: две вкладки — «актуальные» (STATUS_PENDING,
    API ещё не создан) и «неактуальные» (API сделано — STATUS_DONE).
    Актуальный запрос провисает, когда API создан: админ жмёт «API создано»,
    и строка переезжает в «неактуальные».
    """

    STATUS_PENDING = "pending"
    STATUS_DONE = "done"

    STATUS_CHOICES = (
        (STATUS_PENDING, "Актуальный"),
        (STATUS_DONE, "Неактуальный"),
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="ai_dl_api_requests",
        verbose_name="Кто попросил",
    )
    external_user_id = models.CharField(max_length=255, blank=True, db_index=True, verbose_name="ID на dl.gsu.by")
    user_full_name = models.CharField(max_length=500, blank=True, default="", verbose_name="ФИО")
    username = models.CharField(max_length=255, blank=True, default="", verbose_name="Логин")
    description = models.TextField(blank=True, default="", verbose_name="Описание")
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True,
        verbose_name="Статус",
    )
    created_at = models.DateTimeField(default=timezone.now, db_index=True, verbose_name="Когда попросил")
    resolved_at = models.DateTimeField(null=True, blank=True, verbose_name="Когда сделано")
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="ai_dl_api_requests_resolved",
        verbose_name="Кто сделал",
    )
    comment = models.TextField(blank=True, default="", verbose_name="Комментарий")

    class Meta:
        db_table = "ai_ai_dl_api_request"
        verbose_name = "Запрос API к DL"
        verbose_name_plural = "Запросы API к DL"
        ordering = ("-created_at",)
        indexes = [
            # Вкладки «актуальные/неактуальные» + сортировка по дате.
            models.Index(fields=["status", "created_at"], name="ai_dl_api_req_status_idx"),
        ]

    def __str__(self) -> str:
        who = self.user_full_name or self.username or self.external_user_id or "—"
        return f"{who}: {self.description[:60]}"
