/* Расширение быстрого фильтра левого меню (#nav-filter) на кастомные группы
   AI-инструментов (.ai-nav-group / .ai-nav-item), которые рендерит наш оверрайд
   admin/app_list.html. Штатный nav_sidebar.js фильтрует только строки таблиц
   реальных приложений (th[scope=row] a) — на наши div/ul он не смотрит.

   Контракт тот же: ввод/вставка/Esc на #nav-filter скрывают несовпадающие
   пункты; пустой заголовок группы прячется вместе с опустевшей группой.
   Значение восстанавливается штатным JS из sessionStorage ДО DOMContentLoaded
   (оба скрипта defer, порядок: nav_sidebar.js → base_site.html → этот файл),
   поэтому на старте просто читаем текущее значение input. */
'use strict';
window.addEventListener('DOMContentLoaded', function () {
  var nav = document.getElementById('nav-filter');
  var sidebar = document.getElementById('nav-sidebar');
  if (!nav || !sidebar) { return; }

  function apply() {
    var value = (nav.value || '').trim().toLowerCase();
    var groups = sidebar.querySelectorAll('.ai-nav-group');
    if (!groups.length) { return; }
    groups.forEach(function (group) {
      var anyVisible = false;
      group.querySelectorAll('.ai-nav-item').forEach(function (item) {
        var label = (item.getAttribute('data-filter') || item.textContent || '').trim().toLowerCase();
        var visible = !value || label.indexOf(value) !== -1;
        item.style.display = visible ? '' : 'none';
        if (visible) { anyVisible = true; }
      });
      group.style.display = (!value || anyVisible) ? '' : 'none';
    });
  }

  nav.addEventListener('change', apply);
  nav.addEventListener('input', apply);
  nav.addEventListener('keyup', apply);
  apply();
});