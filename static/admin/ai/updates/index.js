// Логика страницы «Обновления»: инициализация авто-применяемых фильтров.
// Вынесена из ai/templates/admin/ai/updates.html.
// Хелпер initAiAutoFilters (static/admin/js/ai_auto_filters.js) подключается
// отдельным <script> в шаблоне — он должен загрузиться ДО этого файла.
initAiAutoFilters(document.querySelector('.ai-updates-filter'));
