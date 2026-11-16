// Страница changelist'а админки (ai/templates/admin/change_list.html).
//
// Фильтры и поиск применяются БЕЗ перезагрузки страницы: запрос уходит на
// тот же URL, из полного ответа берутся #changelist-filter и #changelist
// (таблица, поиск, пагинация, date hierarchy) и подменяются на месте.
// Адресная строка обновляется через history.pushState — F5 и «назад»
// сохраняют состояние.
//
// Что перехватывается:
//   1) смена любого селектора фильтра (query_string опции уже содержит
//      остальные активные фильтры и поиск);
//   2) отправка поиска (все параметры формы + страховка из текущего URL);
//   3) «Сбросить все фильтры» и ссылки date hierarchy;
//   4) кнопки «назад/вперёд» браузера (popstate).
//
// После подмены таблицы пере-инициализируются массовые действия Django
// (Actions вешает обработчики на конкретный tbody) и состояние фильтров.
// Если AJAX не удался (сессия истекла, сеть) — обычный переход по ссылке.
(function () {
  'use strict';

  // Параметры, которые не являются фильтрами (как IGNORED_PARAMS Django).
  var NON_FILTER_PARAMS = ['q', 'p', 'o', 'e'];
  var inflight = null;

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

  function searchHref(form) {
    var params = new URLSearchParams();
    new FormData(form).forEach(function (value, key) {
      var text = String(value == null ? '' : value);
      if (text.trim() !== '') params.append(key, text);
    });
    // Страховка: фильтры, которых нет в форме (жалоба «фильтры сбрасываются
    // после „Искать“»), переносим из текущего URL.
    var current = new URLSearchParams(window.location.search);
    current.forEach(function (value, name) {
      if (NON_FILTER_PARAMS.indexOf(name) !== -1) return;
      if (params.has(name)) return;
      params.append(name, value);
    });
    params.delete('p');
    var tail = params.toString();
    return window.location.pathname + (tail ? '?' + tail : '');
  }

  function setBusy(busy) {
    document.documentElement.classList.toggle('ai-changelist-busy', busy);
    var filter = document.getElementById('changelist-filter');
    if (filter) filter.setAttribute('aria-busy', busy ? 'true' : 'false');
  }

  function activeFilterParam() {
    var active = document.activeElement;
    if (!active || !active.getAttribute) return '';
    return active.getAttribute('data-ai-filter-param') || '';
  }

  function focusFilter(param) {
    if (!param) return;
    var select = document.querySelector(
      'select[data-ai-filter][data-ai-filter-param="' + param + '"]'
    );
    if (select) select.focus();
  }

  function replaceFilterBar(doc) {
    var fresh = doc.querySelector('#changelist-filter');
    var current = document.querySelector('#changelist-filter');
    if (fresh && current) {
      current.replaceWith(fresh);
      return;
    }
    if (fresh) {
      var list = document.querySelector('#changelist');
      if (list && list.parentNode) list.parentNode.insertBefore(fresh, list);
    } else if (current) {
      current.remove();
    }
  }

  function replaceObjectTools(doc) {
    // У «Добавить» в ссылке сохранены активные фильтры (_changelist_filters) —
    // после AJAX-смены фильтра ссылка должна вести с новыми.
    var fresh = doc.querySelector('.object-tools');
    var current = document.querySelector('.object-tools');
    if (fresh && current) current.replaceWith(fresh);
  }

  function bindActions() {
    if (!window.Actions) return;
    var boxes = document.querySelectorAll('#result_list tr input.action-select');
    if (!boxes.length) return;
    try {
      window.Actions(boxes);
    } catch (e) {
      // Массовые действия не критичны для фильтрации — не ломаем страницу.
    }
  }

  // «Полоса фильтров не показывает Все при активном фильтре».
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

  // Номер страницы списка добавляется к ссылкам «правка объекта»: Django из
  // сохранённых фильтров (_changelist_filters) номер страницы ВЫБРАСЫВАЕТ.
  function restorePageLinks() {
    var params = new URLSearchParams(window.location.search);
    var page = params.get('p');
    if (!page || page === '1') return;
    var links = document.querySelectorAll('#result_list a[href*="/change/"]');
    for (var i = 0; i < links.length; i++) {
      var link = links[i];
      var href = link.getAttribute('href');
      if (!href) continue;
      var url;
      try {
        url = new URL(href, window.location.origin);
      } catch (e) {
        continue;
      }
      if (url.searchParams.has('p')) continue;
      url.searchParams.set('p', page);
      link.setAttribute('href', url.pathname + url.search + url.hash);
    }
  }

  function notifyState() {
    // ai_admin_state.js: сохранить новый querystring для возврата на страницу.
    window.dispatchEvent(new Event('ai:changelist-navigated'));
  }

  function applyPage(doc) {
    var activeParam = activeFilterParam();
    var searchFocused = document.activeElement === document.getElementById('searchbar');
    replaceFilterBar(doc);
    replaceObjectTools(doc);
    var fresh = doc.querySelector('#changelist');
    var current = document.querySelector('#changelist');
    if (!fresh || !current) return false;
    current.replaceWith(fresh);
    bindActions();
    syncFilterSelects();
    restorePageLinks();
    if (searchFocused) {
      // Форма поиска живёт внутри #changelist — вернуть фокус и каретку.
      var input = document.getElementById('searchbar');
      if (input) {
        input.focus();
        var end = input.value.length;
        try { input.setSelectionRange(end, end); } catch (e) { /* не text */ }
      }
    } else {
      focusFilter(activeParam);
    }
    return true;
  }

  // historyMode: 'push' | 'replace' | 'none'.
  function load(url, historyMode) {
    url = url || window.location.href;
    var target;
    try {
      // База — ТЕКУЩИЙ адрес: ссылки пагинации и «показать все» относительные
      // («?p=2»), от одного origin они уехали бы на корень сайта.
      target = new URL(url, window.location.href);
    } catch (e) {
      window.location.href = url;
      return;
    }

    if (inflight) inflight.abort();
    var controller = new AbortController();
    inflight = controller;
    setBusy(true);

    fetch(target.toString(), {
      headers: { 'X-Requested-With': 'XMLHttpRequest' },
      credentials: 'same-origin',
      signal: controller.signal,
    })
      .then(function (response) {
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return response.text().then(function (html) {
          return { html: html, finalUrl: response.url || target.toString() };
        });
      })
      .then(function (result) {
        var doc = new DOMParser().parseFromString(result.html, 'text/html');
        if (!doc.querySelector('#changelist')) {
          // Протухшая сессия отдала страницу логина — обычный переход.
          throw new Error('changelist not found');
        }
        // URL обновляем ДО applyPage: иначе syncFilterSelects/restorePageLinks
        // прочитают ещё старые параметры (и потеряют номер страницы).
        if (historyMode === 'push') {
          history.pushState({ aiChangelist: true }, '', result.finalUrl);
        } else if (historyMode === 'replace') {
          history.replaceState(history.state, '', result.finalUrl);
        }
        if (!applyPage(doc)) throw new Error('changelist not found');
        notifyState();
      })
      .catch(function (error) {
        if (error && error.name === 'AbortError') return;
        window.location.href = target.toString();
      })
      .finally(function () {
        // Отменённый запрос не должен гасить индикатор более нового.
        if (inflight !== controller) return;
        inflight = null;
        setBusy(false);
      });
  }

  // 1) Смена фильтра — AJAX.
  document.addEventListener('change', function (event) {
    var select = event.target.closest('select[data-ai-filter]');
    if (!select) return;
    load(navigationHref(select.value), 'push');
  });

  // 2) Поиск — AJAX, все активные фильтры сохраняются.
  document.addEventListener('submit', function (event) {
    var form = event.target;
    if (!form.matches || !form.matches('#changelist-search')) return;
    event.preventDefault();
    load(searchHref(form), 'push');
  });

  // 3) «Сбросить все фильтры», date hierarchy, «показать все результаты»
  //    (счётчик в тулбаре) и пагинация — тоже без перезагрузки.
  document.addEventListener('click', function (event) {
    var link = event.target.closest(
      '#changelist-filter a[href], #changelist .toplinks a[href],' +
      ' #toolbar a[href], #changelist .paginator a[href]'
    );
    if (!link) return;
    var href = link.getAttribute('href');
    if (!href || href.charAt(0) === '#') return;
    event.preventDefault();
    load(href, 'push');
  });

  // 4) Назад/вперёд: состояние уже в URL, только перерисовываем таблицу.
  window.addEventListener('popstate', function () {
    load(window.location.href, 'none');
  });

  // Возврат на changelist с сохранёнными фильтрами (ai_admin_state.js) —
  // через тот же AJAX-механизм, без второй полной загрузки страницы.
  window.aiChangelistLoad = load;

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', syncFilterSelects);
  } else {
    syncFilterSelects();
  }
  restorePageLinks();
})();
