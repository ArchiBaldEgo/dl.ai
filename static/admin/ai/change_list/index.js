// Селекторы фильтров changelist: выбор опции = переход на choice.query_string
// (в ней уже сохранены остальные активные фильтры и поиск).
// Вынесено из ai/templates/admin/change_list.html.
(function () {
  var bar = document.getElementById('changelist-filter');
  if (!bar) return;
  bar.addEventListener('change', function (event) {
    var select = event.target.closest('select[data-ai-filter]');
    if (!select) return;
    window.location.href = window.location.pathname + select.value;
  });
})();
