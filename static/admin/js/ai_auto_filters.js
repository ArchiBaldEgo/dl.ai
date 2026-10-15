// Общий хелпер динамических фильтров админки: форма сама применяет фильтры,
// без кнопки «Фильтровать» — как в журнале запросов, но проще: страницы с
// партиалом-таблицей (журнал) и AJAX-навигацией (changelists) держат СВОИ
// механики; здесь — полная GET-перезагрузка формы (таблица — часть страницы).
//
// Подключать к форме-фильтру: initAiAutoFilters(document.querySelector('.ai-…'));
// Текстовые поля — с паузой (не гонять перезагрузку на каждую букву),
// селекты и даты — мгновенно на change, Enter — мгновенно.
'use strict';

function initAiAutoFilters(formEl, opts) {
    if (!formEl) return;
    var DEBOUNCE_MS = (opts && opts.debounceMs) || 350;
    var timer = null;

    function applyNow() {
        if (timer) { clearTimeout(timer); timer = null; }
        formEl.submit();
    }

    // Текстовые поля: применяем через паузу после набора.
    formEl.querySelectorAll('input[type="text"], input[type="search"]').forEach(function (el) {
        el.addEventListener('input', function () {
            clearTimeout(timer);
            timer = setTimeout(applyNow, DEBOUNCE_MS);
        });
        el.addEventListener('keydown', function (e) {
            if (e.key === 'Enter') { e.preventDefault(); applyNow(); }
        });
    });

    // Селекты и даты: применяем сразу на change.
    formEl.querySelectorAll('select, input[type="date"]').forEach(function (el) {
        el.addEventListener('change', applyNow);
    });

    // Enter в любом месте формы — применить здесь, а не навигировать.
    formEl.addEventListener('submit', function (e) {
        e.preventDefault();
        applyNow();
    });
}