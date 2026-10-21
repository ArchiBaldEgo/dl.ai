// Доска «Таски» (ai/admin/tasks.py): создать/взять/закрыть таск, вернуть
// закрытый («Закрытые»; двухшажно — спросить, оставить ли исполнителя,
// «Освободить» шлёт free_executor) — POST fetch; переставить строки
// фронта/хотелок — HTML5 drag & drop + POST /tasks/reorder/ ({kind, ids};
// сервер перенумеровывает 1..N в пределах вида). Фильтры «Кто взял»
// (селект-автосабмит) и дат закрытия архива — обычные GET-формы.
// JS на делегировании, IIFE.
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
                return data;
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

    // «Вернуть» раскрывает выбор исполнителя в своей ячейке (см. шаблон):
    // main-кнопка data-reopen-ask прячется, показывается ai-task-reopen-choice.
    function expandReopenChoice(btn) {
        var cell = btn.closest('.ai-task-actions');
        if (!cell) return;
        btn.hidden = true;
        var choice = cell.querySelector('.ai-task-reopen-choice');
        if (choice) choice.hidden = false;
    }

    function collapseReopenChoice(el) {
        var cell = el.closest('.ai-task-actions');
        if (!cell) return;
        var choice = cell.querySelector('.ai-task-reopen-choice');
        if (choice) choice.hidden = true;
        var main = cell.querySelector('[data-reopen-ask]');
        if (main) main.hidden = false;
    }

    // Правка таска: форма живёт ОТДЕЛЬНОЙ строкой таблицы (data-edit-row
    // с тем же id), кнопка «Изменить» — в строке самого таска. Показ/скрыв
    // по кнопке; в форме — отправка JSON на tasks/<id>/edit/.
    function toggleEditForm(el, show) {
        var row = el.closest('tr[data-task-id]');
        if (!row) return;
        var taskId = row.getAttribute('data-task-id');
        var editRow = document.querySelector('tr[data-edit-row="' + taskId + '"]');
        if (!editRow) return;
        editRow.hidden = !show;
        var btn = row.querySelector('[data-edit-toggle]');
        if (btn) btn.hidden = show;
        if (show) {
            var input = editRow.querySelector('input[name="title"]');
            if (input) { input.focus(); input.select(); }
        }
    }

    // Взять/закрыть/вернуть — успех перезагружает страницу: статусы строк
    // пересчитывает сервер (на фронте после reorder reload не нужен).
    // «Вернуть» — двухшажный: первый клик спрашивает, оставить ли
    // исполнителя; «С исполнителем» шлёт POST без тела, «Освободить»
    // — {"free_executor": true} (исполнитель сбрасывается, таск в
    // «Не взятые»).
    document.addEventListener('click', function (event) {
        var ask = event.target.closest('[data-reopen-ask]');
        if (ask) { expandReopenChoice(ask); return; }
        var cancel = event.target.closest('.ai-task-btn-reopen-cancel');
        if (cancel) { collapseReopenChoice(cancel); return; }
        // Правка таска (только автору): «Изменить» раскрывает форму правки
        // в строке под таском, «Отмена» прячет её обратно.
        var editToggle = event.target.closest('[data-edit-toggle]');
        if (editToggle) { toggleEditForm(editToggle, true); return; }
        var editCancel = event.target.closest('.ai-task-btn-edit-cancel');
        if (editCancel) { toggleEditForm(editCancel, false); return; }
        var freeBtn = event.target.closest('[data-reopen-free]');
        if (freeBtn) {
            var freeUrl = freeBtn.getAttribute('data-reopen-url');
            if (freeUrl) {
                guard(freeBtn, function () {
                    return post(freeUrl, JSON.stringify({ free_executor: true }))
                        .then(function () { window.location.reload(); });
                });
                return;
            }
        }
        var btn = event.target.closest(
            '[data-claim-url], [data-close-url], [data-reopen-url]'
        );
        if (!btn) return;
        var url = btn.getAttribute('data-claim-url')
            || btn.getAttribute('data-close-url')
            || btn.getAttribute('data-reopen-url');
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

    // Фильтр «Кто взял» на «Поиске»: смена селекта — GET этой же страницы.
    document.addEventListener('change', function (event) {
        var select = event.target.closest('[data-autosubmit="1"]');
        if (select && select.form) select.form.submit();
    });

    // Правка таска: сабмит формы правки — POST JSON {title, description,
    // estimate} на tasks/<id>/edit/. Делегирование (форма может быть
    // добавлена в DOM сервером при каждой перерисовке строк).
    document.addEventListener('submit', function (event) {
        var form = event.target.closest('.ai-task-edit-form');
        if (!form) return;
        event.preventDefault();
        var url = form.getAttribute('data-edit-url');
        if (!url) return;
        var btn = form.querySelector('[type="submit"]');
        if (btn) btn.classList.add('busy');
        var payload = {
            title: (form.querySelector('input[name="title"]') || {}).value || '',
            description: (form.querySelector('textarea[name="description"]') || {}).value || '',
            estimate: (form.querySelector('input[name="estimate"]') || {}).value || '',
        };
        post(url, JSON.stringify(payload))
            .then(function (data) {
                // Обновляем карточку НА МЕСТЕ: перезагрузка страницы выкидывала
                // оператора наверх (он правил таск в середине доски).
                applyEditInPlace(form, data || payload);
            })
            .catch(function (err) {
                if (btn) btn.classList.remove('busy');
                alert(err.message);
            });
    });

    // Карточка таска после правки: значения в строке обновляются, форма
    // правки сворачивается, страница НЕ перезагружается (скролл сохраняется).
    function applyEditInPlace(form, data) {
        var editRow = form.closest('tr[data-edit-row]');
        if (!editRow) return;
        var taskId = editRow.getAttribute('data-edit-row');
        var taskRow = document.querySelector('tr[data-task-id="' + taskId + '"]');
        if (taskRow) {
            var title = taskRow.querySelector('.ai-task-title');
            if (title) title.textContent = data.title || '';
            var description = taskRow.querySelector('.ai-task-description');
            if (description) description.textContent = data.description || '—';
            var estimate = taskRow.querySelector('.ai-task-estimate');
            if (estimate) estimate.textContent = data.estimate || '—';
        }
        // Значения в самой форме — чтобы повторное «Изменить» показывало
        // уже сохранённые (а не старые) значения.
        var titleInput = form.querySelector('input[name="title"]');
        if (titleInput && data.title) titleInput.value = data.title;
        var descriptionInput = form.querySelector('textarea[name="description"]');
        if (descriptionInput && data.description) descriptionInput.value = data.description;
        var estimateInput = form.querySelector('input[name="estimate"]');
        if (estimateInput && data.estimate) estimateInput.value = data.estimate;
        var btn = form.querySelector('[type="submit"]');
        if (btn) btn.classList.remove('busy');
        // Сворачиваем форму правки и возвращаем кнопку «Изменить».
        editRow.hidden = true;
        if (taskRow) {
            var editBtn = taskRow.querySelector('[data-edit-toggle]');
            if (editBtn) editBtn.hidden = false;
        }
    }

    // --- Перетаскивание строк фронта/хотелок (только table[data-reorder="1"]) ---
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
            post('/ai/admin/tasks/reorder/', JSON.stringify({
                kind: table.getAttribute('data-kind') || 'front',
                ids: ids,
            }))
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