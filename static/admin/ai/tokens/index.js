// Страница «Контроль токенов» (/ai/admin/ai/tokens/).
//
// Фильтры применяются БЕЗ перезагрузки страницы: текстовые поля — с
// паузой (debounce 350 мс), селекты и даты — сразу, Enter — сразу.
// Сервер в XHR-режиме отдаёт только панели (карточки + срезы), они
// подменяются на месте. Так администратор не теряет позицию и не ждёт
// перерисовку всей страницы после каждой буквы.
//
// Приём — тот же, что в журнале запросов (static/admin/ai/request_logs/
// index.js): параметры берём из ФОРМЫ (в URL ещё старые значения),
// предыдущий запрос отменяем через AbortController, адресную строку
// синхронизируем через history.replaceState.
(function () {
    'use strict';

    var WRAPPER_SEL = '#ai-tokens-panels';
    var TEXT_DEBOUNCE_MS = 350;

    function init() {
        var form = document.querySelector('.ai-tokens-filters');
        var wrapper = document.querySelector(WRAPPER_SEL);
        if (!form || !wrapper) return;

        var inflight = null;

        function buildParams() {
            var params = new URLSearchParams();
            new FormData(form).forEach(function (value, key) {
                var v = String(value == null ? '' : value).trim();
                if (v !== '') params.append(key, v);
            });
            return params;
        }

        function refreshPanels() {
            if (inflight) inflight.abort();
            inflight = new AbortController();

            var url = new URL(window.location.pathname, window.location.origin);
            url.search = buildParams().toString();

            fetch(url, {
                headers: { 'X-Requested-With': 'XMLHttpRequest' },
                signal: inflight.signal,
            }).then(function (resp) {
                if (!resp.ok) throw new Error('HTTP ' + resp.status);
                return resp.text();
            }).then(function (html) {
                var doc = new DOMParser().parseFromString(html, 'text/html');
                var fresh = doc.querySelector(WRAPPER_SEL);
                if (!fresh) throw new Error('Панели не найдены в ответе');
                wrapper.replaceWith(fresh);
                wrapper = document.querySelector(WRAPPER_SEL);
                // Адресная строка: F5 и «поделиться ссылкой» сохранят фильтры.
                history.replaceState(null, '', url.toString());
            }).catch(function (err) {
                if (err && err.name === 'AbortError') return;
                console.error('refresh tokens panels failed:', err);
                window.alert('Не удалось обновить таблицы: ' + err.message);
            }).then(function () {
                inflight = null;
            });
        }

        // Текстовые поля → пауза (не гоним запрос на каждую букву).
        form.querySelectorAll('input[type="text"], input[type="search"]').forEach(function (el) {
            var timer = null;
            el.addEventListener('input', function () {
                clearTimeout(timer);
                timer = setTimeout(refreshPanels, TEXT_DEBOUNCE_MS);
            });
            el.addEventListener('keydown', function (e) {
                if (e.key === 'Enter') {
                    e.preventDefault();
                    clearTimeout(timer);
                    refreshPanels();
                }
            });
        });

        // select / date → применяем сразу.
        form.querySelectorAll('select, input[type="date"]').forEach(function (el) {
            el.addEventListener('change', refreshPanels);
        });

        // Enter в любом месте формы — применить, а не навигировать.
        form.addEventListener('submit', function (e) {
            e.preventDefault();
            refreshPanels();
        });

        // «Сбросить» — обычная навигация (чистые фильтры, без ajax).
        var resetLink = form.querySelector('a[href="?"]');
        if (resetLink) {
            resetLink.addEventListener('click', function (e) {
                e.preventDefault();
                window.location.href = window.location.pathname;
            });
        }
    }

    document.addEventListener('DOMContentLoaded', init);
})();
