// Логика страницы «Обновления»: фильтры применяются AJAX'ом без перезагрузки.
// Хелпер initAiAjaxFilters (static/admin/js/ai_ajax_filters.js) подключается
// отдельным <script> в шаблоне — он должен загрузиться ДО этого файла.
// Заменяется только область результатов; форма (с фокусом в поле поиска)
// остаётся на месте.
initAiAjaxFilters(document.querySelector('.ai-updates-filter'), {
    targets: ['#ai-updates-results'],
    textTargets: ['#ai-updates-count'],
});
