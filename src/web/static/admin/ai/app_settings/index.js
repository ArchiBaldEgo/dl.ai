
(function () {
    // Развёртка строк ПАКЕТНЫХ РЕШЕНИЙ (клик по строке → результаты
    // прогона) и кнопка ★ живут в ОБЩЕМ модуле
    // static/admin/ai/batch_results/rows.js (подключается глобально в
    // admin/base_site.html) — там же делегирование, поэтому работает и
    // после AJAX-обновления таблицы. Здесь остаётся только развёртка
    // ОБЫЧНЫХ запросов (admin/ai/_ai_log_row.html).

    // «Последние запросы»: клик по строке → ленивая развёртка деталей
    // (JSON GET detail-json → метаданные + текст запроса + ответ модели).
    // Полные тексты в HTML страницы не вшиты — записи журнала бывают
    // большими; подгрузка по первому клику, как у пакетных решений.
    // Строка — общий партиал admin/ai/_ai_log_row.html: обычный запрос имеет
    // класс .ai-log-row, пакетное решение — .ai-batch-row (развёртка
    // результатов прогона обрабатывается выше).
    function escapeDetailHtml(value) {
        return String(value == null ? '' : value)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
    document.querySelectorAll('.ai-log-row').forEach(function (row) {
        row.addEventListener('click', function (event) {
            if (event.target.closest('a')) { return; } // клик по ссылке ID — обычный переход
            if (event.target.closest('button')) { return; } // клик по ★ — закрепление
            var detailRow = row.nextElementSibling;
            if (!detailRow || !detailRow.classList.contains('ai-recent-log-detail')) { return; }
            var willOpen = detailRow.hidden;
            var body = detailRow.querySelector('.ai-recent-log-detail-body');
            if (willOpen && body && !body.dataset.rendered && !body.dataset.loading) {
                var url = row.getAttribute('data-detail-url');
                if (!url) { return; }
                body.dataset.loading = '1';
                body.innerHTML = '<p class="ai-recent-logs-empty">Загрузка подробностей…</p>';
                fetch(url, { credentials: 'same-origin', headers: { 'Accept': 'application/json' } })
                    .then(function (response) {
                        if (!response.ok) { throw new Error('HTTP ' + response.status); }
                        return response.json();
                    })
                    .then(function (data) {
                        if (!data || !data.ok || !data.log) { throw new Error('Нет данных'); }
                        var l = data.log;
                        var sender = l.external_user_id
                            ? '<a href="https://dl.gsu.by/report.asp?id=' + encodeURIComponent(l.external_user_id) + '" target="_blank" rel="noopener">' + escapeDetailHtml(l.sender) + '</a>'
                            : escapeDetailHtml(l.sender);
                        var prompt = l.prompt_id
                            ? '<a href="/ai/admin/ai/prompt/' + encodeURIComponent(l.prompt_id) + '/change/" target="_blank" rel="noopener">' + escapeDetailHtml(l.prompt_name) + '</a>'
                            : escapeDetailHtml(l.prompt_name);
                        var task = l.task_node_id
                            ? escapeDetailHtml(l.task_name || l.task_node_id) + ' (' + escapeDetailHtml(l.task_node_id) + ')'
                            : escapeDetailHtml(l.task_name || '—');
                        var html = [
                            '<h4>Параметры запроса</h4>',
                            '<div>',
                            '<div><strong>ID:</strong> ' + escapeDetailHtml(l.id) + '</div>',
                            '<div><strong>Источник:</strong> ' + escapeDetailHtml(l.source_display) + '</div>',
                            '<div><strong>Режим:</strong> ' + escapeDetailHtml(l.mode_display) + '</div>',
                            '<div><strong>Модель:</strong> ' + escapeDetailHtml(l.model_names) + '</div>',
                            '<div><strong>Статус:</strong> ' + escapeDetailHtml(l.status_display) + '</div>',
                            '<div><strong>Время ответа:</strong> ' + escapeDetailHtml(l.duration_seconds) + ' с</div>',
                            '<div><strong>Токенов:</strong> ' + escapeDetailHtml(l.tokens == null ? '—' : l.tokens) + '</div>',
                            '<div><strong>Отправлен:</strong> ' + escapeDetailHtml(l.sent_at) + '</div>',
                            '<div><strong>Получен:</strong> ' + escapeDetailHtml(l.received_at) + '</div>',
                            '<div><strong>Кто:</strong> ' + sender + '</div>',
                            '<div><strong>Язык:</strong> ' + escapeDetailHtml(l.programming_language_name) + '</div>',
                            '<div><strong>Тема:</strong> ' + escapeDetailHtml(l.topic_name) + '</div>',
                            '<div><strong>Задача:</strong> ' + task + '</div>',
                            '<div><strong>Промпт:</strong> ' + prompt + '</div>',
                            '</div>',
                            l.is_batch
                                ? '<h4>Прогон</h4><div><strong>Название:</strong> ' + escapeDetailHtml(l.run_name || '—')
                                + ' · <strong>Статус:</strong> ' + escapeDetailHtml(l.run_status_display || '—')
                                + ' · <strong>Окончание:</strong> ' + escapeDetailHtml(l.run_finished_at || '—')
                                + ' · <strong>Общее время:</strong> ' + escapeDetailHtml(l.run_duration_display || '—')
                                + ' · результаты прогона — в блоке «Последние пакетные решения» выше или в <a href="' + escapeDetailHtml(l.detail_url) + '">полной записи</a>.</div>'
                                : '<h4>Текст запроса</h4><pre>' + escapeDetailHtml(l.message || '(пусто)') + '</pre>',
                            l.is_batch ? '' : '<h4>Ответ модели</h4><pre>' + escapeDetailHtml(l.response_text || '(пусто)') + '</pre>',
                            l.error_message
                                ? '<h4>Ошибка</h4><pre>' + escapeDetailHtml(l.error_message) + '</pre>'
                                : '',
                            '<p style="margin:8px 0 0;"><a href="' + escapeDetailHtml(l.detail_url) + '">Полная запись журнала →</a></p>',
                        ].join('');
                        body.innerHTML = html;
                        body.dataset.rendered = '1';
                        delete body.dataset.loading;
                    })
                    .catch(function () {
                        body.innerHTML = '<p class="ai-recent-logs-empty">Не удалось загрузить подробности — попробуйте ещё раз.</p>';
                        delete body.dataset.loading;
                    });
            }
            detailRow.hidden = !willOpen;
        });
    });

    // Кнопка «Сохранить» — сразу под вкл/выкл AI-приложения: единственный
    // редактируемый филд формы — переключатель is_enabled (первый fieldset),
    // а штатный submit-row рендерится В САМОМ НИЗУ — после блоков последних
    // прогонов/запросов. Переносим submit-row прямо под переключатель.
    // DOMContentLoaded: скрипт грузится из after_field_sets, т.е. ДО того,
    // как Django отрендерит submit_buttons_bottom (submit-row ещё не в DOM).
    document.addEventListener('DOMContentLoaded', function () {
        var toggle = document.querySelector('input[name="is_enabled"]');
        var submitRow = document.querySelector('.submit-row');
        if (!toggle || !submitRow) return;
        var fieldset = toggle.closest('fieldset');
        // Переносим только «свой» submit-row этой формы (guard от чужих блоков).
        if (fieldset && submitRow.closest('form') === fieldset.closest('form')) {
            fieldset.insertAdjacentElement('afterend', submitRow);
        }
    });
})();