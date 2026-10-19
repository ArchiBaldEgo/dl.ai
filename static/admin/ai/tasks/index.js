// Доска «Таски» (ai/admin/tasks.py): создать/взять/закрыть таск — POST fetch.
// JS целиком на делегировании, выживает после любого замен DOM. Успех →
// location.reload(): сортировку и статусы строк пересчитывает сервер.
(function () {
    'use strict';

    // CSRF — как в pinned_runs.html / app_settings/index.js: из инпута
    // формы ({% csrf_token %} формы создания) или куки csrftoken.
    function csrftoken() {
        var input = document.querySelector('input[name="csrfmiddlewaretoken"]');
        if (input && input.value) return input.value;
        var m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
        return m ? decodeURIComponent(m[1]) : '';
    }

    // AJAX админки всегда ждёт JSON: при протухшей сессии admin_view
    // редиректит fetch на страницу логина (HTML) — честный текст вместо
    // ошибки парсинга (та же конвенция, что в arm_solve/test_console).
    function parseJsonResponse(response) {
        return response.text().then(function (text) {
            try {
                return JSON.parse(text);
            } catch (e) {
                throw new Error('Сессия истекла — обновите страницу и войдите заново');
            }
        });
    }

    function post(url) {
        return fetch(url, {
            method: 'POST',
            headers: { 'X-CSRFToken': csrftoken(), 'X-Requested-With': 'XMLHttpRequest' },
            credentials: 'same-origin',
        })
            .then(function (response) {
                return parseJsonResponse(response);
            })
            .then(function (data) {
                if (!data.ok) {
                    throw new Error(data.message || 'Не удалось выполнить действие');
                }
                window.location.reload();
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

    document.addEventListener('click', function (event) {
        var btn = event.target.closest('[data-claim-url], [data-close-url]');
        if (!btn) return;
        var url = btn.getAttribute('data-claim-url') || btn.getAttribute('data-close-url');
        if (!url) return;
        guard(btn, function () { return post(url); });
    });

    var createForm = document.querySelector('.ai-tasks-create');
    if (createForm) {
        createForm.addEventListener('submit', function (event) {
            event.preventDefault();
            var btn = createForm.querySelector('[type="submit"]');
            var data = new FormData(createForm);
            if (btn) btn.classList.add('busy');
            fetch(createForm.getAttribute('data-create-url'), {
                method: 'POST',
                headers: { 'X-CSRFToken': csrftoken(), 'X-Requested-With': 'XMLHttpRequest' },
                credentials: 'same-origin',
                body: data,
            })
                .then(function (response) {
                    return parseJsonResponse(response);
                })
                .then(function (payload) {
                    if (!payload.ok) {
                        throw new Error(payload.message || 'Не удалось создать таск');
                    }
                    window.location.reload();
                })
                .catch(function (err) {
                    if (btn) btn.classList.remove('busy');
                    alert(err.message);
                });
        });
    }
})();