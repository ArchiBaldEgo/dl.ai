# src/ai/db/DLEducationName.py

from django.db import models

class DLEducationName(models.Model):
    """Справочник DL «id → человекочитаемое название» (вуз/школа, группа/класс).

    REST get-user-info отдаёт только коды: schoolId (строка), schoolKind,
    schoolNo и groupMaskId — настоящих названий вузов/школ и групп там нет.
    Ведётся вручную суперюзером (по образцу AuthorAlias); отображение
    (дневной отчёт журнала) резолвится при рендере — правка записи
    ретроактивно чинит прошлые строки. Читатель — единственный:
    ai/services/education_directory.py.
    """
    KIND_SCHOOL = "school"
    KIND_GROUP = "group"
    KIND_CHOICES = (
        (KIND_SCHOOL, "Школа/вуз (schoolId)"),
        (KIND_GROUP, "Группа/класс (groupMaskId)"),
    )
    kind = models.CharField(
        max_length=10, choices=KIND_CHOICES, verbose_name="Тип записи",
    )
    key = models.CharField(max_length=100, verbose_name="ID в DL")
    title = models.CharField(max_length=255, verbose_name="Название")
    # Вуз/школа, к которой относится группа/класс: key соответствующей
    # school-строки этого же справочника (groupMaskId — строка без структуры,
    # DL не сообщает заведение группы). Заполняется ТОЛЬКО у kind="group";
    # читатель — education_directory (groups_school), фолбэк в дневном отчёте
    # показывает школу студента от привязки его группы.
    school = models.CharField(
        max_length=100, blank=True, default="",
        verbose_name="Вуз/школа (key из schoolId)",
    )

    class Meta:
        db_table = "ai_dl_education_name"
        verbose_name = "Учебное название DL (id → название)"
        verbose_name_plural = "Учебные названия DL (вуз/школа, группа)"
        ordering = ("kind", "title")
        constraints = [
            models.UniqueConstraint(
                fields=("kind", "key"),
                name="ai_dl_education_name_kind_key_uniq",
            ),
        ]

    def __str__(self):
        return f"{self.title} [{self.key}]"

