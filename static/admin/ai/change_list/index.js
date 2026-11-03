// Страница changelist'а админки (ai/templates/admin/change_list.html).
//
// 1) Селекторы фильтров: выбор опции = мгновенный переход на
//    choice.query_string (в ней уже сохранены остальные активные фильтры и
//    поиск). Недописанный текст из #searchbar переносится в новую ссылку
//    параметром q, иначе он терялся при смене фильтра.
// 2) Отправка поиска сохраняет ВСЕ параметры текущего URL: стоковая форма
//    рендерит скрытые поля из cl.params, а этот слой — страховка, если
//    какой-то параметр фильтра туда не попал (жалоба «фильтры сбрасываются
//    после „Искать“»).
// 3) Если сервер не пометил ни одну опцию выбранной, но в URL фильтр активен —
//    выбираем опцию по значению параметра (data-ai-filter-param): полоса
//    фильтров никогда не показывает «Все» при активном фильтре.
// 4) Номер страницы списка добавляется к ссылкам «правка объекта»:
//    Django из сохранённых фильтров (_changelist_filters) номер страницы
//    ВЫБРАСЫВАЕТ — после правки объекта оператор оказывался на 1-й странице
//    списка (то есть «выкидывало наверх»). Ссылка с 'p' сохраняет страницу.
(function () {
  // Параметры, которые не являются фильтрами (как IGNORED_PARAMS Django).
  var NON_FILTER_PARAMS = ['q', 'p', 'o', 'e'];

  function typedSearchTerm() {
    var input = document.getElementById('searchbar');
    return input ? (input.value || '').trim() : '';
  }

  function navigationHref(queryString) {
    var search = queryString || '';
    var params = new URLSearchParams(search);
    // Поиск ещё не отправлен — дописываем набранный текст.
    var term = typedSearchTerm();
    if (term) {
      params.set('q', term);
    }
    var tail = params.toString();
    return window.location.pathname + (tail ? '?' + tail : '');
  }

  // 1) Смена фильтра — сразу переходим (полная навигация: стоковый
  //    admin/js/actions.js держит прямые обработчики на #result_list и при
  //    AJAX-подмене таблицы осиротел бы).
  document.addEventListener('change', function (event) {
    var select = event.target.closest('select[data-ai-filter]');
    if (!select) return;
    window.location.href = navigationHref(select.value);
  });

  // 2) Страховка для поиска: доводим в форму все фильтры текущего URL.
  //    Стоковая форма уже несёт их скрытыми полями из cl.params; дубли не
  //    создаём (проверяем по name).
  document.addEventListener('submit', function (event) {
    var form = event.target;
    if (!form.matches || !form.matches('#changelist-search')) return;
    var current = new URLSearchParams(window.location.search);
    var present = {};
    var fields = form.querySelectorAll('input[name]');
    for (var i = 0; i < fields.length; i++) {
      present[fields[i].name] = true;
    }
    current.forEach(function (value, name) {
      if (NON_FILTER_PARAMS.indexOf(name) !== -1) return;
      if (present[name]) return;
      var hidden = document.createElement('input');
      hidden.type = 'hidden';
      hidden.name = name;
      hidden.value = value;
      form.appendChild(hidden);
      present[name] = true;
    });
  });

  // 3) «Полоса фильтров не показывает Все при активном фильтре».
  function syncFilterSelects() {
    var current = new URLSearchParams(window.location.search);
    var selects = document.querySelectorAll('select[data-ai-filter]');
    for (var i = 0; i < selects.length; i++) {
      var select = selects[i];
      if (select.querySelector('option[selected]')) continue;
      var param = select.getAttribute('data-ai-filter-param');
      if (!param) continue;
      var wanted = current.get(param) || '';
      var options = select.querySelectorAll('option');
      for (var j = 0; j < options.length; j++) {
        var optionParams = new URLSearchParams(options[j].value || '');
        var optionValue = optionParams.get(param) || '';
        if (optionValue === wanted) {
          options[j].selected = true;
          break;
        }
      }
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', syncFilterSelects);
  } else {
    syncFilterSelects();
  }

  // 4) Номер текущей страницы списка (в URL он есть только для p>1).
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
