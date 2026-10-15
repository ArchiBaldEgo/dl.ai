
(function () {
    // Клик по строке пакетного решения → развёртка результатов прогона
    // (та же таблица, что на /arm/solve/ после прогона). Snapshot в HTML
    // страницы НЕ встраивается (ленивая загрузка): по первому клику он
    // подтягивается AJAX-ом (GET data-snapshot-url → batch-snapshot view)
    // и рендерится через window.ArmBatchResults.
    document.querySelectorAll('.ai-batch-row-clickable').forEach(function (row) {
        row.addEventListener('click', function (event) {
            if (event.target.closest('a')) { return; } // клик по «Детали →» — обычный переход
            if (event.target.closest('button')) { return; } // клик по ★ — закрепление, не развёртка
            var detailRow = row.nextElementSibling;
            if (!detailRow || !detailRow.classList.contains('ai-batch-detail')) { return; }
            var willOpen = detailRow.hidden;
            var body = detailRow.querySelector('.ai-batch-detail-body');
            if (willOpen && body && !body.dataset.rendered && !body.dataset.loading) {
                var url = row.getAttribute('data-snapshot-url');
                if (!url) { return; }
                body.dataset.loading = '1';
                body.innerHTML = '<p class="ai-recent-logs-empty">Загрузка результатов…</p>';
                fetch(url, { credentials: 'same-origin', headers: { 'Accept': 'application/json' } })
                    .then(function (response) {
                        if (!response.ok) { throw new Error('HTTP ' + response.status); }
                        return response.json();
                    })
                    .then(function (snapshot) {
                        body.innerHTML = '';
                        window.ArmBatchResults.init(
                            body, snapshot, snapshot.run_id || row.getAttribute('data-run-id') || ''
                        );
                        body.dataset.rendered = '1';
                        delete body.dataset.loading;
                    })
                    .catch(function () {
                        body.innerHTML = '<p class="ai-recent-logs-empty">Не удалось загрузить результаты прогона — попробуйте ещё раз.</p>';
                        delete body.dataset.loading; // повторная попытка при следующем клике
                    });
            }
            detailRow.hidden = !willOpen;
            row.classList.toggle('ai-batch-row-open', willOpen);
        });
    });

    // «Последние запросы»: клик по строке → ленивая развёртка деталей
    // (JSON GET detail-json → метаданные + текст запроса + ответ модели).
    // Полные тексты в HTML страницы не вшиты — записи журнала бывают
    // большими; подгрузка по первому клику, как у пакетных решений.
    function escapeDetailHtml(value) {
        return String(value == null ? '' : value)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
    document.querySelectorAll('.ai-recent-log-row').forEach(function (row) {
        row.addEventListener('click', function (event) {
            if (event.target.closest('a')) { return; } // клик по ссылке ID — обычный переход
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

    // ★ Закрепить/открепить batch-прогон прямо из блока «Последние пакетные
    // решения» (POST toggle, ai/admin/pinned.py). CSRF — из куки csrftoken
    // (админские страницы всегда её выставляют через {% csrf_token %} форм).
    function pinCsrfToken() {
        var input = document.querySelector('input[name="csrfmiddlewaretoken"]');
        if (input && input.value) return input.value;
        var m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
        return m ? decodeURIComponent(m[1]) : '';
    }
    document.querySelectorAll('.ai-log-pin-btn').forEach(function (btn) {
        btn.addEventListener('click', function () {
            if (btn.disabled || btn.classList.contains('busy')) return;
            btn.classList.add('busy');
            fetch(btn.getAttribute('data-pin-url'), {
                method: 'POST',
                headers: { 'X-CSRFToken': pinCsrfToken(), 'X-Requested-With': 'XMLHttpRequest' },
                credentials: 'same-origin',
            })
                .then(function (resp) {
                    return resp.json().catch(function () { throw new Error('HTTP ' + resp.status); });
                })
                .then(function (data) {
                    if (!data.ok) { throw new Error(data.message || 'Не удалось изменить закрепление'); }
                    btn.classList.toggle('ai-log-pin-btn-active', data.pinned);
                    btn.textContent = data.pinned ? '★' : '☆';
                    btn.title = data.pinned
                        ? 'Убрать из «Закреплённых»'
                        : 'Закрепить в «Закреплённых пакетных решениях»';
                })
                .catch(function (err) {
                    alert('Закрепление: ' + err.message);
                })
                .finally(function () { btn.classList.remove('busy'); });
        });
    });
})();