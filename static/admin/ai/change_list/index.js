// Страница changelist'а админки (ai/templates/admin/change_list.html).
//
// 1) Селекторы фильтров: выбор опции = переход на choice.query_string
//    (в ней уже сохранены остальные активные фильтры и поиск).
// 2) Номер страницы списка добавляется к ссылкам «правка объекта»:
//    Django из сохранённых фильтров (_changelist_filters) номер страницы
//    ВЫБРАСЫВАЕТ — после правки объекта оператор оказывался на 1-й странице
//    списка (то есть «выкидывало наверх»). Ссылка с 'p' сохраняет страницу.
(function () {
  var bar = document.getElementById('changelist-filter');
  if (bar) {
    bar.addEventListener('change', function (event) {
      var select = event.target.closest('select[data-ai-filter]');
      if (!select) return;
      window.location.href = window.location.pathname + select.value;
    });
  }

  // Номер текущей страницы списка (в URL он есть только для p>1).
  var params = new URLSearchParams(window.location.search);
  var page = params.get('p');
  if (!page || page === '1') { return; }
  var links = document.querySelectorAll('#result_list a[href*="/change/"]');
  for (var i = 0; i < links.length; i++) {
    var link = links[i];
    var href = link.getAttribute('href');
    if (!href) { continue; }
    var url;
    try {
      url = new URL(href, window.location.origin);
    } catch (e) {
      continue;
    }
    if (url.searchParams.has('p')) { continue; }
    url.searchParams.set('p', page);
    link.setAttribute('href', url.pathname + url.search + url.hash);
  }
})();
