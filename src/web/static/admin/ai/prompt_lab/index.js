/* Лаборатория промптов: выбор кейсов, запуск, поллинг, метрики.
 * Без зависимостей; весь пользовательский/модельный текст экранируется
 * перед вставкой в innerHTML. */
(function () {
    'use strict';

    var root = document.getElementById('promptLab');
    if (!root) return;

    var CASES = [];
    try {
        var node = document.getElementById('prompt-lab-cases');
        CASES = node ? JSON.parse(node.textContent) : [];
    } catch (err) {
        CASES = [];
    }

    var selected = new Set();
    var currentRunId = '';
    var pollTimer = null;
    var currentSnapshot = null;

    var STATUS_LABELS = {
        running: 'выполняется',
        completed: 'завершён',
        failed: 'ошибка',
        cancelled: 'прерван'
    };

    function $(id) { return document.getElementById(id); }

    function escapeHtml(value) {
        return String(value === null || value === undefined ? '' : value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function csrfToken() {
        var match = document.cookie.match(/(?:^|;\s*)(?:ai_)?csrftoken=([^;]+)/);
        return match ? decodeURIComponent(match[1]) : '';
    }

    function parseJsonResponse(response) {
        if (!response.ok) {
            if (response.status === 403) {
                return Promise.reject(new Error('Нет прав или истекла сессия. Обновите страницу и войдите заново.'));
            }
            return response.text().then(function (text) {
                throw new Error('Сервер вернул ' + response.status + (text ? ': ' + text.slice(0, 300) : ''));
            });
        }
        var type = response.headers.get('content-type') || '';
        if (type.indexOf('application/json') === -1) {
            return Promise.reject(new Error('Сессия истекла или сервер вернул не JSON. Обновите страницу.'));
        }
        return response.json();
    }

    function postJson(url, payload) {
        return fetch(url, {
            method: 'POST',
            credentials: 'same-origin',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': csrfToken()
            },
            body: JSON.stringify(payload || {})
        }).then(parseJsonResponse);
    }

    function getJson(url) {
        return fetch(url, { credentials: 'same-origin' }).then(parseJsonResponse);
    }

    function codesText(codes) {
        if (!codes || !codes.length) return '—';
        return codes.map(function (code) { return 'error_' + code; }).join(', ');
    }

    function fmtNum(value, digits) {
        if (value === null || value === undefined || isNaN(value)) return '—';
        return Number(value).toFixed(digits === undefined ? 3 : digits);
    }

    function fmtPercent(value) {
        if (value === null || value === undefined || isNaN(value)) return '—';
        return (Number(value) * 100).toFixed(1) + '%';
    }

    function statusBadge(status) {
        return '<span class="lab-badge lab-status-' + escapeHtml(status) + '">' +
            escapeHtml(STATUS_LABELS[status] || status) + '</span>';
    }

    // --- выбор кейсов -----------------------------------------------------

    function visibleCases() {
        var groupId = $('labFilterGroup').value;
        var status = $('labFilterStatus').value;
        var query = ($('labFilterSearch').value || '').trim().toLowerCase();
        var onlyStatement = $('labFilterStatement').checked;
        return CASES.filter(function (item) {
            if (groupId && String(item.group_id) !== String(groupId)) return false;
            if (status && item.status !== status) return false;
            if (onlyStatement && !item.has_statement) return false;
            if (query) {
                var haystack = (item.name + ' ' + item.group + ' ' + item.ref + ' ' +
                    item.codes.map(function (c) { return 'error_' + c; }).join(' ')).toLowerCase();
                if (haystack.indexOf(query) === -1) return false;
            }
            return true;
        });
    }

    function renderCases() {
        var items = visibleCases();
        var box = $('labCases');
        if (!items.length) {
            box.innerHTML = '<div class="lab-muted">Кейсы не найдены. Заполните базу или измените фильтры.</div>';
            updateCounter();
            return;
        }
        var rows = items.map(function (item) {
            var checked = selected.has(item.id) ? ' checked' : '';
            return '<label class="lab-case-row">' +
                '<input type="checkbox" class="lab-case-check" value="' + item.id + '"' + checked + '>' +
                '<span class="lab-case-name">' + escapeHtml(item.group) + ' / ' + escapeHtml(item.name) + '</span>' +
                '<span class="lab-case-codes">' + escapeHtml(codesText(item.codes)) + '</span>' +
                '<span class="lab-case-flags">' +
                    (item.has_statement ? '' : '<em title="Условие задачи пустое">без условия</em> ') +
                    '<span class="lab-badge lab-status-' + escapeHtml(item.status) + '">' + escapeHtml(item.status) + '</span>' +
                '</span>' +
            '</label>';
        });
        box.innerHTML = rows.join('');
        updateCounter();
    }

    function updateCounter() {
        var count = selected.size;
        $('labCaseCounter').textContent = 'выбрано: ' + count;
        var warning = $('labCostWarning');
        warning.hidden = count <= 20;
        if (count > 20) {
            var modelCount = document.querySelectorAll('.lab-model:checked').length || 1;
            warning.textContent = 'Внимание: ' + count + ' кейсов × ' + modelCount +
                ' модель(ей) = ' + (count * modelCount) + ' вызовов модели. Это долго и тратит токены.';
        }
        $('labStartBtn').disabled = count === 0;
    }

    function setSelection(ids, checked) {
        ids.forEach(function (id) {
            if (checked) selected.add(id); else selected.delete(id);
        });
        renderCases();
    }

    // --- запуск и статус --------------------------------------------------

    function startRun() {
        var models = Array.prototype.map.call(
            document.querySelectorAll('.lab-model:checked'),
            function (input) { return input.value; }
        );
        if (!models.length) {
            $('labStartMessage').textContent = 'Выберите хотя бы одну модель.';
            return;
        }
        if (!selected.size) {
            $('labStartMessage').textContent = 'Выберите хотя бы один кейс.';
            return;
        }
        $('labStartBtn').disabled = true;
        $('labStartMessage').textContent = 'Запускаю…';
        postJson(root.dataset.startUrl, {
            prompt_id: $('labPrompt').value || null,
            model_keys: models,
            case_ids: Array.from(selected),
            ui_language: $('labUiLanguage').value,
            include_eval_suffix: $('labEvalSuffix').checked
        }).then(function (data) {
            if (!data.ok) throw new Error(data.error || 'Не удалось запустить прогон.');
            $('labStartMessage').textContent = 'Прогон запущен.';
            openRun(data.run_id);
            refreshRecentRuns();
        }).catch(function (err) {
            $('labStartMessage').textContent = err.message;
        }).finally(function () {
            updateCounter();
        });
    }

    function openRun(runId) {
        currentRunId = runId;
        currentSnapshot = null;
        $('labRunPanel').hidden = false;
        $('labRunId').textContent = runId.slice(0, 8);
        $('labExportLink').href = root.dataset.exportUrl + '?run_id=' + encodeURIComponent(runId);
        $('labRunMessage').textContent = '';
        $('labDetail').hidden = true;
        $('labSummary').querySelector('tbody').innerHTML = '';
        $('labResults').querySelector('tbody').innerHTML = '';
        $('labProgress').hidden = false;
        $('labProgressFill').style.width = '0%';
        $('labProgressText').textContent = 'Загрузка…';
        pollRun();
    }

    function stopPolling() {
        if (pollTimer) {
            clearTimeout(pollTimer);
            pollTimer = null;
        }
    }

    function pollRun() {
        stopPolling();
        if (!currentRunId) return;
        getJson(root.dataset.statusUrl + '?run_id=' + encodeURIComponent(currentRunId))
            .then(function (data) {
                if (!data.ok) throw new Error(data.error || 'Прогон не найден.');
                currentSnapshot = data.snapshot;
                renderSnapshot(currentSnapshot);
                if (currentSnapshot.status === 'running') {
                    pollTimer = setTimeout(pollRun, 2000);
                }
            })
            .catch(function (err) {
                $('labRunMessage').textContent = err.message;
            });
    }

    function renderSnapshot(snapshot) {
        var metaParts = [snapshot.prompt_name || 'По умолчанию'];
        if (snapshot.model_titles && snapshot.model_titles.length) {
            metaParts.push(snapshot.model_titles.join(', '));
        }
        metaParts.push(snapshot.case_count + ' кейсов');
        metaParts.push(statusText(snapshot.status));
        if (snapshot.started_at) metaParts.push('начат ' + snapshot.started_at.replace('T', ' ').slice(0, 16));
        $('labRunMeta').textContent = metaParts.join(' · ');

        var progress = snapshot.progress || {};
        var total = progress.total || 0;
        var done = progress.done || 0;
        if (snapshot.status === 'running') {
            $('labProgress').hidden = false;
            var pct = total ? Math.round(done * 100 / total) : 0;
            $('labProgressFill').style.width = pct + '%';
            $('labProgressText').textContent = done + ' / ' + total + ' (' + pct + '%)' +
                (progress.errors ? ', ошибок: ' + progress.errors : '');
        } else {
            $('labProgress').hidden = (done === 0 && total === 0);
            if (!$('labProgress').hidden) {
                $('labProgressFill').style.width = '100%';
                $('labProgressText').textContent = done + ' / ' + total;
            }
        }
        $('labRunMessage').textContent = snapshot.error_message || '';
        $('labCancelBtn').hidden = snapshot.status !== 'running';

        renderSummary(snapshot.report || {});
        renderResults(snapshot);
    }

    function statusText(status) {
        return STATUS_LABELS[status] || status;
    }

    function renderSummary(report) {
        var tbody = $('labSummary').querySelector('tbody');
        var models = report.models || [];
        if (!models.length) {
            tbody.innerHTML = '<tr><td colspan="10" class="lab-muted">Пока нет завершённых пар.</td></tr>';
            return;
        }
        tbody.innerHTML = models.map(function (item) {
            return '<tr>' +
                '<td>' + escapeHtml(item.model_title || item.model_key) + '</td>' +
                '<td>' + item.cases + '</td>' +
                '<td>' + item.errors + '</td>' +
                '<td>' + item.exact + '</td>' +
                '<td>' + fmtPercent(item.exact_rate) + '</td>' +
                '<td>' + fmtNum(item.precision) + '</td>' +
                '<td>' + fmtNum(item.recall) + '</td>' +
                '<td>' + fmtNum(item.f1) + '</td>' +
                '<td>' + fmtNum(item.avg_duration, 2) + ' с</td>' +
                '<td>' + item.tokens + '</td>' +
            '</tr>';
        }).join('');
    }

    function renderResults(snapshot) {
        var onlyProblems = $('labOnlyProblems').checked;
        var results = snapshot.results || [];
        var tbody = $('labResults').querySelector('tbody');
        var rows = results.filter(function (item) {
            if (!onlyProblems) return true;
            return item.status !== 'ok' || !item.exact_match;
        });
        if (!rows.length) {
            tbody.innerHTML = '<tr><td colspan="12" class="lab-muted">Нет строк по текущему фильтру.</td></tr>';
            return;
        }
        tbody.innerHTML = rows.map(function (item) {
            if (item.status !== 'ok') {
                return '<tr class="lab-row-error">' +
                    '<td>' + escapeHtml(item.group_name) + ' / ' + escapeHtml(item.case_name) + '</td>' +
                    '<td>' + escapeHtml(item.model_title || item.model_key) + '</td>' +
                    '<td colspan="8">' + escapeHtml(item.error_message || 'Ошибка вызова') + '</td>' +
                    '<td><button type="button" class="lab-detail-btn" data-result-id="' + item.id + '">Детали</button></td>' +
                '</tr>';
            }
            var exact = item.exact_match
                ? '<span class="lab-badge lab-exact">exact</span>'
                : '<span class="lab-badge lab-partial">partial</span>';
            return '<tr>' +
                '<td>' + escapeHtml(item.group_name) + ' / ' + escapeHtml(item.case_name) + '</td>' +
                '<td>' + escapeHtml(item.model_title || item.model_key) + '</td>' +
                '<td>' + escapeHtml(codesText(item.expected_codes)) + '</td>' +
                '<td>' + escapeHtml(codesText(item.parsed_codes)) + '</td>' +
                '<td>' + escapeHtml(codesText(item.matched_codes)) + '</td>' +
                '<td>' + escapeHtml(codesText(item.missed_codes)) + '</td>' +
                '<td>' + escapeHtml(codesText(item.extra_codes)) + '</td>' +
                '<td>' + exact + '</td>' +
                '<td>' + fmtNum(item.precision) + '</td>' +
                '<td>' + fmtNum(item.recall) + '</td>' +
                '<td>' + fmtNum(item.f1) + '</td>' +
                '<td><button type="button" class="lab-detail-btn" data-result-id="' + item.id + '">Детали</button></td>' +
            '</tr>';
        }).join('');
    }

    function showDetail(resultId) {
        getJson(root.dataset.bodyUrl + resultId + '/body/')
            .then(function (data) {
                if (!data.ok) throw new Error(data.error || 'Результат не найден.');
                var r = data.result;
                $('labDetail').hidden = false;
                $('labDetailTitle').textContent = r.group_name + ' / ' + r.case_name + ' — ' + r.model_title;
                var metrics = 'Ожидалось: ' + codesText(r.expected_codes) +
                    ' · Найдено: ' + codesText(r.parsed_codes) +
                    ' · Пропущено: ' + codesText(r.missed_codes) +
                    ' · Лишние: ' + codesText(r.extra_codes) +
                    ' · P=' + fmtNum(r.precision) + ' R=' + fmtNum(r.recall) + ' F1=' + fmtNum(r.f1);
                $('labDetailBody').innerHTML =
                    '<div class="lab-detail-metrics">' + escapeHtml(metrics) + '</div>' +
                    '<h4>Отправленное сообщение</h4><pre>' + escapeHtml(r.message) + '</pre>' +
                    '<h4>Ответ модели</h4><pre>' + escapeHtml(r.raw_response || r.error_message || '') + '</pre>';
                $('labDetail').scrollIntoView({ behavior: 'smooth', block: 'start' });
            })
            .catch(function (err) {
                $('labRunMessage').textContent = err.message;
            });
    }

    function cancelRun() {
        if (!currentRunId) return;
        if (!window.confirm('Прервать прогон? Уже полученные результаты сохранятся.')) return;
        postJson(root.dataset.cancelUrl, { run_id: currentRunId })
            .then(function (data) {
                if (!data.ok) throw new Error(data.error || 'Не удалось прервать.');
                pollRun();
            })
            .catch(function (err) {
                $('labRunMessage').textContent = err.message;
            });
    }

    function refreshRecentRuns() {
        // Лёгкое обновление списка «Процессы»: перезагружаем страницу нельзя —
        // пользователь мог выбрать кейсы. Достаточно добавить строку текущего
        // прогона; полный список обновится при следующем открытии страницы.
        var tbody = $('labRecentRuns').querySelector('tbody');
        if (!currentRunId) return;
        var existing = tbody.querySelector('[data-run-id="' + currentRunId + '"]');
        if (existing) return;
        var empty = tbody.querySelector('.lab-muted');
        if (empty) empty.parentElement.remove();
        var row = document.createElement('tr');
        row.setAttribute('data-run-id', currentRunId);
        row.innerHTML = '<td>только что</td><td>—</td><td>—</td><td>—</td>' +
            '<td><span class="lab-badge lab-status-running">выполняется</span></td>' +
            '<td><button type="button" class="lab-show-run" data-run-id="' +
            escapeHtml(currentRunId) + '">Показать</button></td>';
        tbody.insertBefore(row, tbody.firstChild);
    }

    // --- события ----------------------------------------------------------

    $('labFilterGroup').addEventListener('change', renderCases);
    $('labFilterStatus').addEventListener('change', renderCases);
    $('labFilterSearch').addEventListener('input', renderCases);
    $('labFilterStatement').addEventListener('change', renderCases);
    $('labCases').addEventListener('change', function (event) {
        var input = event.target;
        if (!input.classList.contains('lab-case-check')) return;
        if (input.checked) selected.add(Number(input.value));
        else selected.delete(Number(input.value));
        updateCounter();
    });
    $('labSelectVisible').addEventListener('click', function () {
        setSelection(visibleCases().map(function (item) { return item.id; }), true);
    });
    $('labClearSelection').addEventListener('click', function () {
        selected.clear();
        renderCases();
    });
    document.querySelectorAll('.lab-model').forEach(function (input) {
        input.addEventListener('change', updateCounter);
    });
    $('labStartBtn').addEventListener('click', startRun);
    $('labCancelBtn').addEventListener('click', cancelRun);
    $('labOnlyProblems').addEventListener('change', function () {
        if (currentSnapshot) renderResults(currentSnapshot);
    });
    $('labDetailClose').addEventListener('click', function () {
        $('labDetail').hidden = true;
    });
    $('labResults').addEventListener('click', function (event) {
        var button = event.target.closest('.lab-detail-btn');
        if (button) showDetail(button.dataset.resultId);
    });
    $('labRecentRuns').addEventListener('click', function (event) {
        var button = event.target.closest('.lab-show-run');
        if (button) openRun(button.dataset.runId);
    });

    renderCases();
})();
