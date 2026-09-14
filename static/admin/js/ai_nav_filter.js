/* Расширение быстрого фильтра левого меню (#nav-filter) на все пункты
   навигации. Наш оверрайд admin/app_list.html рендерит и группы
   AI-инструментов, и реальные приложения («Раздел ИИ») единым списком
   .ai-nav-item — штатный nav_sidebar.js фильтрует только строки таблиц
   (th[scope=row] a), которых в новой разметке нет вовсе: он видит пустой
   набор опций и вешает на поле флага .no-results при любом вводе.

   Поэтому этот скрипт — ЕДИНСТВЕННЫЙ активный фильтр пунктов:
   • ввод/вставка/Esc на #nav-filter скрывают несовпадающие .ai-nav-item
     (по data-filter или тексту), опустевшая группа прячется целиком;
   • флаг .no-results на поле переключаем сами — по наличию видимых
     совпадений среди .ai-nav-item (штатное поведение для поля сохраняем).

   Значение восстанавливается штатным JS из sessionStorage ДО
   DOMContentLoaded (оба скрипта defer, порядок: nav_sidebar.js →
   base_site.html → этот файл), поэтому на старте просто читаем текущее
   значение input. */
'use strict';
window.addEventListener('DOMContentLoaded', function () {
  var nav = document.getElementById('nav-filter');
  var sidebar = document.getElementById('nav-sidebar');
  if (!nav || !sidebar) { return; }

  function apply() {
    var value = (nav.value || '').trim().toLowerCase();
    var groups = sidebar.querySelectorAll('.ai-nav-group');
    if (!groups.length) { return; }
    var anyVisible = false;
    groups.forEach(function (group) {
      var groupHasVisible = false;
      group.querySelectorAll('.ai-nav-item').forEach(function (item) {
        var label = (item.getAttribute('data-filter') || item.textContent || '').trim().toLowerCase();
        var visible = !value || label.indexOf(value) !== -1;
        item.style.display = visible ? '' : 'none';
        if (visible) { groupHasVisible = true; anyVisible = true; }
      });
      group.style.display = (!value || groupHasVisible) ? '' : 'none';
    });
    // Штатный nav_sidebar.js при вводе ставит .no-results на пустом наборе
    // опций — снимаем/ставим сами по фактическим совпадениям.
    nav.classList.toggle('no-results', !!value && !anyVisible);
  }

  nav.addEventListener('change', apply);
  nav.addEventListener('input', apply);
  nav.addEventListener('keyup', apply);
  apply();
});