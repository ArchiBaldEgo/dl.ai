// Панель «Мои AI-модели» — общий скрипт для админской (/ai/admin/user-models/)
// и клиентской (/ai/my-models/) страниц. Разметка одна (partial
// ai/_user_models_panel.html), адреса create/delete — в data-атрибутах
// контейнера (у страниц разные).
//
// Тексты — из json_script «ai-um-i18n» (все три языка): подписи
// перерисовываются при смене языка (#selectLang) на клиентской странице и
// один раз подставляются на админской (там язык не переключается).
(function () {
    'use strict';

    function csrftoken() {
        var input = document.querySelector('input[name="csrfmiddlewaretoken"]');
        if (input && input.value) return input.value;
        var m = document.cookie.match(/(?:^|;\s*)(?:ai_)?csrftoken=([^;]+)/);
        return m ? decodeURIComponent(m[1]) : '';
    }

    // AJAX админки всегда ждёт JSON: протухшая сессия → HTML логина.
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

    function post(url, body) {
        return fetch(url, {
            method: 'POST',
            headers: {
                'X-CSRFToken': csrftoken(),
                'X-Requested-With': 'XMLHttpRequest',
                'Content-Type': 'application/json',
            },
            credentials: 'same-origin',
            body: JSON.stringify(body),
        })
            .then(function (response) {
                return parseJsonResponse(response).then(function (data) {
                    if (!data.ok) {
                        throw new Error(data.message || 'Не удалось выполнить действие');
                    }
                    return data;
                });
            });
    }

    function readJson(id) {
        var el = document.getElementById(id);
        if (!el) return {};
        try {
            return JSON.parse(el.textContent) || {};
        } catch (e) {
            return {};
        }
    }

    // Текущий язык интерфейса: значение селекта #selectLang (на клиенте) —
    // иначе русский (админка не переводится).
    function currentLang() {
        var sel = document.getElementById('selectLang');
        if (sel && sel.selectedIndex >= 0) {
            var attr = sel.options[sel.selectedIndex].getAttribute('language');
            if (attr) return attr;
        }
        return 'Русский';
    }

    function init() {
        var wrap = document.querySelector('.ai-um-wrap');
        if (!wrap) return;
        var form = document.getElementById('ai-um-form');
        if (!form) return;
        var createUrl = wrap.getAttribute('data-create-url') || '';
        var deleteUrl = wrap.getAttribute('data-delete-url') || '';
        var providerUrls = readJson('ai-um-provider-urls');
        var rows = readJson('ai-um-rows');
        var i18n = readJson('ai-um-i18n');
        var rowsById = {};
        for (var i = 0; i < rows.length; i++) {
            rowsById[String(rows[i].id)] = rows[i];
        }
        var cancelBtn = document.getElementById('ai-um-cancel');

        function tr(key, fallback) {
            var dict = i18n[currentLang()] || i18n['Русский'] || {};
            return dict[key] || fallback;
        }

        var messageBox = document.getElementById('ai-um-message');
        function setMessage(text, kind) {
            if (!messageBox) return;
            messageBox.textContent = text || '';
            messageBox.className = 'ai-um-message' + (kind ? ' ai-um-message-' + kind : '');
        }

        // === Перерисовка подписей при смене языка ===
        // Клиентская страница переключает язык через #selectLang — тексты
        // панели следуют за языком. Админка языком не управляет, но функция
        // безопасна и там (перерисовка по текущему языку).
        function applyTexts() {
            var intro = document.querySelector('.ai-um-intro');
            if (intro) intro.textContent = tr('intro', '');
            var title = document.getElementById('ai-um-form-title');
            if (title) {
                title.textContent = form.id.value
                    ? tr('editModel', 'Правка модели')
                    : tr('newModel', 'Новая модель');
            }
            var saveBtn = document.getElementById('ai-um-save');
            if (saveBtn) saveBtn.textContent = tr('btnSave', 'Сохранить модель');
            if (cancelBtn) cancelBtn.textContent = tr('btnCancel', 'Отменить правку');
            // Заголовки таблицы.
            var headers = document.querySelectorAll('.ai-um-table thead th');
            var headerKeys = ['colTitle', 'colProvider', 'colModelId', 'colUrl', 'colKey', 'colState'];
            for (var h = 0; h < headers.length && h < headerKeys.length; h++) {
                headers[h].textContent = tr(headerKeys[h], headers[h].textContent);
            }
            // Кнопки и состояния в строках.
            var editBtns = document.querySelectorAll('.ai-um-edit');
            for (var e = 0; e < editBtns.length; e++) {
                editBtns[e].textContent = tr('btnEdit', 'Править');
            }
            var delBtns = document.querySelectorAll('.ai-um-delete');
            for (var d = 0; d < delBtns.length; d++) {
                delBtns[d].textContent = tr('btnDelete', 'Удалить');
            }
            var keyCells = document.querySelectorAll('.ai-um-table tbody tr td:nth-child(5)');
            for (var k = 0; k < keyCells.length; k++) {
                var cell = keyCells[k];
                var hasKey = cell.getAttribute('data-has-key') === '1';
                cell.textContent = hasKey ? tr('keySet', 'задан') : tr('keyNone', 'нет');
            }
            var stateCells = document.querySelectorAll('.ai-um-table tbody tr td:nth-child(6)');
            for (var s = 0; s < stateCells.length; s++) {
                var scell = stateCells[s];
                var isActive = scell.getAttribute('data-active') === '1';
                scell.textContent = isActive ? tr('stateOn', 'активна') : tr('stateOff', 'выключена');
            }
            var emptyCell = document.querySelector('.ai-um-empty');
            if (emptyCell) emptyCell.textContent = tr('empty', 'У вас пока нет своих моделей.');
        }

        var langSel = document.getElementById('selectLang');
        if (langSel) {
            langSel.addEventListener('change', applyTexts);
        }

        function fillProviderUrl() {
            var provider = form.provider.value;
            var field = form.base_url;
            if (!field) return;
            if (field.value) return; // не затираем введённое вручную
            field.placeholder = providerUrls[provider] || 'https://api.example.com/v1';
        }

        form.provider.addEventListener('change', fillProviderUrl);
        fillProviderUrl();

        function resetForm() {
            form.reset();
            form.id.value = '';
            cancelBtn.hidden = true;
            applyTexts();
            fillProviderUrl();
        }

        form.addEventListener('submit', function (event) {
            event.preventDefault();
            setMessage(tr('msgSaving', 'Сохраняем…'), '');
            var payload = {
                id: form.id.value || null,
                title: form.title.value,
                provider: form.provider.value,
                model_id: form.model_id.value,
                base_url: form.base_url.value,
                api_key: form.api_key.value,
                max_tokens: form.max_tokens.value,
                supports_vision: form.supports_vision.checked,
                supports_think: form.supports_think.checked,
                is_active: form.is_active.checked,
            };
            post(createUrl, payload)
                .then(function () {
                    setMessage(tr('msgSaved', 'Сохранено. Обновите страницу, чтобы увидеть модель в списке.'), 'ok');
                    window.location.reload();
                })
                .catch(function (err) {
                    setMessage(err.message || tr('errSave', 'Ошибка сохранения'), 'err');
                });
        });

        cancelBtn.addEventListener('click', function () {
            resetForm();
            setMessage('', '');
        });

        wrap.addEventListener('click', function (event) {
            var target = event.target;
            if (!target || !target.classList) return;

            if (target.classList.contains('ai-um-edit')) {
                var row = rowsById[String(target.getAttribute('data-id'))];
                if (!row) return;
                form.id.value = row.id;
                form.title.value = row.title || '';
                form.provider.value = row.provider || 'openai';
                form.model_id.value = row.model_id || '';
                form.base_url.value = row.base_url || '';
                form.api_key.value = '';
                form.max_tokens.value = row.max_tokens || 8000;
                form.supports_vision.checked = !!row.supports_vision;
                form.supports_think.checked = !!row.supports_think;
                form.is_active.checked = !!row.is_active;
                applyTexts();
                cancelBtn.hidden = false;
                setMessage('', '');
                form.scrollIntoView({ behavior: 'smooth', block: 'start' });
                return;
            }

            if (target.classList.contains('ai-um-delete')) {
                var id = target.getAttribute('data-id');
                if (!window.confirm(tr('msgDeleteConfirm', 'Удалить модель? Её нельзя будет выбрать в чате и на страницах решения.'))) {
                    return;
                }
                setMessage(tr('msgDeleting', 'Удаляем…'), '');
                post(deleteUrl, { id: Number(id) })
                    .then(function () {
                        window.location.reload();
                    })
                    .catch(function (err) {
                        setMessage(err.message || tr('errDelete', 'Ошибка удаления'), 'err');
                    });
            }
        });

        applyTexts();
    }

    document.addEventListener('DOMContentLoaded', init);
})();
