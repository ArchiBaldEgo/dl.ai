// AJAX-фильтры кастомных страниц админки: форма применяет фильтры без
// перезагрузки. Приём тот же, что в журнале запросов и «Контроле токенов»:
// параметры берём из ФОРМЫ (в URL ещё старые значения), запрос отменяем
// через AbortController, результат подменяет только область результатов
// (сама форма с фокусом остаётся на месте).
//
// Использование:
//   initAiAjaxFilters(document.querySelector('.ai-updates-filter'), {
//     targets: ['#ai-updates-results'],       // заменить outerHTML
//     textTargets: ['#ai-updates-count'],     // заменить innerHTML
//     debounceMs: 350,
//     onUpdated: function (doc) {},
//   });
//
// Если разметка результата не нашлась в ответе (протухшая сессия отдала
// страницу логина), выполняется обычный переход по URL.
'use strict';

function initAiAjaxFilters(formEl, opts) {
    if (!formEl) return;
    opts = opts || {};
    var debounceMs = opts.debounceMs || 350;
    var targets = opts.targets || [];
    var textTargets = opts.textTargets || [];
    var inflight = null;
    var timer = null;

    function buildUrl() {
        var params = new URLSearchParams();
        new FormData(formEl).forEach(function (value, key) {
            var text = String(value == null ? '' : value);
            if (text.trim() !== '') params.append(key, text);
        });
        var tail = params.toString();
        return window.location.pathname + (tail ? '?' + tail : '');
    }

    function setBusy(busy) {
        targets.forEach(function (selector) {
            var el = document.querySelector(selector);
            if (el) el.classList.toggle('ai-ajax-busy', busy);
        });
    }

    function apply(doc) {
        var replaced = 0;
        targets.forEach(function (selector) {
            var fresh = doc.querySelector(selector);
            var current = document.querySelector(selector);
            if (fresh && current) {
                current.replaceWith(fresh);
                replaced += 1;
            }
        });
        textTargets.forEach(function (selector) {
            var fresh = doc.querySelector(selector);
            var current = document.querySelector(selector);
            if (fresh && current) {
                current.innerHTML = fresh.innerHTML;
                replaced += 1;
            }
        });
        return replaced;
    }

    function refresh() {
        if (timer) {
            clearTimeout(timer);
            timer = null;
        }
        if (inflight) inflight.abort();
        var controller = new AbortController();
        inflight = controller;

        var url = buildUrl();
        setBusy(true);

        fetch(url, {
            headers: { 'X-Requested-With': 'XMLHttpRequest' },
            credentials: 'same-origin',
            signal: controller.signal,
        })
            .then(function (response) {
                if (!response.ok) throw new Error('HTTP ' + response.status);
                return response.text();
            })
            .then(function (html) {
                var doc = new DOMParser().parseFromString(html, 'text/html');
                if (!apply(doc)) throw new Error('Результаты не найдены в ответе');
                // Адресная строка: F5 и «поделиться ссылкой» сохранят фильтры.
                history.replaceState(null, '', url);
                if (opts.onUpdated) opts.onUpdated(doc);
            })
            .catch(function (error) {
                if (error && error.name === 'AbortError') return;
                window.location.href = url;
            })
            .finally(function () {
                // Запрос мог быть отменён новым: чужой finally не должен
                // сбрасывать ссылку на актуальный контроллер и индикатор.
                if (inflight !== controller) return;
                inflight = null;
                setBusy(false);
            });
    }

    // Текстовые поля — с паузой (не гоняем запрос на каждую букву).
    formEl.querySelectorAll('input[type="text"], input[type="search"]')
        .forEach(function (el) {
            el.addEventListener('input', function () {
                clearTimeout(timer);
                timer = setTimeout(refresh, debounceMs);
            });
            el.addEventListener('keydown', function (event) {
                if (event.key === 'Enter') {
                    event.preventDefault();
                    refresh();
                }
            });
        });

    // Селекты и даты — сразу.
    formEl.querySelectorAll('select, input[type="date"]')
        .forEach(function (el) {
            el.addEventListener('change', refresh);
        });

    // Кнопка «Показать»/Enter — применить, а не навигировать.
    formEl.addEventListener('submit', function (event) {
        event.preventDefault();
        refresh();
    });
}
