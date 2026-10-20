// Страница «Контроль токенов» (/ai/admin/ai/tokens/).
// Фильтры — обычная GET-форма (страница и так серверная), JS здесь только
// для мелочей: автоотправка при смене селекта и подсказка «сбросить».
(function () {
    'use strict';

    function initAutoSubmit() {
        var form = document.querySelector('.ai-tokens-filters');
        if (!form) return;
        var selects = form.querySelectorAll('select');
        for (var i = 0; i < selects.length; i++) {
            selects[i].addEventListener('change', function () {
                form.submit();
            });
        }
    }

    document.addEventListener('DOMContentLoaded', initAutoSubmit);
})();
