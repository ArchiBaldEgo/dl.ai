// Страница «Группы доступа» (ai/admin/access_groups.py): сохранить права
// группы / удалить группу / toggle участника — POST fetch. JS на делегировании
// (выживает после любого replaceWith), успех → location.reload().
(function () {
    'use strict';

    // CSRF: из инпута формы ({% csrf_token %}) или куки — в проде имя куки
    // переименовано в ai_csrftoken, поэтому матчим оба варианта.
    function csrftoken() {
        var input = document.querySelector('input[name="csrfmiddlewaretoken"]');
        if (input && input.value) return input.value;
        var m = document.cookie.match(/(?:^|;\s*)(?:ai_)?csrftoken=([^;]+)/);
        return m ? decodeURIComponent(m[1]) : '';
    }

    // AJAX админки всегда ждёт JSON: при протухшей сессии admin_view
    // редиректит fetch на страницу логина (HTML) — честный текст вместо
    // ошибки парсинга.
    function parseJsonResponse(response) {
        return response.text().then(function (text) {
            try {
                return JSON.parse(text);
            } catch (e) {
                throw new Error('Сессия истекла — обновите страницу и войдите заново');
            }
        });
    }

    function post(url, body) {
        return fetch(url, {
            method: 'POST',
            headers: {
                'X-CSRFToken': csrftoken(),
                'X-Requested-With': 'XMLHttpRequest',
            },
            credentials: 'same-origin',
            body: body,
        })
            .then(function (response) { return parseJsonResponse(response); })
            .then(function (data) {
                if (!data.ok) {
                    throw new Error(data.message || 'Не удалось выполнить действие');
                }
            });
    }

    function guard(container, handler) {
        if (container.classList.contains('busy')) return;
        container.classList.add('busy');
        handler().then(function () {
            window.location.reload();
        }).catch(function (err) {
            container.classList.remove('busy');
            alert(err.message);
        });
    }

    // Формы сохранения группы: имя/описание/чекбоксы прав.
    document.addEventListener('submit', function (event) {
        var form = event.target.closest('.ai-groups-form, .ai-groups-create');
        if (!form) return;
        event.preventDefault();
        guard(form, function () {
            return post(form.getAttribute('data-save-url'), new FormData(form));
        });
    });

    // Toggle участника: построчный add/remove, состав не затирается частично.
    document.addEventListener('click', function (event) {
        var btn = event.target.closest('[data-toggle-url]');
        if (btn) {
            var data = new URLSearchParams();
            data.set('user_id', btn.getAttribute('data-user-id'));
            data.set('action', btn.getAttribute('data-action') || 'add');
            guard(btn, function () {
                return post(btn.getAttribute('data-toggle-url'), data);
            });
            return;
        }

        var del = event.target.closest('[data-delete-url]');
        if (!del) return;
        if (!window.confirm('Удалить группу? Права и состав уйдут вместе с ней.')) return;
        guard(del, function () {
            return post(del.getAttribute('data-delete-url'), new FormData());
        });
    });
})();