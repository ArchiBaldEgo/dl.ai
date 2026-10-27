// Панель «Мои AI-модели» — общий скрипт для админской (/ai/admin/user-models/)
// и клиентской (/ai/my-models/) страниц. Разметка одна (partial
// ai/_user_models_panel.html), адреса create/delete/access/search — в
// data-атрибутах контейнера (у страниц разные).
//
// Доступ: чекбокс «Доступна ВСЕМ» (по умолчанию ВЫКЛ + предупреждение) и
// адресная выдача — поиск по DL-id/ФИО, несколько получателей, чипы с ×.
// Суперюзерская таблица «Все модели» управляет ТОЛЬКО доступом: адреса,
// ключи и настройки чужих моделей на страницу не попадают.
//
// Тексты — из json_script «ai-um-i18n» (все три языка): подписи
// перерисовываются при смене языка (#selectLang) на клиентской странице и
// один раз подставляются на админской (там язык не переключается).
(function () {
    'use strict';

    //: Пауза перед поиском получателя (debounce), мс.
    var SEARCH_DEBOUNCE_MS = 300;

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

    function getJson(url) {
        return fetch(url, {
            headers: { 'X-Requested-With': 'XMLHttpRequest' },
            credentials: 'same-origin',
        }).then(function (response) {
            return parseJsonResponse(response).then(function (data) {
                if (!data.ok) {
                    throw new Error(data.message || 'Не удалось выполнить поиск');
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

    // Редактор доступа: поиск получателя + чипы + чекбокс публичности.
    // Один и тот же виджет используется в форме своей модели и в панели
    // суперюзера (DRY); состояние (выбранные получатели) живёт здесь.
    function createAccessEditor(opts) {
        var selected = [];
        var timer = null;
        var requestSeq = 0;

        function renderChips() {
            if (!opts.chipsBox) return;
            opts.chipsBox.textContent = '';
            selected.forEach(function (user) {
                var chip = document.createElement('span');
                chip.className = 'ai-um-chip';
                var label = document.createElement('span');
                label.textContent = user.display || ('#' + user.id);
                if (user.dl_id) label.textContent += ' [' + user.dl_id + ']';
                var remove = document.createElement('button');
                remove.type = 'button';
                remove.className = 'ai-um-chip-remove';
                remove.setAttribute('data-id', String(user.id));
                remove.textContent = '×';
                remove.title = 'Убрать';
                chip.appendChild(label);
                chip.appendChild(remove);
                opts.chipsBox.appendChild(chip);
            });
            if (opts.emptyBox) opts.emptyBox.hidden = selected.length > 0;
        }

        function addUser(user) {
            if (!user || !user.id) return;
            var exists = selected.some(function (item) {
                return String(item.id) === String(user.id);
            });
            if (!exists) selected.push(user);
            renderChips();
            hideResults();
            if (opts.searchInput) opts.searchInput.value = '';
        }

        function removeUser(id) {
            selected = selected.filter(function (item) {
                return String(item.id) !== String(id);
            });
            renderChips();
        }

        function hideResults() {
            if (!opts.resultsBox) return;
            opts.resultsBox.hidden = true;
            opts.resultsBox.textContent = '';
        }

        function showHint(text) {
            if (!opts.hintBox) return;
            opts.hintBox.textContent = text || '';
            opts.hintBox.hidden = !text;
        }

        function renderResults(users) {
            if (!opts.resultsBox) return;
            opts.resultsBox.textContent = '';
            if (!users.length) {
                var none = document.createElement('div');
                none.className = 'ai-um-share-none';
                none.textContent = '—';
                opts.resultsBox.appendChild(none);
                opts.resultsBox.hidden = false;
                return;
            }
            users.forEach(function (user) {
                var item = document.createElement('button');
                item.type = 'button';
                item.className = 'ai-um-share-result';
                item.setAttribute('data-id', String(user.id));
                item.textContent = (user.display || ('#' + user.id)) +
                    (user.dl_id ? ' [' + user.dl_id + ']' : '');
                // Полный объект — на самом элементе: контейнер хранит только
                // id, а чипу нужны display/dl_id (без второго запроса).
                item.aiUser = user;
                opts.resultsBox.appendChild(item);
            });
            opts.resultsBox.hidden = false;
        }

        function search(query) {
            var url = (opts.searchUrl || '') + '?q=' + encodeURIComponent(query);
            var seq = ++requestSeq;
            getJson(url).then(function (data) {
                if (seq !== requestSeq) return; // устаревший ответ
                renderResults(Array.isArray(data.users) ? data.users : []);
            }).catch(function (err) {
                if (seq !== requestSeq) return;
                showHint(err.message || '');
            });
        }

        if (opts.searchInput) {
            opts.searchInput.addEventListener('input', function () {
                var query = opts.searchInput.value.trim();
                showHint('');
                if (timer) window.clearTimeout(timer);
                if (!query) {
                    hideResults();
                    return;
                }
                var digitsOnly = /^\d+$/.test(query);
                if (!digitsOnly && query.length < 2) {
                    hideResults();
                    showHint(opts.tr('shareMinChars', 'Введите минимум 2 символа или DL-id.'));
                    return;
                }
                timer = window.setTimeout(function () {
                    search(query);
                }, SEARCH_DEBOUNCE_MS);
            });
        }

        if (opts.resultsBox) {
            opts.resultsBox.addEventListener('click', function (event) {
                var target = event.target;
                if (!target || !target.classList) return;
                if (!target.classList.contains('ai-um-share-result')) return;
                if (target.aiUser) addUser(target.aiUser);
            });
        }

        if (opts.chipsBox) {
            opts.chipsBox.addEventListener('click', function (event) {
                var target = event.target;
                if (!target || !target.classList) return;
                if (!target.classList.contains('ai-um-chip-remove')) return;
                removeUser(target.getAttribute('data-id'));
            });
        }

        if (opts.publicCheckbox) {
            opts.publicCheckbox.addEventListener('change', function () {
                if (opts.warningBox) {
                    opts.warningBox.hidden = !opts.publicCheckbox.checked;
                }
            });
        }

        return {
            getIds: function () {
                return selected.map(function (user) { return Number(user.id); });
            },
            setGrants: function (grants) {
                selected = (Array.isArray(grants) ? grants : []).map(function (user) {
                    return { id: user.id, display: user.display, dl_id: user.dl_id };
                });
                renderChips();
            },
            setPublic: function (value) {
                if (opts.publicCheckbox) opts.publicCheckbox.checked = !!value;
                if (opts.warningBox) {
                    opts.warningBox.hidden = !(opts.publicCheckbox && opts.publicCheckbox.checked);
                }
            },
            reset: function () {
                selected = [];
                renderChips();
                hideResults();
                showHint('');
                if (opts.searchInput) opts.searchInput.value = '';
                if (opts.publicCheckbox) opts.publicCheckbox.checked = false;
                if (opts.warningBox) opts.warningBox.hidden = true;
            },
        };
    }

    function init() {
        var wrap = document.querySelector('.ai-um-wrap');
        if (!wrap) return;
        var form = document.getElementById('ai-um-form');
        if (!form) return;
        var createUrl = wrap.getAttribute('data-create-url') || '';
        var deleteUrl = wrap.getAttribute('data-delete-url') || '';
        var accessUrl = wrap.getAttribute('data-access-url') || '';
        var searchUrl = wrap.getAttribute('data-search-url') || '';
        var providerUrls = readJson('ai-um-provider-urls');
        var rows = readJson('ai-um-rows');
        var allRows = readJson('ai-um-all-rows');
        var i18n = readJson('ai-um-i18n');
        var rowsById = {};
        for (var i = 0; i < rows.length; i++) {
            rowsById[String(rows[i].id)] = rows[i];
        }
        var allRowsById = {};
        for (var a = 0; a < allRows.length; a++) {
            allRowsById[String(allRows[a].id)] = allRows[a];
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

        var editor = createAccessEditor({
            searchInput: document.getElementById('ai-um-share-search'),
            resultsBox: document.getElementById('ai-um-share-results'),
            chipsBox: document.getElementById('ai-um-share-chips'),
            emptyBox: document.getElementById('ai-um-share-empty'),
            hintBox: document.getElementById('ai-um-share-hint'),
            publicCheckbox: document.getElementById('ai-um-public'),
            warningBox: document.getElementById('ai-um-public-warning'),
            searchUrl: searchUrl,
            tr: tr,
        });

        // === Суперюзерская таблица «Все модели»: только доступ ===
        var accessPanel = document.getElementById('ai-um-access-panel');
        var allTable = document.getElementById('ai-um-all-models');
        var allMessage = document.getElementById('ai-um-access-message');
        var accessModelId = null;

        function setAllMessage(text, kind) {
            if (!allMessage) return;
            allMessage.textContent = text || '';
            allMessage.className = 'ai-um-message' + (kind ? ' ai-um-message-' + kind : '');
        }

        var accessEditor = null;
        if (accessPanel) {
            accessEditor = createAccessEditor({
                searchInput: document.getElementById('ai-um-access-search'),
                resultsBox: document.getElementById('ai-um-access-results'),
                chipsBox: document.getElementById('ai-um-access-chips'),
                emptyBox: document.getElementById('ai-um-access-empty'),
                hintBox: document.getElementById('ai-um-access-hint'),
                publicCheckbox: document.getElementById('ai-um-access-public'),
                warningBox: document.getElementById('ai-um-access-public-warning'),
                searchUrl: searchUrl,
                tr: tr,
            });
        }

        if (accessPanel) {
            document.getElementById('ai-um-access-cancel').addEventListener('click', function () {
                accessPanel.hidden = true;
                accessModelId = null;
                setAllMessage('', '');
            });
            document.getElementById('ai-um-access-save').addEventListener('click', function () {
                if (!accessModelId) return;
                setAllMessage(tr('msgSaving', 'Сохраняем…'), '');
                post(accessUrl, {
                    id: Number(accessModelId),
                    is_public: document.getElementById('ai-um-access-public').checked,
                    shared_with: accessEditor.getIds(),
                }).then(function () {
                    setAllMessage(tr('msgAccessSaved', 'Доступ сохранён.'), 'ok');
                    window.location.reload();
                }).catch(function (err) {
                    setAllMessage(err.message || tr('errAccessSave', 'Не удалось сохранить доступ'), 'err');
                });
            });
        }

        // === Перерисовка подписей при смене языка ===
        // Клиентская страница переключает язык через #selectLang — тексты
        // панели следуют за языком. Админка языком не управляет, но функция
        // безопасна и там (перерисовка по текущему языку).
        function applyAccessCell(cell) {
            var isPublic = cell.getAttribute('data-public') === '1';
            var grants = Number(cell.getAttribute('data-grants') || 0);
            if (isPublic) {
                cell.textContent = tr('accessPublic', 'всем');
            } else if (grants > 0) {
                cell.textContent = grants + ' ' + tr('accessUnit', 'польз.');
            } else {
                cell.textContent = tr('accessPrivate', 'только я');
            }
        }

        function applyTexts() {
            var intro = document.querySelector('.ai-um-intro');
            if (intro) intro.textContent = tr('intro', '');
            var allTitle = document.querySelector('.ai-um-all-title');
            if (allTitle) allTitle.textContent = tr('allModelsTitle', 'Все модели');
            var allIntro = document.getElementById('ai-um-all-intro');
            if (allIntro) allIntro.textContent = tr('allModelsIntro', '');
            var title = document.getElementById('ai-um-form-title');
            if (title) {
                title.textContent = form.id.value
                    ? tr('editModel', 'Правка модели')
                    : tr('newModel', 'Новая модель');
            }
            var saveBtn = document.getElementById('ai-um-save');
            if (saveBtn) saveBtn.textContent = tr('btnSave', 'Сохранить модель');
            if (cancelBtn) cancelBtn.textContent = tr('btnCancel', 'Отменить правку');

            // Заголовки таблицы владельца (8 колонок; последняя — кнопки).
            var ownHeaders = document.querySelectorAll('#ai-um-table thead th');
            var headerKeys = ['colTitle', 'colProvider', 'colModelId', 'colUrl',
                'colKey', 'colState', 'colAccess'];
            for (var h = 0; h < ownHeaders.length && h < headerKeys.length; h++) {
                ownHeaders[h].textContent = tr(headerKeys[h], ownHeaders[h].textContent);
            }
            // Заголовки суперюзерской таблицы (отдельный набор).
            var allHeaders = document.querySelectorAll('#ai-um-all-models thead th');
            var allKeys = ['colTitle', 'colOwner', 'colAccess', 'colState', 'colManage'];
            for (var ah = 0; ah < allHeaders.length && ah < allKeys.length; ah++) {
                allHeaders[ah].textContent = tr(allKeys[ah], allHeaders[ah].textContent);
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
            var accessBtns = document.querySelectorAll('.ai-um-access-edit');
            for (var ab = 0; ab < accessBtns.length; ab++) {
                accessBtns[ab].textContent = tr('btnAccess', 'Настроить доступ');
            }
            var accessCells = document.querySelectorAll('.ai-um-access');
            for (var ac = 0; ac < accessCells.length; ac++) {
                applyAccessCell(accessCells[ac]);
            }
            var keyCells = document.querySelectorAll('#ai-um-table tbody tr td:nth-child(5)');
            for (var k = 0; k < keyCells.length; k++) {
                var cell = keyCells[k];
                var hasKey = cell.getAttribute('data-has-key') === '1';
                cell.textContent = hasKey ? tr('keySet', 'задан') : tr('keyNone', 'нет');
            }
            var stateCells = document.querySelectorAll('#ai-um-table tbody tr td:nth-child(6)');
            for (var s = 0; s < stateCells.length; s++) {
                var scell = stateCells[s];
                var isActive = scell.getAttribute('data-active') === '1';
                scell.textContent = isActive ? tr('stateOn', 'активна') : tr('stateOff', 'выключена');
            }
            var allStateCells = document.querySelectorAll('#ai-um-all-models tbody tr td:nth-child(4)');
            for (var as = 0; as < allStateCells.length; as++) {
                var acell = allStateCells[as];
                var active = acell.getAttribute('data-active') === '1';
                acell.textContent = active ? tr('stateOn', 'активна') : tr('stateOff', 'выключена');
            }
            var emptyCell = document.querySelector('.ai-um-empty');
            if (emptyCell) emptyCell.textContent = tr('empty', 'У вас пока нет своих моделей.');
            var publicLabel = document.getElementById('ai-um-public-label');
            if (publicLabel) publicLabel.textContent = tr('checkPublic', 'Доступна ВСЕМ');
            var accessPublicLabel = document.getElementById('ai-um-access-public-label');
            if (accessPublicLabel) accessPublicLabel.textContent = tr('checkPublic', 'Доступна ВСЕМ');
            var publicWarnings = document.querySelectorAll('.ai-um-public-warning');
            for (var w = 0; w < publicWarnings.length; w++) {
                publicWarnings[w].textContent = tr('publicWarning', '');
            }
            var shareLabels = document.querySelectorAll('label[for="ai-um-share-search"]');
            for (var sl = 0; sl < shareLabels.length; sl++) {
                shareLabels[sl].textContent = tr('fieldShare', 'Доступ к модели');
            }
            var shareInputs = document.querySelectorAll('#ai-um-share-search, #ai-um-access-search');
            for (var si = 0; si < shareInputs.length; si++) {
                shareInputs[si].placeholder = tr('shareSearchPlaceholder', '');
            }
            var shareEmpty = document.querySelectorAll('.ai-um-share-empty');
            for (var se = 0; se < shareEmpty.length; se++) {
                shareEmpty[se].textContent = tr('shareEmpty', '');
            }
            var panelTitle = document.querySelector('.ai-um-access-panel h4');
            if (panelTitle) {
                panelTitle.textContent = tr('accessPanelTitle', 'Доступ к модели') + ': ';
                panelTitle.appendChild(document.getElementById('ai-um-access-panel-title'));
            }
            var saveAccess = document.getElementById('ai-um-access-save');
            if (saveAccess) saveAccess.textContent = tr('btnSaveAccess', 'Сохранить доступ');
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
            editor.reset();
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
                is_public: form.is_public.checked,
                shared_with: editor.getIds(),
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
                editor.setGrants(row.grants || []);
                editor.setPublic(!!row.is_public);
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
                return;
            }

            if (target.classList.contains('ai-um-access-edit')) {
                var openId = target.getAttribute('data-id');
                var model = allRowsById[String(openId)];
                if (!model || !accessPanel) return;
                accessModelId = openId;
                var panelTitleName = document.getElementById('ai-um-access-panel-title');
                if (panelTitleName) panelTitleName.textContent = model.title || '';
                accessEditor.setGrants(model.grants || []);
                accessEditor.setPublic(!!model.is_public);
                setAllMessage('', '');
                accessPanel.hidden = false;
                accessPanel.scrollIntoView({ behavior: 'smooth', block: 'start' });
            }
        });

        applyTexts();
    }

    document.addEventListener('DOMContentLoaded', init);
})();
