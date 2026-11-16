# src/ai/db/AIAppSettings.py

from django.db import models
from django.utils import timezone

class AIAppSettings(models.Model):
    """Глобальная настройка приложения AI (singleton-модель).

    Хранит флаг включения/выключения AI-функциональности.
    Модель имеет фиксированный pk=1 — только одна строка в таблице.
    """
    is_enabled = models.BooleanField(default=True, verbose_name="Включено")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Обновлён")
    # Дата-отсечка для счётчика «фаворитов» (топ-2 моделей по частоте использования).
    # Успешные AIRequestLog старше этой даты не учитываются при ранжировании,
    # что даёт разовый сброс фаворитов для всех пользователей (нет записей новее
    # epoch → строгий алфавит) без удаления логов; новые запросы снова набирают
    # топ-2. См. ai/views.py::_get_user_top_model_keys и management-команду
    # reset_favorites_epoch.
    favorites_epoch = models.DateTimeField(
        null=True, blank=True, default=timezone.now,
        verbose_name="Дата отсечки фаворитов",
    )
    class Meta:
        verbose_name = "Настройки ИИ-приложения"
        verbose_name_plural = "Настройки ИИ-приложения"

    # Кэш «одиночных» настроек: get_solo() зовётся на каждый рендер страницы,
    # на подключение WebSocket и при ранжировании фаворитов. 30 с — достаточно,
    # чтобы снять повторные SELECT'ы, и мало, чтобы рассинхрон с админкой был
    # заметен; save()/delete() сбрасывают ключ немедленно.
    CACHE_KEY = "ai:app_settings:solo"
    CACHE_TTL = 30
    # Ссылка на change-форму синглтона в навигации админки (ai/admin/site.py).
    NAV_URL_CACHE_KEY = "ai:app_settings:nav_url"

    def save(self, *args, **kwargs):
        # Keep a single row for global app state.
        self.pk = 1
        super().save(*args, **kwargs)
        self._invalidate_cache()

    def delete(self, *args, **kwargs):
        result = super().delete(*args, **kwargs)
        self._invalidate_cache()
        return result

    @classmethod
    def _invalidate_cache(cls):
        from django.core.cache import cache
        cache.delete_many([cls.CACHE_KEY, cls.NAV_URL_CACHE_KEY])

    @classmethod
    def get_solo(cls):
        import copy

        from ai.services.cache_epochs import cached_call

        obj = cached_call(cls.CACHE_KEY, lambda: cls.objects.get_or_create(pk=1)[0], cls.CACHE_TTL)
        # Копия: вызывающий код мутирует атрибуты перед save(), и закэшированный
        # объект не должен меняться.
        return copy.copy(obj)

    def __str__(self):
        return "AI app settings"
