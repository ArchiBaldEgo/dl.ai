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
//
// Удобства поверх фильтров:
// - период-кнопки («Сегодня / 7 / 30 / Всё время») — заполняют даты и
//   сразу обновляют панели;
// - drill-down: клик по названию строки среза ставит его как фильтр
//   (модель / кто / режим / день) и обновляет панели;
// - числа ≥ 1000 группируются неразрывными пробелами (12 345 читается
//   быстрее 12345) — в таблицах и в карточках-итогах.
(function () {
    'use strict';

    var WRAPPER_SEL = '#ai-tokens-panels';
    var TEXT_DEBOUNCE_MS = 350;

    // Локальная (браузерная) дата → «YYYY-MM-DD». toISOString не годится:
    // он в UTC, а день на странице фильтруется по МСК — для аудитории
    // (МСК-часовой пояс) совпадает, но сдвиг на минуту после полуночи в
    // другой зоне не должен ронять пресет.
    function isoLocal(date) {
        var pad = function (n) { return String(n).padStart(2, '0'); };
        return date.getFullYear() + '-' + pad(date.getMonth() + 1) + '-' + pad(date.getDate());
    }

    // Группировка разрядов в готовых ячейках таблиц/карточках. Идемпотентно:
    // после группировки все куски ≤ 3 цифр — повторный прогон по регэкспу
    // «4+ цифры подряд» ничего не меняет. Даты (2026-10-16) не задеваем:
    // их куски короче 4 цифр. Неразрывный пробел: число не рвётся при переносе.
    function groupNumbers(root) {
        root.querySelectorAll(
            '.ai-tokens-num, .ai-tokens-card-value, .ai-tokens-card-sub',
        ).forEach(function (el) {
            if (!/\d{4,}/.test(el.textContent)) return;
            el.textContent = el.textContent.replace(/\d{4,}/g, function (num) {
                return num.replace(/\B(?=(\d{3})+$)/g, ' ');
            });
        });
    }

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
                groupNumbers(fresh);
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

        // Период-кнопки: заполняют поля дат и сразу обновляют панели.
        // data-days="0" — сегодня, "7"/"30" — последние N дней (включая
        // сегодняшний), "" — очистить период («Всё время»).
        var fromInput = form.querySelector('input[name="date_from"]');
        var toInput = form.querySelector('input[name="date_to"]');
        document.querySelectorAll('.ai-tokens-chip').forEach(function (chip) {
            chip.addEventListener('click', function () {
                if (!fromInput || !toInput) return;
                var days = chip.getAttribute('data-days');
                if (days === '') {
                    fromInput.value = '';
                    toInput.value = '';
                } else {
                    var today = new Date();
                    var from = new Date(today);
                    from.setDate(today.getDate() - Number(days));
                    fromInput.value = isoLocal(from);
                    toInput.value = isoLocal(today);
                }
                refreshPanels();
            });
        });

        // Drill-down: клик по названию строки среза → этот срез становится
        // фильтром. Значения собираются с ОСТАЛЬНЫМИ фильтрами (не
        // сбрасывают их) — срез читается в уже выбранном контексте.
        // Делегируем на document: обёртка панелей после каждого refresh
        // заменяется целиком (навешенный на неё listener погиб бы вместе
        // с ней). «—» (пустое значение) фильтром не бывает — игнорируем.
        document.addEventListener('click', function (e) {
            var link = e.target.closest('[data-fill]');
            if (!link || !link.closest(WRAPPER_SEL)) return;
            var value = (link.getAttribute('data-value') || '').trim();
            if (!value || value === '—') return;
            var fill = link.getAttribute('data-fill');
            if (fill === 'day') {
                if (fromInput && toInput) {
                    fromInput.value = value;
                    toInput.value = value;
                }
            } else {
                var field = form.querySelector('[name="' + fill + '"]');
                if (field) field.value = value;
            }
            refreshPanels();
        });

        // Разряды в начальном рендере (сервер отдаёт «сырые» числа).
        groupNumbers(wrapper);
    }

    document.addEventListener('DOMContentLoaded', init);
})();
