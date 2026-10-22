// Страница «Запросы по DL API» (/ai/admin/dl-api-requests/).
// Отметка «API создано» / «Вернуть в актуальные» — POST fetch (JSON).
(function () {
    'use strict';

    function csrftoken() {
        var input = document.querySelector('input[name="csrfmiddlewaretoken"]');
        if (input && input.value) return input.value;
        var m = document.cookie.match(/(?:^|;\s*)(?:ai_)?csrftoken=([^;]+)/);
        return m ? decodeURIComponent(m[1]) : '';
    }

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
                throw new Error('Сервер вернул не JSON, HTTP ' + response.status + ': ' + head);
            }
        });
    }

    function init() {
        var wrap = document.querySelector('.ai-dlapi-wrap');
        if (!wrap) return;
        var updateUrl = wrap.getAttribute('data-update-url') || '';
        var createUrl = wrap.getAttribute('data-create-url') || '';

        // === Создание заявки (форма в админке) =============================
        var createForm = document.getElementById('ai-dlapi-create-form');
        if (createForm && createUrl) {
            createForm.addEventListener('submit', function (event) {
                event.preventDefault();
                var field = document.getElementById('ai-dlapi-description');
                var status = createForm.querySelector('.ai-dlapi-create-status');
                var submitBtn = createForm.querySelector('.ai-dlapi-create-submit');
                var description = field ? field.value.trim() : '';
                if (!description) {
                    if (status) { status.textContent = 'Опишите, какой API нужен'; status.className = 'ai-dlapi-create-status ai-dlapi-create-err'; }
                    if (field) { field.focus(); }
                    return;
                }
                if (submitBtn) { submitBtn.disabled = true; }
                if (status) { status.textContent = 'Создаём…'; status.className = 'ai-dlapi-create-status'; }
                fetch(createUrl, {
                    method: 'POST',
                    headers: {
                        'X-CSRFToken': csrftoken(),
                        'X-Requested-With': 'XMLHttpRequest',
                        'Content-Type': 'application/json',
                    },
                    credentials: 'same-origin',
                    body: JSON.stringify({ description: description }),
                })
                    .then(function (response) {
                        return parseJsonResponse(response).then(function (data) {
                            if (!data.ok) {
                                throw new Error(data.message || 'Не удалось создать заявку');
                            }
                            return data;
                        });
                    })
                    .then(function () {
                        // Заявка попала во вкладку «Актуальные» — перерисовываем.
                        window.location.reload();
                    })
                    .catch(function (err) {
                        if (submitBtn) { submitBtn.disabled = false; }
                        if (status) { status.textContent = err.message || 'Ошибка'; status.className = 'ai-dlapi-create-status ai-dlapi-create-err'; }
                    });
            });
        }

        wrap.addEventListener('click', function (event) {
            var target = event.target;
            if (!target || !target.classList) return;
            var status = '';
            if (target.classList.contains('ai-dlapi-done')) {
                status = 'done';
            } else if (target.classList.contains('ai-dlapi-reopen')) {
                status = 'pending';
            } else {
                return;
            }
            var id = Number(target.getAttribute('data-id'));
            var comment = window.prompt('Комментарий (необязательно):', '') || '';
            target.disabled = true;
            fetch(updateUrl, {
                method: 'POST',
                headers: {
                    'X-CSRFToken': csrftoken(),
                    'X-Requested-With': 'XMLHttpRequest',
                    'Content-Type': 'application/json',
                },
                credentials: 'same-origin',
                body: JSON.stringify({ id: id, status: status, comment: comment }),
            })
                .then(function (response) {
                    return parseJsonResponse(response).then(function (data) {
                        if (!data.ok) {
                            throw new Error(data.message || 'Не удалось выполнить действие');
                        }
                        return data;
                    });
                })
                .then(function () {
                    window.location.reload();
                })
                .catch(function (err) {
                    target.disabled = false;
                    window.alert(err.message || 'Ошибка');
                });
        });
    }

    document.addEventListener('DOMContentLoaded', init);
})();
