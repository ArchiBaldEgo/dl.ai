"""Changelist без второго COUNT'а там, где он заведомо совпадает с первым.

Django 5.1 в ``ChangeList.get_results`` всегда делает два запроса на
количество: ``paginator.count`` (с учётом фильтров) и, если включён
``show_full_result_count`` (умолчание, нигде у нас не переопределено),
``root_queryset.count()`` — ради строки «N из M». Когда в ``request.GET``
нет ни одного параметра, фильтры/поиск не применялись, и оба числа равны:
второй COUNT(*) — чистая работа впустую на самой дорогой странице админки.

Здесь второй COUNT не выполняется, а ``full_result_count`` заполняется
первым. Отображаемые числа и поведение actions при пустом списке — те же,
что были: при пустом GET ``M == N``.

Если в GET есть хоть один параметр (фильтр, поиск, сортировка, страница) или
``list_filter`` умеет фильтровать «по умолчанию» — работает штатная ветка
Django. Для надёжности сравниваем SQL текущего queryset'а с root_queryset:
если отличаются (кроме сортировки и select_related — на COUNT они не влияют),
оптимизация не применяется.
"""

import copy

from django.contrib.admin.views.main import ChangeList


class FastFullCountChangeList(ChangeList):
    def _queryset_matches_root(self):
        """Совпадает ли выборка с root по ВСЕМУ, кроме сортировки.

        Changelist сортирует ``self.queryset`` (``order_by`` из запроса), а
        ``root_queryset`` остаётся без этой сортировки — на COUNT она не
        влияет, поэтому из сравниваемых копий она вырезается. Любое реальное
        отличие (фильтр, поиск, distinct) остаётся и отключает оптимизацию.
        """
        try:
            q1 = self.queryset.query.clone()
            q2 = self.root_queryset.query.clone()
            for query in (q1, q2):
                # Сортировка на COUNT не влияет; select_related — JOIN'ы
                # many-to-one, они не размножают строки и COUNT не меняют.
                # Остаётся сравнение WHERE/аннотаций/distinct — того, что
                # реально может сделать числа разными.
                query.order_by = ()
                query.select_related = False
            return str(q1) == str(q2)
        except Exception:  # pragma: no cover - нестандартный queryset
            return False

    def get_results(self, request):
        if (
            not request.GET
            and self.model_admin.show_full_result_count
            and not self.queryset.query.is_sliced
            and self._queryset_matches_root()
        ):
            # ModelAdmin — общий инстанс на процесс; не мутируем его флаг
            # (это гонка между запросами). Берём поверхностную копию с
            # выключенным счётчиком и сразу возвращаем оригинал на место.
            original_admin = self.model_admin
            fast_admin = copy.copy(original_admin)
            fast_admin.show_full_result_count = False
            self.model_admin = fast_admin
            try:
                super().get_results(request)
            finally:
                self.model_admin = original_admin
            # Оба числа совпадают по построению — подставляем первое.
            self.full_result_count = self.result_count
            self.show_full_result_count = True
            self.show_admin_actions = bool(self.result_count)
            return
        super().get_results(request)


class FastChangelistAdminMixin:
    """Подключает ``FastFullCountChangeList`` к ModelAdmin."""

    def get_changelist(self, request, **kwargs):
        return FastFullCountChangeList
