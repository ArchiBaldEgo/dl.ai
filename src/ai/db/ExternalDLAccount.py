# src/ai/db/ExternalDLAccount.py

from django.conf import settings
from django.db import models
from django.db.models.functions import Upper
from django.contrib.postgres.indexes import GinIndex, OpClass


class ExternalDLAccount(models.Model):
    """Link between Django User and external DL (dl.gsu.by) account."""
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='external_dl_account',
        verbose_name="Пользователь",
    )
    external_user_id = models.CharField(
        max_length=255,
        unique=True,
        db_index=True,
        help_text="ID пользователя из API dl.gsu.by",
        verbose_name="ID пользователя dl.gsu.by",
    )
    external_login = models.CharField(
        max_length=255,
        help_text="Последний известный логин dl.gsu.by",
        verbose_name="Логин dl.gsu.by",
    )
    external_first_name = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Имя из dl.gsu.by",
        verbose_name="Имя",
    )
    external_last_name = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Фамилия из dl.gsu.by",
        verbose_name="Фамилия",
    )
    # Учебные данные студента из post/get-user-info (dl-rest-api.json): top-level
    # email + вложенный блок education. Обновляются при каждом входе
    # пользователя; массовый бэкфилл невозможен — endpoint требует sessionId
    # самого пользователя.
    dl_email = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Email из dl.gsu.by (get-user-info)",
        verbose_name="Email dl.gsu.by",
    )
    education_form = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Форма обучения (education.form)",
        verbose_name="Форма обучения",
    )
    education_school_id = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="ID школы (education.schoolId)",
        verbose_name="Школа: ID",
    )
    education_school_kind = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Тип школы (education.schoolKind)",
        verbose_name="Школа: тип",
    )
    education_school_no = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Номер школы (education.schoolNo)",
        verbose_name="Школа: номер",
    )
    education_group_mask_id = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Группа (education.groupMaskId)",
        verbose_name="Группа (groupMaskId)",
    )
    education_form_letter = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Класс/буква (education.formLetter)",
        verbose_name="Класс (буква)",
    )
    # Названия учебного заведения и группы присылает сам DL
    # (dl-rest-api.json, EducationInfo.schoolName/formName/groupName) —
    # с версии 1.4.2 REST API отдаёт их вместе с ID. Раньше названий в API
    # не было, поэтому их вели вручную в справочнике DLEducationName.
    education_school_name = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Название учебного заведения (education.schoolName)",
        verbose_name="Школа/вуз: название",
    )
    education_form_name = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Название класса/курса (education.formName)",
        verbose_name="Класс/курс: название",
    )
    education_group_name = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Название группы (education.groupName)",
        verbose_name="Группа: название",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Создан")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Обновлён")

    class Meta:
        verbose_name = "Внешняя учётная запись DL"
        verbose_name_plural = "Внешние учётные записи DL"
        indexes = [
            # date_hierarchy / фильтры «по дате» в админке.
            models.Index(fields=["created_at"], name="ai_ext_dl_created_idx"),
            models.Index(fields=["updated_at"], name="ai_ext_dl_updated_idx"),
            # Поиск по id/логину/ФИО (icontains) — expression-индексы на UPPER:
            # предикат UPPER(col::text) LIKE UPPER(%p%) обычный индекс не
            # обслуживает. В БД создаёт миграция 0079 (только Postgres).
            GinIndex(
                OpClass(Upper("external_user_id"), "gin_trgm_ops"),
                name="ai_ext_dl_extid_trgm_idx",
            ),
            GinIndex(
                OpClass(Upper("external_login"), "gin_trgm_ops"),
                name="ai_ext_dl_login_trgm_idx",
            ),
            GinIndex(
                OpClass(Upper("external_first_name"), "gin_trgm_ops"),
                name="ai_ext_dl_fname_trgm_idx",
            ),
            GinIndex(
                OpClass(Upper("external_last_name"), "gin_trgm_ops"),
                name="ai_ext_dl_lname_trgm_idx",
            ),
        ]

    def __str__(self):
        return f"{self.user.username} (DL: {self.external_login})"

