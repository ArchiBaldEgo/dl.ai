// Страница «Группы доступа» (ai/admin/access_groups.py): карточки-аккордеоны,
// права в ДВЕ колонки (слева страницы/доки, справа действия активных страниц),
// сохранение прав / удаление группы / AJAX-поиск участников / построчный
// add-remove состава. JS на делегировании (выживает после replaceWith).
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
    // ошибки парсинга. Прочие не-JSON (CSRF 403, 500) — со статусом и
    // началом тела: маскировать всё под «сессию» нельзя, это прятало
    // настоящую причину отказа (2026-10: удаление группы падало с 403).
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

    // Тело c токеном: header'а мало, если прод когда-то грызёт куки —
    // дублируем csrfmiddlewaretoken прямо в POST (Django берёт тело раньше).
    function withToken(body) {
        var token = csrftoken();
        if (token && typeof body.set === 'function') body.set('csrfmiddlewaretoken', token);
        return body;
    }

    // thenReload=false — мутация живёт без перезагрузки: busy снимается сам.
    function guard(container, handler, thenReload) {
        if (container.classList.contains('busy')) return Promise.resolve();
        container.classList.add('busy');
        var done = function () {
            if (thenReload === false) {
                container.classList.remove('busy');
                return;
            }
            window.location.reload();
        };
        var fail = function (err) {
            container.classList.remove('busy');
            alert(err && err.message ? err.message : String(err));
        };
        return handler().then(done, fail);
    }

    // --- Аккордеон карточек: клик по шапке раскрывает/скрывает настройку. ---
    function setCardOpen(card, open) {
        card.classList.toggle('ai-group-open', open);
        var head = card.querySelector('.ai-group-head');
        if (head) head.setAttribute('aria-expanded', open ? 'true' : 'false');
    }

    // --- Колонка «Действия»: активные страницы + их вложенные действия. ---
    // Блок страницы скрыт, пока её галочка слева не включена (в базе или в
    // галочках группы); включение/снятие галочки показывает/прячет его сразу.
    function syncActionColumn(form) {
        form.querySelectorAll('[data-ai-actions-for]').forEach(function (block) {
            var key = block.getAttribute('data-ai-actions-for');
            var page = form.querySelector('input[name="permissions"][value="' + key + '"]');
            block.hidden = !(page && page.checked);
        });
    }

    document.addEventListener('change', function (event) {
        var page = event.target.closest('input[name="permissions"]');
        if (!page) return;
        var form = page.closest('form.ai-groups-form');
        if (form && page.closest('.ai-groups-col-left')) syncActionColumn(form);
    });

    document.addEventListener('click', function (event) {
        var head = event.target.closest('.ai-group-head');
        if (!head || event.target.closest('[data-toggle-url], [data-delete-url], .ai-groups-compose')) return;
        var card = head.closest('[data-group-card]');
        if (card) setCardOpen(card, !card.classList.contains('ai-group-open'));
    });

    // Клавиатура на шапке (role="button", tabindex=0).
    document.addEventListener('keydown', function (event) {
        if (event.key !== 'Enter' && event.key !== ' ') return;
        var head = event.target.closest('.ai-group-head');
        if (!head || event.target.closest('input, textarea, .button')) return;
        event.preventDefault();
        var card = head.closest('[data-group-card]');
        if (card) setCardOpen(card, !card.classList.contains('ai-group-open'));
    });

    // «Состав» — раскрыть карточку и показать блок состава.
    document.addEventListener('click', function (event) {
        var compose = event.target.closest('.ai-groups-compose');
        if (!compose) return;
        var card = compose.closest('[data-group-card]');
        if (!card) return;
        setCardOpen(card, true);
        var members = card.querySelector('.ai-groups-members');
        if (members) {
            members.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
            var input = card.querySelector('.ai-groups-search input[type="text"]');
            if (input) input.focus();
        }
    });

    // --- Редактор состава: поиск AJAX'ом, add/remove без перезагрузки. ---
    // Строка участника рендерится тем же DOM, что и серверный
    // (data-toggle-url с действием — движок мутации знает обе формы).
    function bumpMemberCount(card, delta) {
        var count = card.querySelector('.ai-group-count');
        if (!count) return;
        var m = count.textContent.match(/\d+/);
        if (!m) return;
        count.textContent = (parseInt(m[0], 10) + delta) + ' участн.';
    }

    document.addEventListener('input', function (event) {
        var input = event.target.closest('.ai-groups-search input[type="text"]');
        if (!input) return;
        var box = input.closest('.ai-groups-search');
        var q = input.value.trim();
        clearTimeout(box._timer);
        box._timer = setTimeout(function () {
            var results = box.querySelector('.ai-groups-search-results');
            if (!q) { results.hidden = true; return; }
            fetch(box.getAttribute('data-search-url') + '?q=' + encodeURIComponent(q), {
                headers: { 'X-Requested-With': 'XMLHttpRequest' },
                credentials: 'same-origin',
            })
                .then(function (response) { return parseJsonResponse(response); })
                .then(function (data) {
                    var list = results.querySelector('.ai-groups-search-list');
                    list.textContent = '';
                    var users = data.users || [];
                    users.forEach(function (user) {
                        var row = document.createElement('div');
                        row.className = 'ai-groups-user';
                        var name = document.createElement('span');
                        name.textContent = user.name;
                        row.appendChild(name);
                        var btn = document.createElement('button');
                        btn.type = 'button';
                        btn.className = 'button ai-group-add';
                        btn.textContent = 'Добавить';
                        btn.setAttribute('data-toggle-url', user.toggle_url);
                        btn.setAttribute('data-user-id', user.id);
                        btn.setAttribute('data-action', 'add');
                        row.appendChild(btn);
                        list.appendChild(row);
                    });
                    if (!users.length) {
                        var empty = document.createElement('p');
                        empty.className = 'ai-groups-empty';
                        empty.textContent = 'Никого не найдено.';
                        list.appendChild(empty);
                    }
                    results.hidden = false;
                })
                .catch(function () { /* тише: промах поиска не роняет страницу */ });
        }, 250);
    });

    document.addEventListener('submit', function (event) {
        var form = event.target.closest('.ai-groups-form, .ai-groups-create');
        if (!form) return;
        event.preventDefault();
        // Сохранение существующей группы: фиксируем раскрытую карточку в URL
        // (?group=<id>) ДО reload — reload вернёт к той же раскрытой группе.
        var card = form.closest('[data-group-card]');
        if (card) {
            var toggleBtn = card.querySelector('[data-toggle-url]');
            var m = (toggleBtn && toggleBtn.getAttribute('data-toggle-url') || '')
                .match(/ai-groups\/(\d+)\//);
            if (m) window.history.replaceState(null, '', '?group=' + m[1]);
        }
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
            var card = btn.closest('[data-group-card]');
            guard(btn, function () {
                return post(btn.getAttribute('data-toggle-url'), withToken(data));
            }, false).then(function () {
                var action = btn.getAttribute('data-action') || 'add';
                var row = btn.closest('.ai-groups-user');
                var memberName = row ? row.querySelector('span').textContent : '';
                if (row) row.remove();
                if (action === 'add' && memberName) {
                    // Из результатов поиска — в «Текущие участники» (локально).
                    var members = card.querySelector('.ai-groups-current');
                    if (members) {
                        var empty = members.querySelector('.ai-groups-empty');
                        if (empty) empty.remove();
                        var moved = document.createElement('div');
                        moved.className = 'ai-groups-user';
                        var name = document.createElement('span');
                        name.textContent = memberName;
                        moved.appendChild(name);
                        var removeBtn = document.createElement('button');
                        removeBtn.type = 'button';
                        removeBtn.className = 'button ai-group-remove';
                        removeBtn.textContent = 'Убрать';
                        removeBtn.setAttribute('data-toggle-url', btn.getAttribute('data-toggle-url'));
                        removeBtn.setAttribute('data-user-id', btn.getAttribute('data-user-id'));
                        removeBtn.setAttribute('data-action', 'remove');
                        moved.appendChild(removeBtn);
                        members.appendChild(moved);
                        var searchInput = card.querySelector('.ai-groups-search input[type="text"]');
                        if (searchInput) {
                            searchInput.value = '';
                            var results = card.querySelector('.ai-groups-search-results');
                            if (results) results.hidden = true;
                        }
                    }
                }
                bumpMemberCount(card, action === 'add' ? 1 : -1);
            });
            return;
        }

        var del = event.target.closest('[data-delete-url]');
        if (!del) return;
        var name = del.getAttribute('data-name') || '';
        var note = del.closest('.ai-group-builtin')
            ? ' Встроенная группа пересоздастся ПУСТОЙ при следующем добавлении участника — права придётся выдать заново.'
            : '';
        if (!window.confirm('Удалить группу «' + name + '»? Права и состав уйдут вместе с ней.' + note)) return;
        guard(del, function () {
            return post(del.getAttribute('data-delete-url'), withToken(new FormData()));
        });
    });
})();