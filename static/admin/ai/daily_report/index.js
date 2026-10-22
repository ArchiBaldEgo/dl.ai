// Логика страницы «Дневной отчёт» (журнал запросов × сегодняшний день).
// Вынесена из ai/templates/admin/ai/daily_report.html.
// Шаблонных переменных здесь нет: все адреса эндпоинтов и тексты — в коде.
// Хелпер initAiAutoFilters (static/admin/js/ai_auto_filters.js) подключается
// отдельным <script> в шаблоне — он должен загрузиться ДО этого файла.
(function () {
  // Фильтры применяются сами (без кнопок): общий хелпер админки.
  initAiAutoFilters(document.querySelector('.ai-daily-filters'));
  // Клик по колонке «Запросов» → развёртка всех запросов студента за
  // сегодня (свёрнутый блок уже в HTML — без запроса к серверу).
  document.querySelectorAll('.ai-daily-count').forEach(function (cell) {
    cell.addEventListener('click', function () {
      var row = cell.closest('.ai-daily-row');
      var detail = row && row.nextElementSibling;
      if (!detail || !detail.classList.contains('ai-daily-detail')) { return; }
      var willOpen = detail.hidden;
      detail.hidden = !willOpen;
      cell.classList.toggle('open', willOpen);
    });
  });
  // Клик по строке запроса: разворачивает на месте информацию запроса —
  // ровно ту же, что в журнале запросов. Тексты запроса/ответа в дневном
  // отчёте не хранятся (страница лёгкая) — подтягиваются один раз по клику
  // через существующий JSON-эндпоинт детального лога (DIP: его же использует
  // блок «Последние запросы» на «Настройке ИИ-приложения»).
  function renderLogInfo(cell, log) {
    cell.textContent = '';
    var meta = document.createElement('div');
    meta.className = 'ai-daily-req-detail-meta';
    var duration = (log.duration_seconds ? Math.round(log.duration_seconds) + ' с' : '—');
    var who = log.sender || '—';
    if (log.external_user_id) { who += ' [' + log.external_user_id + ']'; }
    var pairs = [
      ['ID', String(log.id)],
      ['Статус', log.status_display],
      ['Режим', log.mode_display],
      ['Модель', log.model_names],
      ['Кто', who],
      ['Язык', log.programming_language_name],
      ['Тема', log.topic_name],
      ['Промпт', log.prompt_name],
      ['Задача', log.task_name || log.task_node_id],
      ['Отправлен', log.sent_at],
      ['Получен', log.received_at],
      ['Время ответа', duration],
      ['Токенов', log.tokens],
    ];
    pairs.forEach(function (pair) {
      var item = document.createElement('div');
      var label = document.createElement('strong');
      label.textContent = pair[0] + ': ';
      item.appendChild(label);
      // Промпт — кликабелен, как в журнале запросов (открыть промпт).
      if (pair[0] === 'Промпт' && log.prompt_id) {
        var a = document.createElement('a');
        a.href = '/ai/admin/ai/prompt/' + log.prompt_id + '/change/';
        a.target = '_blank';
        a.rel = 'noopener';
        a.textContent = pair[1] || '—';
        item.appendChild(a);
      } else {
        item.appendChild(document.createTextNode(pair[1] !== null && pair[1] !== undefined ? pair[1] : '—'));
      }
      meta.appendChild(item);
    });
    cell.appendChild(meta);

    // Batch-записи: тексты не отдаём (могут быть десятки МБ) — как и на
    // «Настройке ИИ», отправляем в детальную страницу лога.
    if (log.is_batch) {
      var h4b = document.createElement('h4'); h4b.textContent = 'Результаты прогона';
      cell.appendChild(h4b);
      var ab = document.createElement('a');
      ab.href = log.detail_url;
      ab.textContent = 'Открыть детальную страницу лога →';
      cell.appendChild(ab);
      return;
    }

    var h4q = document.createElement('h4'); h4q.textContent = 'Текст запроса';
    cell.appendChild(h4q);
    var preQ = document.createElement('pre');
    preQ.className = 'ai-daily-req-block';
    preQ.textContent = log.message || '(пусто)';
    cell.appendChild(preQ);

    var h4a = document.createElement('h4'); h4a.textContent = 'Ответ модели';
    cell.appendChild(h4a);
    var preA = document.createElement('pre');
    preA.className = 'ai-daily-req-block';
    preA.textContent = log.response_text || '(пусто)';
    cell.appendChild(preA);

    if (log.error_message) {
      var h4e = document.createElement('h4'); h4e.textContent = 'Ошибка';
      cell.appendChild(h4e);
      var preE = document.createElement('pre');
      preE.className = 'ai-daily-req-block ai-daily-req-error';
      preE.textContent = log.error_message;
      cell.appendChild(preE);
    }
  }

  function parseJsonResponse(response) {
    // При протухшей сессии admin_view редиректит на страницу логина
    // (HTML) — показываем понятное сообщение, а не ошибку JSON.parse.
    return response.text().then(function (text) {
      try { return JSON.parse(text); }
      catch (e) {
        throw new Error('Сессия истекла — обновите страницу и войдите заново.');
      }
    });
  }

  document.querySelectorAll('.ai-daily-req-row').forEach(function (row) {
    row.addEventListener('click', function () {
      var detail = row.nextElementSibling;
      if (detail && detail.classList.contains('ai-daily-req-detail')) {
        detail.hidden = !detail.hidden;  // повторный клик сворачивает
        return;
      }
      detail = document.createElement('tr');
      detail.className = 'ai-daily-req-detail';
      var cell = document.createElement('td'); cell.colSpan = 6;
      cell.textContent = 'Загрузка…';
      detail.appendChild(cell);
      row.parentNode.insertBefore(detail, row.nextSibling);
      fetch('/ai/admin/ai/request_logs/' + row.dataset.logId + '/detail-json/')
        .then(parseJsonResponse)
        .then(function (data) { renderLogInfo(cell, data.log); })
        .catch(function (err) {
          cell.textContent = 'Ошибка: ' + err.message;
        });
    });
  });
})();
