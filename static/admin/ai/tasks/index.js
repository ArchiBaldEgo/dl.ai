// Доска «Таски» (ai/admin/tasks.py): создать/взять/закрыть таск — POST fetch,
// переставить строки фронта — HTML5 drag & drop + POST /tasks/reorder/ (ids
// в новом порядке; сервер перенумеровывает 1..N). JS на делегировании, IIFE.
(function () {
    'use strict';

    // CSRF — из инпута формы ({% csrf_token %}) или куки; в проде имя куки
    // переименовано в ai_csrftoken, поэтому матчим оба варианта.
    function csrftoken() {
        var input = document.querySelector('input[name="csrfmiddlewaretoken"]');
        if (input && input.value) return input.value;
        var m = document.cookie.match(/(?:^|;\s*)(?:ai_)?csrftoken=([^;]+)/);
        return m ? decodeURIComponent(m[1]) : '';
    }

    // AJAX админки всегда ждёт JSON: при протухшей сессии admin_view
    // редиректит fetch на страницу логина (HTML) — честный текст вместо
    // ошибки парсинга (та же конвенция, что в arm_solve/test_console).
    // Не-JSON с другим статусом (CSRF 403, 500) показывается со статусом
    // и началом тела — маскировать всё под «сессию» нельзя.
    function parseJsonResponse(response) {
        return response.text().then(function (text) {
            try {
                return JSON.parse(text);
            } catch (e) {
                var head = text.replace(/\s+/g, ' ').slice(0, 120);
                if (response.status === 403) {
                    throw new Error('CSRF-проверка не прошла (HTTP 403): ' + head +
                        ' — обновите страницу (Ctrl+F5) и повторите');
                }
                throw new Error('Сервер вернул не JSON, HTTP ' + response.status +
                    ': ' + head + (head.indexOf('<!DOCTYPE') === 0 || head.indexOf('<html') === 0
                        ? ' — сессия истекла, войдите заново'
                        : ''));
            }
        });
    }

    function post(url, body) {
        return fetch(url, {
            method: 'POST',
            headers: { 'X-CSRFToken': csrftoken(), 'X-Requested-With': 'XMLHttpRequest' },
            credentials: 'same-origin',
            body: body,
        })
            .then(function (response) {
                return parseJsonResponse(response);
            })
            .then(function (data) {
                if (!data.ok) {
                    throw new Error(data.message || 'Не удалось выполнить действие');
                }
            });
    }

    function guard(button, handler) {
        if (button.classList.contains('busy')) return;
        button.classList.add('busy'); // busy → pointer-events: none, повторного клика нет
        handler().catch(function (err) {
            button.classList.remove('busy');
            alert(err.message);
        });
    }

    // Взять/закрыть — успех перезагружает страницу: статусы строк
    // пересчитывает сервер (на фронте после reorder reload не нужен).
    document.addEventListener('click', function (event) {
        var btn = event.target.closest('[data-claim-url], [data-close-url]');
        if (!btn) return;
        var url = btn.getAttribute('data-claim-url') || btn.getAttribute('data-close-url');
        if (!url) return;
        guard(btn, function () { return post(url).then(function () { window.location.reload(); }); });
    });

    var createForm = document.querySelector('.ai-tasks-create');
    if (createForm) {
        createForm.addEventListener('submit', function (event) {
            event.preventDefault();
            var btn = createForm.querySelector('[type="submit"]');
            var data = new FormData(createForm);
            if (btn) btn.classList.add('busy');
            post(createForm.getAttribute('data-create-url'), data)
                .then(function () { window.location.reload(); })
                .catch(function (err) {
                    if (btn) btn.classList.remove('busy');
                    alert(err.message);
                });
        });
    }

    // --- Перетаскивание строк фронта (только table[data-reorder="1]) ---
    var table = document.querySelector('table[data-reorder="1"]');
    if (table) {
        var body = table.tBodies[0];
        var dragRow = null;

        function clearMarkers() {
            var marked = table.querySelectorAll('.ai-task-drop-above, .ai-task-drop-below');
            for (var i = 0; i < marked.length; i++) {
                marked[i].classList.remove('ai-task-drop-above', 'ai-task-drop-below');
            }
        }

        var dropTargetRow = null;
        var dropBelow = false;

        function rowMidpoint(event, row) {
            var rect = row.getBoundingClientRect();
            return event.clientY < rect.top + rect.height / 2;
        }

        body.addEventListener('dragstart', function (event) {
            dragRow = event.target.closest('tr[data-task-id]');
            if (!dragRow) return;
            dragRow.classList.add('ai-task-dragging');
            if (event.dataTransfer) {
                event.dataTransfer.effectAllowed = 'move';
                event.dataTransfer.setData('text/plain', dragRow.getAttribute('data-task-id'));
            }
        });

        body.addEventListener('dragend', function () {
            if (dragRow) dragRow.classList.remove('ai-task-dragging');
            dragRow = null;
            dropTargetRow = null;
            clearMarkers();
        });

        body.addEventListener('dragover', function (event) {
            if (!dragRow) return;
            var row = event.target.closest('tr[data-task-id]');
            if (!row || row === dragRow) return;
            event.preventDefault(); // иначе drop запрещён браузером
            event.dataTransfer.dropEffect = 'move';
            clearMarkers();
            dropTargetRow = row;
            dropBelow = !rowMidpoint(event, row);
            row.classList.add(dropBelow ? 'ai-task-drop-below' : 'ai-task-drop-above');
        });

        body.addEventListener('drop', function (event) {
            if (!dragRow || !dropTargetRow) return;
            event.preventDefault();
            var target = dropTargetRow;
            var below = dropBelow;
            clearMarkers();
            // Переставляем строку в DOM: DOM-порядок = новый порядок доски.
            if (below) {
                target.after(dragRow);
            } else {
                target.before(dragRow);
            }
            dropTargetRow = null;
            // Собираем новый порядок id и отправляем: успех — обновляем «№»
            // на месте (без reload, скролл сохраняется), ошибка — reload,
            // чтобы серверная сортировка не разошлась с DOM.
            var ids = [];
            var rows = body.querySelectorAll('tr[data-task-id]');
            for (var i = 0; i < rows.length; i++) {
                rows[i].classList.remove('ai-task-dragging');
                ids.push(parseInt(rows[i].getAttribute('data-task-id'), 10));
            }
            post('/ai/admin/tasks/reorder/', JSON.stringify({ ids: ids }))
                .then(function () {
                    for (var i = 0; i < rows.length; i++) {
                        var cell = rows[i].querySelector('.ai-task-col-no');
                        if (cell) cell.textContent = String(i + 1);
                    }
                })
                .catch(function (err) {
                    alert(err.message);
                    window.location.reload();
                });
        });
    }
})();