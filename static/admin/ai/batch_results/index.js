// Общий JS мини-таблицы результатов batch-solve (ARM).
// Раньше жил инлайном в ai/templates/admin/ai/_ai_batch_results.html.
// Подключается ГЛОБАЛЬНО в ai/templates/admin/base_site.html (блок
// extrahead) — partial включается на четырёх страницах (настройки
// приложения, /arm/solve/, «Закреплённые», деталь записи журнала), и
// window.ArmBatchResults обязан быть определён ДО любых вызывающих
// инлайн-скриптов. Порядок подключения в base_site.html это гарантирует.
//
// Шаблонных переменных здесь нет: все адреса эндпоинтов зашиты в код
// (они одинаковы для всех поверхностей).
window.ArmBatchResults = (function () {
  // Режим фильтра списка «Ответы моделей»: 'all' | 'failed' («Не пройденные»
  // = не решено ИЛИ пропущено). Хранится в localStorage — выбранный режим
  // переживает перезагрузку и одинаков на всех поверхностях (/arm/solve/,
  // развёртка в «Настройка ИИ-приложения», деталь записи журнала).
  var FILTER_STORAGE_KEY = 'ai_arm_batch_filter';
  var filterMode = 'all';
  try {
    filterMode = localStorage.getItem(FILTER_STORAGE_KEY) === 'failed' ? 'failed' : 'all';
  } catch (e) { /* localStorage недоступен — работает дефолт */ }
  var prevRenderedMode = null;   // режим текущей отрисовки списка в DOM
  var allOpenKeys = new Set();   // ручные раскрытия карточек в режиме «Все»
  var lastRender = null;         // аргументы последнего рендера (для re-render переключателем)
  var lastRenderSig = null;      // сигнатура последнего рендера (длина+режим) для скипа

  // ── Ленивые тела результатов (lazy_body + чанковая догрузка) ─────────────
  // Снапшот страницы/поллинга не содержит raw_response/code/short_response/
  // dl_comment/dl_error (большой прогон — десятки МБ текста). Тело каждой
  // записи подтягивается GET-ом при первом раскрытии карточки: raw_response —
  // ЧАНКАМИ по RAW_CHUNK_SIZE символов (обрезанные пары — десятки КБ; остаток
  // догружается кнопкой «Показать ещё»), прочие поля — целиком. Загруженное
  // кэшируется в памяти — перерисовки и переключения фильтров не дёргают
  // сервер повторно.
  var RAW_CHUNK_SIZE = 25000;    // символов raw_response за один запрос
  var bodyCache = {};            // result_id -> Promise<state> | state
  function parseJsonResponse(response) {
    // При протухшей сессии admin_view редиректит на страницу логина (HTML) —
    // fetch молча следует за 302, и response.json() даёт бессмысленный
    // «Unexpected token '<'» вместо понятного сообщения (тот же контракт,
    // что в arm_solve.html / daily_report.html).
    return response.text().then(function (text) {
      try { return JSON.parse(text); }
      catch (e) {
        throw new Error('Сессия истекла — обновите страницу и войдите заново.');
      }
    });
  }
  function fetchResultChunk(resultId, offset) {
    var url = '/ai/admin/arm/solve/result/' + encodeURIComponent(resultId) + '/body/'
      + '?offset=' + encodeURIComponent(offset) + '&length=' + RAW_CHUNK_SIZE;
    return fetch(url, {
      method: 'GET', credentials: 'same-origin', headers: { 'X-Requested-With': 'XMLHttpRequest' },
    })
      .then(parseJsonResponse)
      .then(function (data) {
        if (!data || !data.ok || !data.result) { throw new Error('Тело результата недоступно'); }
        return data.result;
      });
  }
  // Загрузить (и закрепить в кэше) состояние тела: первый чанк raw_response —
  // сразу, остальное догружается позже (state.has_more). В кэше может лежать
  // и обещание, и разрешённое состояние — Promise.resolve нормализует оба:
  // у вызывающего всегда есть .then/.catch (иначе после разрешения кэша
  // возвращался голый объект → «.then is not a function», что обрывало
  // поллинг и ре-рендер).
  function loadResultBodyState(resultId) {
    if (!resultId) { return Promise.reject(new Error('Нет result_id')); }
    if (bodyCache[resultId]) { return Promise.resolve(bodyCache[resultId]); }
    var promise = fetchResultChunk(resultId, 0).then(function (b) {
      var state = {
        raw: b.raw_response || '',
        total: (b.raw_total != null) ? Number(b.raw_total) : (b.raw_response || '').length,
        has_more: !!b.raw_has_more,
        code: b.code || '',
        short_response: b.short_response || '',
        dl_comment: b.dl_comment || '',
        dl_error: b.dl_error || '',
        dl_queue_id: b.dl_queue_id || 0,
        delta_id: b.delta_id || 0,
        delta_name: b.delta_name || '',
        truncated: !!b.truncated,
      };
      bodyCache[resultId] = state;
      return state;
    });
    bodyCache[resultId] = promise;
    // Не оставляем отказ в кэше: следующий клик повторит попытку.
    promise.catch(function () { delete bodyCache[resultId]; });
    return promise;
  }

  function escapeHtml(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#039;');
  }
  function verdictBadge(v) {
    var labels = { solved: 'Решено', failed: 'Не решено', skipped: 'Пропущено' };
    var cls = v === 'solved' ? 'solved' : (v === 'failed' ? 'failed' : 'skipped');
    return '<span class="arm-badge ' + cls + '">' + escapeHtml(labels[v] || v) + '</span>';
  }
  // Бейдж «обрезан»: ответ упёрся в потолок генерации провайдера (Ollama
  // num_predict / SambaNova max_tokens) — стрим остановлен ровно на лимите,
  // часть текста (и возможно кода) потеряна (см. model_clients.is_output_truncated).
  function truncatedBadge() {
    return '<span class="arm-badge truncated" title="Ответ обрезан потолком генерации — модель не успела дописать текст">✂ обрезан</span>';
  }
  // Машина (Дельта), на которой DL проверял решение: deltaId/deltaName из
  // get-solution-result. Пусто — машина не назначена (или проверка не шла).
  function deltaLabel(deltaId, deltaName) {
    if (!deltaId && !deltaName) { return ''; }
    var text = deltaName ? escapeHtml(deltaName) : ('#' + escapeHtml(deltaId));
    if (deltaId && deltaName) { text += ' (id ' + escapeHtml(deltaId) + ')'; }
    return ' <span style="color:#6b7280;">· машина: ' + text + '</span>';
  }

  function isFailedRow(r) {
    // «Не пройденные»: не решено (failed) или пропущено (skipped).
    return !!r && r.verdict !== 'solved';
  }

  // Бейдж причины сбоя («сбой ответа модели» / «не выкушен код» /
  // «ошибка компиляции» / «неправильный ответ»). Подпись приходит с
  // сервера (AIModelTestResult.FAILURE_REASON_LABELS) — клиент её не
  // дублирует. У решённых пар причины нет.
  function failureReasonBadge(reason, label) {
    if (!reason) { return ''; }
    var text = label || reason;
    return '<span class="arm-badge arm-reason" title="Причина сбоя">' + escapeHtml(text) + '</span>';
  }

  function renderBucketTable(rows, labelHeader, wrapEl) {
    var prev = wrapEl.querySelector('table.arm-summary-table, .arm-bucket-empty');
    if (prev) { prev.remove(); }
    if (!rows || !rows.length) {
      var empty = document.createElement('p');
      empty.className = 'arm-section-note arm-bucket-empty';
      empty.textContent = 'нет данных';
      wrapEl.appendChild(empty);
      return;
    }
    var head = '<thead><tr><th>' + escapeHtml(labelHeader) + '</th><th class="arm-col-num">% решённых</th><th class="arm-col-num">Среднее время, сек</th><th class="arm-col-num">Токены</th><th class="arm-col-num">Решено / Всего</th></tr></thead>';
    var body = rows.map(function (r) {
      return '<tr><td>' + escapeHtml(r.label) + '</td>'
        + '<td class="arm-col-num">' + escapeHtml(r.percent_solved) + '</td>'
        + '<td class="arm-col-num">' + escapeHtml(r.avg_duration) + '</td>'
        + '<td class="arm-col-num">' + escapeHtml(r.tokens) + '</td>'
        + '<td class="arm-col-num">' + escapeHtml(r.solved) + ' / ' + escapeHtml(r.total) + '</td></tr>';
    }).join('');
    wrapEl.insertAdjacentHTML('beforeend', '<table class="arm-summary-table">' + head + '<tbody>' + body + '</tbody></table>');
  }

  // Компактные подписи отчёта: время и объём прогона (report.run_meta).
  function fmtTs(ts, sameDayAsTs) {
    if (!ts) { return ''; }
    const d = new Date(ts * 1000);
    // Финиш в тот же день, что и старт: время без даты («15:17»);
    // в другой день дата повторяется («15.10, 02:07»).
    if (sameDayAsTs != null) {
      const other = new Date(sameDayAsTs * 1000);
      if (d.toDateString() === other.toDateString()) {
        return d.toLocaleString('ru-RU', { hour: '2-digit', minute: '2-digit' });
      }
    }
    return d.toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
  }
  function fmtDuration(sec) {
    sec = Math.round(Number(sec) || 0);
    if (!(sec > 0)) { return ''; }
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = sec % 60;
    if (h) { return h + ' ч ' + m + ' мин'; }
    if (m) { return m + ' мин' + (s ? ' ' + s + ' с' : ''); }
    return s + ' с';
  }
  // Русские формы множественного числа: pluralRu(43, 'задача', 'задачи', 'задач')
  function pluralRu(n, one, few, many) {
    const abs = Math.abs(Number(n) || 0) % 100;
    const d = abs % 10;
    if (abs > 10 && abs < 20) { return many; }
    if (d > 1 && d < 5) { return few; }
    if (d === 1) { return one; }
    return many;
  }
  // «Прогон: 7 мин 20 с (14.10, 15:10 → 15:17)»; для идущего — «Прогон: 5 мин (с 14.10, 15:10)».
  function runMetaLine(report) {
    const meta = report.run_meta || {};
    if (!meta.started_at_ts) { return ''; }
    const started = fmtTs(meta.started_at_ts);
    const duration = fmtDuration(meta.duration_seconds);
    if (meta.finished_at_ts) {
      const range = started + ' → ' + fmtTs(meta.finished_at_ts, meta.started_at_ts);
      return duration ? duration + ' (' + range + ')' : range;
    }
    return duration ? duration + ' (с ' + started + ')' : 'с ' + started;
  }
  // «Объём: 43 задачи (1 модель)»; пара счётчиков падает на «Всего пар» выше.
  function runVolumeLine(report) {
    if (report.models_count == null && report.tasks_count == null) { return ''; }
    let line = '';
    if (report.tasks_count != null) {
      line = report.tasks_count + ' ' + pluralRu(report.tasks_count, 'задача', 'задачи', 'задач');
    } else if (report.models_count != null) {
      line = report.models_count + ' ' + pluralRu(report.models_count, 'модель', 'модели', 'моделей');
    } else {
      line = report.total_pairs + ' ' + pluralRu(report.total_pairs, 'пара', 'пары', 'пар');
    }
    if (report.models_count != null && report.tasks_count != null) {
      line += ' (' + report.models_count + ' ' + pluralRu(report.models_count, 'модель', 'модели', 'моделей') + ')';
    }
    return line;
  }

  // els = {card, list, perModel, perTopic, errors, reasons, log}
  function renderReport(report, els) {
    // Скип перерисовки отчёта: во время живого прогона поллинг приносит
    // те же цифры — пересобирать таблицы «Итог по моделям/темам» каждые
    // 2 с незачем. Сигнатура — счётчики отчёта + состав секций.
    var reportSig = report ? [
      report.total_pairs, report.solved, report.failed, report.skipped,
      report.tokens_total, report.truncated_pairs,
      (report.per_model || []).length, (report.per_topic || []).length,
      (report.errors_by_topic || []).length, (report.failure_reasons || []).length,
      (report.hints || []).length,
      (report.run_topic || {}).name || '',
      (report.log_lines || []).length,
      report.interim ? 1 : 0,
    ].join(':') : 'none';
    if (els._sig === reportSig && els.card && els.card.style.display !== 'none') {
      return;
    }
    els._sig = reportSig;
    if (!report) {
      if (els.card) { els.card.style.display = 'none'; }
      if (els.list) { els.list.innerHTML = ''; }
      if (els.perModel) { els.perModel.innerHTML = ''; }
      if (els.perTopic) { els.perTopic.innerHTML = ''; }
      if (els.errors) { els.errors.innerHTML = ''; }
      if (els.reasons) { els.reasons.innerHTML = ''; }
      if (els.log) { els.log.innerHTML = ''; }
      setInterimNote(els.card, false);
      return;
    }
    setInterimNote(els.card, !!report.interim);
    if (els.list) {
      const lines = [
        'Всего пар: ' + escapeHtml(report.total_pairs),
        'Решено: ' + escapeHtml(report.solved),
        'Не решено: ' + escapeHtml(report.failed),
        'Пропущено: ' + escapeHtml(report.skipped),
        'Токенов: ' + escapeHtml(report.tokens_total),
      ];
      if (Number(report.truncated_pairs) > 0) {
        lines.push('Обрезано лимитом генерации: ' + escapeHtml(report.truncated_pairs));
      }
      const timeLine = runMetaLine(report);
      if (timeLine) { lines.push('<strong>Прогон:</strong> ' + escapeHtml(timeLine)); }
      // Тема прогона — запасная тема для задач, у которых свою определить не
      // удалось (см. ensure_task → fallback_topic_id). Показываем, чтобы
      // строки отчёта с этой темой не читались как «своя тема задачи».
      if (report.run_topic && report.run_topic.name) {
        lines.push('<strong>Тема прогона:</strong> ' + escapeHtml(report.run_topic.name)
          + ' — пары без собственной темы наследуют её');
      }
      const volumeLine = runVolumeLine(report);
      if (volumeLine) { lines.push('<strong>Объём:</strong> ' + escapeHtml(volumeLine)); }
      els.list.innerHTML = lines.map(function (line) { return '<li>' + line + '</li>'; }).join('');
    }
    if (els.perModel) {
      els.perModel.innerHTML = '<h3>Итог по моделям</h3><p class="arm-section-note">Сортировка: процент решённых ↓, среднее время ответа ↑</p>';
      renderBucketTable(report.per_model, 'Модель', els.perModel);
    }
    var hasRealTopics = (report.per_topic || []).some(function (r) { return r.label && r.label !== 'Без темы'; });
    if (els.perTopic) {
      if (hasRealTopics) {
        els.perTopic.innerHTML = '<h3>Итог по темам</h3>';
        renderBucketTable(report.per_topic, 'Тема', els.perTopic);
      } else {
        els.perTopic.innerHTML = '';
      }
    }
    renderErrorsByTopic(report.errors_by_topic || [], els.errors);
    renderFailureReasons(report.failure_reasons || [], els.reasons);
    renderRunLog(report.log_lines || [], els.log);
    if (els.card) { els.card.style.display = 'block'; }
  }

  // «Сбои по причинам» — таксономия сбоев прогона (сбой ответа модели /
  // не выкушен код / ошибка компиляции / неправильный ответ + честные
  // «причина не распознана» / «пропущено»). Причины приходят с сервера
  // (arm_runner::_build_failure_reasons) — подписи и проценты не дублируются
  // на клиенте.
  function renderFailureReasons(rows, wrapEl) {
    if (!wrapEl) { return; }
    if (!rows || !rows.length) { wrapEl.innerHTML = ''; return; }
    const body = rows.map(function (row) {
      return '<tr><td>' + escapeHtml(row.label) + '</td>'
        + '<td class="arm-col-num">' + escapeHtml(row.count) + '</td>'
        + '<td class="arm-col-num">' + escapeHtml(row.percent) + '%</td></tr>';
    });
    wrapEl.innerHTML = '<h3>Сбои по причинам</h3>'
      + '<table class="arm-summary-table"><thead><tr><th>Причина</th>'
      + '<th class="arm-col-num">Пар</th><th class="arm-col-num">%</th></tr></thead>'
      + '<tbody>' + body.join('') + '</tbody></table>';
  }

  // «Журнал прогона»: события прогона (сбой ответа модели → повтор).
  // Сервер присылает готовые строки (arm_runner::_append_run_log) — здесь
  // только форматируем время.
  function renderRunLog(lines, wrapEl) {
    if (!wrapEl) { return; }
    if (!lines || !lines.length) { wrapEl.innerHTML = ''; return; }
    const body = lines.map(function (line) {
      const ts = line && line.ts ? formatLogTime(line.ts) : '';
      const text = escapeHtml((line && line.text) || '');
      return '<li>' + (ts ? '<span class="arm-log-time">' + escapeHtml(ts) + '</span> ' : '') + text + '</li>';
    });
    wrapEl.innerHTML = '<h3>Журнал прогона</h3>'
      + '<ul class="arm-run-log">' + body.join('') + '</ul>';
  }

  // Время строки журнала прогона — московское (как в журнале запросов).
  function formatLogTime(ts) {
    const d = new Date((Number(ts) || 0) * 1000);
    if (isNaN(d.getTime())) { return ''; }
    const pad = function (n) { return (n < 10 ? '0' : '') + n; };
    return pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
  }

  // «Ошибки по темам»: нерешённые пары (failed/skipped), компактно — до 6
  // позиций на тему (сервер режет, остаток приходит в hidden).
  // Плашка «цифры промежуточные»: ставится, пока report.interim (сервер
  // строит «Сводку» на лету из уже завершённых пар; финал придёт без флага).
  function setInterimNote(card, show) {
    if (!card) { return; }
    var note = card.querySelector('.arm-report-interim-note');
    if (!show) { if (note) { note.remove(); } return; }
    if (note) { return; }
    note = document.createElement('p');
    note.className = 'arm-report-interim-note';
    note.textContent = 'Предварительный отчёт — прогон ещё выполняется, цифры промежуточные.';
    var head = card.querySelector('.arm-section-head');
    if (head && head.nextSibling) { card.insertBefore(note, head.nextSibling); }
    else { card.appendChild(note); }
  }
  function renderErrorsByTopic(rows, wrapEl) {
    if (!wrapEl) { return; }
    if (!rows || !rows.length) { wrapEl.innerHTML = ''; return; }
    const body = rows.map(function (row) {
      const items = (row.items || []).map(function (x) { return escapeHtml(x); }).join(', ')
        + (row.hidden > 0 ? ' <span class="arm-section-note">+ ещё ' + escapeHtml(row.hidden) + '</span>' : '');
      // Причины сбоя внутри темы (компиляция / неверный ответ / нет кода /
      // сбой модели) — считает сервер (arm_runner::_build_batch_report).
      const reasons = (row.reasons || []).map(function (r) {
        return escapeHtml(r.label) + ' — ' + escapeHtml(r.count);
      }).join(', ');
      return '<tr><td>' + escapeHtml(row.topic) + '</td>'
        + '<td class="arm-col-num">' + escapeHtml(row.count) + '</td>'
        + '<td>' + reasons + '</td>'
        + '<td>' + items + '</td></tr>';
    });
    wrapEl.innerHTML = '<h3>Ошибки по темам</h3>'
      + '<table class="arm-summary-table"><thead><tr><th>Тема</th>'
      + '<th class="arm-col-num">Не решено</th><th>Причины</th><th>Что не решено</th></tr></thead>'
      + '<tbody>' + body.join('') + '</tbody></table>';
  }

  function collectOpenKeys(resultsEl) {
    var keys = new Set();
    if (resultsEl) {
      resultsEl.querySelectorAll('details[open]').forEach(function (d) {
        var key = d.getAttribute('data-key');
        if (key) { keys.add(key); }
      });
    }
    return keys;
  }

  function setFilterMode(mode) {
    if (mode !== 'all' && mode !== 'failed') { return; }
    filterMode = mode;
    try { localStorage.setItem(FILTER_STORAGE_KEY, mode); } catch (e) { /* не критично */ }
    if (lastRender) {
      renderResults(lastRender.results, lastRender.resultsEl, lastRender.resultsCard, lastRender.filterHost);
    }
  }

  // Переключатель «Все (N)» / «Не пройденные (M)» — создаётся один раз в
  // filterHost (шапка карточки «Ответы моделей»); при M = 0 кнопка неактивна.
  function ensureFilterToggle(filterHost, total, failedCount) {
    if (!filterHost) { return null; }
    var toggle = filterHost.querySelector('.arm-filter-toggle');
    if (!toggle) {
      toggle = document.createElement('div');
      toggle.className = 'arm-filter-toggle';
      var btnAll = document.createElement('button');
      btnAll.type = 'button';
      btnAll.className = 'arm-button arm-filter-all';
      var btnFailed = document.createElement('button');
      btnFailed.type = 'button';
      btnFailed.className = 'arm-button arm-filter-failed';
      toggle.appendChild(btnAll);
      toggle.appendChild(btnFailed);
      filterHost.appendChild(toggle);
      btnAll.addEventListener('click', function () { setFilterMode('all'); });
      btnFailed.addEventListener('click', function () {
        if (!btnFailed.disabled) { setFilterMode('failed'); }
      });
    }
    var btnAll = toggle.querySelector('.arm-filter-all');
    var btnFailed = toggle.querySelector('.arm-filter-failed');
    btnAll.textContent = 'Все (' + total + ')';
    btnFailed.textContent = 'Не пройденные (' + failedCount + ')';
    btnFailed.disabled = failedCount === 0;
    btnAll.classList.toggle('active', filterMode === 'all');
    btnFailed.classList.toggle('active', filterMode === 'failed');
    btnAll.setAttribute('aria-pressed', filterMode === 'all' ? 'true' : 'false');
    btnFailed.setAttribute('aria-pressed', filterMode === 'failed' ? 'true' : 'false');
    return toggle;
  }

  // renderResults(results, resultsEl, resultsCard, filterHost): рендер списка
  // «Ответы моделей» с учётом filterMode. filterHost — контейнер для
  // переключателя (шапка карточки); без него список рисуется целиком.
  // Раскрытие карточек: ручные открытия сохраняются при перерисовке
  // (и переключении фильтров), но по умолчанию ВСЕ карточки свёрнуты —
  // раскрываются только кликом.
  function renderResults(results, resultsEl, resultsCard, filterHost) {
    lastRender = { results: results, resultsEl: resultsEl, resultsCard: resultsCard, filterHost: filterHost };
    results = results || [];
    var failedRows = results.filter(isFailedRow);
    // Режим «Не пройденные» без не пройденных — пустой список за неактивной
    // кнопкой бесполезен: показываем «Все».
    if (filterMode === 'failed' && !failedRows.length) { filterMode = 'all'; }
    ensureFilterToggle(filterHost, results.length, failedRows.length);

    var visible = filterMode === 'failed' ? failedRows : results;
    if (resultsCard) { resultsCard.style.display = results.length ? 'block' : 'none'; }

    // Скип перерисовки: во время живого прогона поллинг каждые 2с приносит
    // новый массив с тем же количеством записей (ничего не завершилось с
    // прошлого тика) — пересобирать тысячу DOM-узлов бессмысленно.
    // В сигнатуру входит число пар с ПРИЧИНОЙ сбоя: вердикт и причина —
    // разные оси, и пара может сменить причину, не сменив вердикт.
    // Плюс — ХЕШ пар (задача×модель→вердикт): пара может перейти
    // failed→solved (число записей то же), и без этого отчёт остался бы
    // с прошлым вердиктом. Хеш — числовой, пересчёт O(n) на тик.
    var reasonsCount = 0;
    var verdictHash = 0;
    for (var ri = 0; ri < results.length; ri++) {
      var row = results[ri];
      if (!row) { continue; }
      if (row.failure_reason) { reasonsCount++; }
      var pairKey = String(row.task_node_id || '') + '|' + String(row.model_key || row.model_title || '') + '|' + String(row.verdict || '');
      for (var pi = 0; pi < pairKey.length; pi++) {
        verdictHash = ((verdictHash * 31) + pairKey.charCodeAt(pi)) | 0;
      }
    }
    var renderSig = results.length + ':' + visible.length + ':' + filterMode + ':' + (resultsEl ? resultsEl.id || 'noid' : '') + ':' + reasonsCount + ':' + verdictHash;
    if (renderSig === lastRenderSig && resultsEl && resultsEl.childElementCount) { return; }
    lastRenderSig = renderSig;

    // Раскрытие (см. докстринг): domOpen читается ДО очистки DOM.
    var domOpen = collectOpenKeys(resultsEl);
    var openKeys = new Set();
    if (filterMode === 'all') {
      if (prevRenderedMode === 'all') { allOpenKeys = domOpen; }
      openKeys = allOpenKeys;
    } else {
      openKeys = domOpen; // внутри режима — ручные сворачивания сохраняются
    }
    prevRenderedMode = filterMode;

    if (resultsEl) { resultsEl.innerHTML = ''; }
    if (!visible.length) {
      if (resultsEl) {
        resultsEl.innerHTML = '<p class="arm-section-note">'
          + (filterMode === 'failed' ? 'Не пройденных результатов нет.' : 'нет данных')
          + '</p>';
      }
      return;
    }

    var downloadBase = '/ai/admin/arm/solve/result/';
    // Группировка «модель → её задачи»: первым уровнем показываются модели
    // (раскрытие — список задач с вердиктами и полными ответами), внутри —
    // прежние карточки задач. Группируются только видимые строки, поэтому в
    // режиме «Не пройденные» модели без провальных пар не выводятся вовсе.
    var groups = [];
    var byKey = {};
    visible.forEach(function (r) {
      var gk = r.model_key || r.model_title || '—';
      if (!byKey[gk]) {
        byKey[gk] = { key: gk, title: r.model_title || r.model_key || '—', tasks: [] };
        groups.push(byKey[gk]);
      }
      byKey[gk].tasks.push(r);
    });
    groups.sort(function (a, b) { return String(a.title).localeCompare(String(b.title), 'ru'); });

    groups.forEach(function (group) {
      var solvedCount = group.tasks.filter(function (t) { return t.verdict === 'solved'; }).length;
      var totalTokens = group.tasks.reduce(function (s, t) { return s + (t.tokens || 0); }, 0);
      var withTime = group.tasks.filter(function (t) { return t.duration != null; });
      var avgTime = withTime.length
        ? (withTime.reduce(function (s, t) { return s + (t.duration || 0); }, 0) / withTime.length).toFixed(1)
        : '';

      var modelCard = document.createElement('details');
      // Кромка группы — итог модели: зелёная (всё решено), красная (ни одной),
      // серая (частично).
      modelCard.className = 'arm-model-group '
        + (solvedCount === group.tasks.length ? 'all-solved'
          : (solvedCount === 0 ? 'none-solved' : 'part-solved'));
      var mkey = 'm:' + group.key;
      modelCard.setAttribute('data-key', mkey);
      if (openKeys.has(mkey)) { modelCard.setAttribute('open', ''); }

      var mSummary = document.createElement('summary');
      var mHead = document.createElement('div');
      mHead.className = 'arm-model-head';
      var mTitle = document.createElement('span');
      mTitle.className = 'arm-model-title';
      mTitle.innerHTML = '<span class="arm-model-arrow">▸</span>' + escapeHtml(group.title);
      mHead.appendChild(mTitle);
      var mMeta = document.createElement('div');
      mMeta.className = 'arm-model-meta';
      mMeta.textContent = 'Решено: ' + solvedCount + ' из ' + group.tasks.length
        + (avgTime ? ' · среднее время: ' + avgTime + ' сек' : '')
        + (totalTokens ? ' · токенов: ' + totalTokens : '');
      mSummary.appendChild(mHead);
      mSummary.appendChild(mMeta);
      modelCard.appendChild(mSummary);

      var tasksEl = document.createElement('div');
      tasksEl.className = 'arm-model-tasks';
      group.tasks.forEach(function (r) {
        var cls = r.verdict === 'solved' ? 'solved' : (r.verdict === 'failed' ? 'failed' : 'skipped');
        var card = document.createElement('details');
        card.className = 'arm-result ' + cls;
        var rkey = (r.task_node_id || '') + ':' + (r.model_key || '');
        card.setAttribute('data-key', rkey);
        if (openKeys.has(rkey)) { card.setAttribute('open', ''); }

        var lazy = !!r.lazy_body;               // тело подтягивается по AJAX
        var dlComment = r.dl_comment || '';
        var dlError = r.dl_error || '';
        var dlQueueId = r.dl_queue_id || 0;
        // Машина (Дельта), на которой DL проверял решение: deltaId/deltaName.
        var deltaId = r.delta_id || 0;
        var deltaName = r.delta_name || '';
        var code = r.code || '';
        var resultId = r.result_id || '';

        var summary = document.createElement('summary');
        var top = document.createElement('div');
        top.className = 'arm-result-top';
        var title = document.createElement('span');
        title.className = 'arm-result-title';
        // Модель уже в шапке группы — в заголовке задачи только её имя.
        // Есть dl_url (dl_task_url на сервере) — номер и имя задачи ведут в DL.
        if (r.dl_url) {
          var link = document.createElement('a');
          link.href = r.dl_url;
          link.target = '_blank';
          link.rel = 'noopener';
          link.title = 'Открыть задачу в DL';
          link.className = 'arm-result-task-link';
          link.textContent = '#' + (r.task_node_id || '') + ' ' + (r.task_name || '');
          title.appendChild(link);
        } else {
          title.textContent = '#' + (r.task_node_id || '') + ' ' + (r.task_name || '');
        }
        top.appendChild(title);
        // Бейджи — ОДНИМ контейнером .arm-result-badges. Раньше каждый бейдж
        // был отдельным flex-элементом .arm-result-top (justify-content:
        // space-between) → «Не решено» улетало влево, причина — вправо,
        // «обрезан» — ещё дальше: строка «прыгала». Теперь все бейджи стоят
        // рядом, в одном столбике у правого края.
        var badges = document.createElement('span');
        badges.className = 'arm-result-badges';
        badges.insertAdjacentHTML('beforeend', verdictBadge(r.verdict));
        if (r.failure_reason) { badges.insertAdjacentHTML('beforeend', failureReasonBadge(r.failure_reason, r.failure_reason_label)); }
        if (r.truncated) { badges.insertAdjacentHTML('beforeend', truncatedBadge()); }
        top.appendChild(badges);
        var meta = document.createElement('div');
        meta.className = 'arm-meta';
        meta.textContent = (r.duration != null ? r.duration : '') + ' сек | ' + (r.tokens != null ? r.tokens : '') + ' ток.';
        summary.appendChild(top);
        summary.appendChild(meta);

        var body = document.createElement('div');
        body.className = 'arm-result-body';

        var dlBlock = document.createElement('div');
        dlBlock.style.cssText = 'padding:8px 12px; margin-bottom:8px; border-radius:4px; font-size:13px; color:#1f2937; background:' + (r.verdict === 'solved' ? '#dcfce7' : '#fee2e2') + ';';
        var dlHtml = '<strong>DL тест:</strong> ';
        if (dlComment) { dlHtml += escapeHtml(dlComment); } else { dlHtml += '<span style="color:#6b7280;">итог тестов отсутствует</span>'; }
        if (dlQueueId) { dlHtml += ' <span style="color:#6b7280;">· queueId: ' + escapeHtml(dlQueueId) + '</span>'; }
        dlHtml += deltaLabel(deltaId, deltaName);
        if (r.saved_solution) { dlHtml += ' <span style="color:#166534;">· ✓ сохранено в «Решённые задачи»</span>'; }
        if (dlError) { dlHtml += '<div style="margin-top:4px; color:#991b1b;"><strong>Ошибка DL:</strong> ' + escapeHtml(dlError) + '</div>'; }
        dlBlock.innerHTML = dlHtml;
        body.appendChild(dlBlock);

        var answerLabel = document.createElement('div');
        answerLabel.style.cssText = 'margin:8px 0 4px; font-weight:600; font-size:13px;';
        answerLabel.textContent = 'Полный ответ модели:';
        body.appendChild(answerLabel);
        var answerPre = document.createElement('pre');
        body.appendChild(answerPre);

        var codeLabel = document.createElement('div');
        codeLabel.style.cssText = 'margin:10px 0 4px; font-weight:600; font-size:13px;';
        codeLabel.textContent = 'Извлечённый код программы:';
        body.appendChild(codeLabel);
        var codePre = document.createElement('pre');
        body.appendChild(codePre);
        var downloadLink = null;
        if (resultId) {
          downloadLink = document.createElement('a');
          downloadLink.href = downloadBase + encodeURIComponent(resultId) + '/download/';
          downloadLink.className = 'arm-button';
          downloadLink.style.cssText = 'display:inline-block; margin-top:6px;';
          downloadLink.textContent = 'Скачать код';
          body.appendChild(downloadLink);
        }

        if (lazy) {
          // Тело ещё не в снапшоте: плейсхолдеры + подгрузка при раскрытии.
          // Содержимое dl-блока тоже приходит из body-эндпоинта.
          dlBlock.innerHTML = '<strong>DL тест:</strong> <span style="color:#6b7280;">' +
            (card.open ? 'загрузка…' : 'раскройте строку для загрузки') + '</span>';
          answerPre.textContent = '';
          codePre.textContent = '';
          if (downloadLink) { downloadLink.style.display = 'none'; }
          var bodyLoaded = false;
          var moreBtn = null;
          var state = null;
          var fillBody = function (st) {
            state = st;
            dlComment = st.dl_comment || '';
            dlError = st.dl_error || '';
            dlQueueId = st.dl_queue_id || dlQueueId;
            deltaId = st.delta_id || deltaId;
            deltaName = st.delta_name || deltaName;
            var dlHtml2 = '<strong>DL тест:</strong> ';
            if (dlComment) { dlHtml2 += escapeHtml(dlComment); } else { dlHtml2 += '<span style="color:#6b7280;">итог тестов отсутствует</span>'; }
            if (dlQueueId) { dlHtml2 += ' <span style="color:#6b7280;">· queueId: ' + escapeHtml(dlQueueId) + '</span>'; }
            dlHtml2 += deltaLabel(deltaId, deltaName);
            if (r.saved_solution) { dlHtml2 += ' <span style="color:#166534;">· ✓ сохранено в «Решённые задачи»</span>'; }
            if (dlError) { dlHtml2 += '<div style="margin-top:4px; color:#991b1b;"><strong>Ошибка DL:</strong> ' + escapeHtml(dlError) + '</div>'; }
            dlBlock.innerHTML = dlHtml2;
            answerPre.textContent = st.raw || st.short_response || '';
            codePre.textContent = st.code || '— код не извлечён —';
            if (downloadLink) { downloadLink.style.display = st.code ? '' : 'none'; }
            // Бейдж «обрезан», если в снапшоте его не было (легаси-строки БД).
            if (st.truncated && !top.querySelector('.arm-badge.truncated')) {
              top.insertAdjacentHTML('beforeend', truncatedBadge());
            }
            if (moreBtn) { moreBtn.remove(); moreBtn = null; }
            if (st.has_more) {
              // Чанковая догрузка: остаток raw_response — по кнопке.
              var remainingKb = Math.max(1, Math.round((st.total - st.raw.length) / 1024));
              moreBtn = document.createElement('button');
              moreBtn.type = 'button';
              moreBtn.className = 'arm-button arm-body-more';
              moreBtn.textContent = 'Показать ещё (' + remainingKb + ' КБ)';
              moreBtn.addEventListener('click', function () {
                if (!state || !state.has_more) { return; }
                var loaded = state.raw.length;
                moreBtn.disabled = true;
                moreBtn.textContent = 'загрузка…';
                fetchResultChunk(resultId, loaded).then(function (b) {
                  state.raw += b.raw_response || '';
                  state.has_more = !!b.raw_has_more;
                  if (b.code && !state.code) { state.code = b.code; }
                  state.truncated = state.truncated || !!b.truncated;
                  fillBody(state);
                }).catch(function () {
                  moreBtn.disabled = false;
                  moreBtn.textContent = 'Ошибка загрузки — повторить';
                });
              });
              answerPre.insertAdjacentElement('afterend', moreBtn);
            }
          };
          var loadBody = function () {
            if (bodyLoaded) { return; }
            loadResultBodyState(resultId).then(function (st) {
              bodyLoaded = true;
              fillBody(st);
            }).catch(function () {
              dlBlock.innerHTML = '<strong>DL тест:</strong> <span style="color:#6b7280;">итог тестов отсутствует</span>';
              answerPre.textContent = 'Не удалось загрузить ответ модели.';
              codePre.textContent = '— код недоступен —';
            });
          };
          if (card.open) { loadBody(); }  // карточка восстановлена раскрытой
          card.addEventListener('toggle', function () {
            if (card.open) { loadBody(); }
          });
        } else {
          // Полный item в снапшоте (обратная совместимость) — рендер как раньше.
          answerPre.textContent = r.raw_response || r.short_response || '';
          codePre.textContent = code || '— код не извлечён —';
          if (downloadLink && !code) { downloadLink.style.display = 'none'; }
        }

        card.appendChild(summary);
        card.appendChild(body);
        tasksEl.appendChild(card);
      });

      modelCard.appendChild(tasksEl);
      if (resultsEl) { resultsEl.appendChild(modelCard); }
    });
  }

  // init(rootEl, snapshot, runId): строит карточки сводки + ответов и рендерит.
  // snapshot = {results, report, ...}. Возвращает {downloadBtn, ...}.
  function init(rootEl, snapshot, runId) {
    if (!rootEl) { return {}; }
    rootEl.innerHTML = '';
    snapshot = snapshot || {};

    var reportCard = document.createElement('section');
    reportCard.className = 'arm-card';
    var reportHead = document.createElement('div');
    reportHead.className = 'arm-section-head';
    var reportHeadLeft = document.createElement('div');
    var reportH2 = document.createElement('h2'); reportH2.textContent = 'Сводка';
    var reportNote = document.createElement('p'); reportNote.className = 'arm-section-note'; reportNote.textContent = 'Результаты прогона';
    reportHeadLeft.appendChild(reportH2); reportHeadLeft.appendChild(reportNote);
    reportHead.appendChild(reportHeadLeft);
    // XLSX-выгрузка — серверный эндпоинт по run_id (единый генератор в
    // ai/admin/export.py, автоширина колонок); ссылка, а не клиентский blob.
    var downloadBtn = document.createElement('a');
    downloadBtn.className = 'arm-button';
    downloadBtn.textContent = 'Скачать результаты (XLSX)';
    downloadBtn.style.fontSize = '13px';
    if (runId) {
      downloadBtn.href = '/ai/admin/arm/solve/report/' + encodeURIComponent(runId) + '/xlsx/';
    }
    reportHead.appendChild(downloadBtn);
    reportCard.appendChild(reportHead);

    var reportList = document.createElement('ul'); reportList.className = 'arm-report';
    reportCard.appendChild(reportList);
    var perModelWrap = document.createElement('div'); reportCard.appendChild(perModelWrap);
    var perTopicWrap = document.createElement('div'); perTopicWrap.style.marginTop = '16px'; reportCard.appendChild(perTopicWrap);
    var errorsWrap = document.createElement('div'); errorsWrap.style.marginTop = '16px'; reportCard.appendChild(errorsWrap);
    var reasonsWrap = document.createElement('div'); reasonsWrap.style.marginTop = '16px'; reportCard.appendChild(reasonsWrap);
    var logWrap = document.createElement('div'); logWrap.style.marginTop = '16px'; reportCard.appendChild(logWrap);
    rootEl.appendChild(reportCard);

    var resultsCard = document.createElement('section');
    resultsCard.className = 'arm-card';
    var resultsHead = document.createElement('div'); resultsHead.className = 'arm-section-head';
    var resultsHeadLeft = document.createElement('div');
    var resultsH2 = document.createElement('h2'); resultsH2.textContent = 'Ответы моделей';
    var resultsNote = document.createElement('p'); resultsNote.className = 'arm-section-note';
    resultsNote.textContent = 'Раскройте модель — внутри её задачи, вердикты и полные ответы';
    resultsHeadLeft.appendChild(resultsH2); resultsHeadLeft.appendChild(resultsNote);
    resultsHead.appendChild(resultsHeadLeft);
    resultsCard.appendChild(resultsHead);
    var resultsEl = document.createElement('div'); resultsCard.appendChild(resultsEl);
    rootEl.appendChild(resultsCard);

    var reportEls = { card: reportCard, list: reportList, perModel: perModelWrap, perTopic: perTopicWrap, errors: errorsWrap, reasons: reasonsWrap, log: logWrap };
    renderReport(snapshot.report || null, reportEls);
    // В шапке карточки — переключатель «Все / Не пройденные».
    renderResults(snapshot.results || [], resultsEl, resultsCard, resultsHead);

    var hasResults = (snapshot.results || []).length > 0 && !!runId;
    downloadBtn.style.display = hasResults ? '' : 'none';

    // reportEls/filterHost отдаются наружу — для ЖИВОЙ развёртки
    // (startLiveUpdates): перерисовка отчёта и списка без сброса DOM.
    return {
      reportCard: reportCard, resultsCard: resultsCard, resultsEl: resultsEl,
      downloadBtn: downloadBtn, reportEls: reportEls, filterHost: resultsHead,
    };
  }

  // ── Живая развёртка прогона (общий поллинг для трёх поверхностей) ───────
  // Пока прогон идёт (run_status = running/paused), раз в opts.intervalMs
  // дёргаем snapshotUrl и ПЕРЕРИСОВЫВАЕМ уже построенные карточки
  // (renderReport/renderResults) — цифры и раздел «Сбои по причинам»
  // обновляются вживую, без перерисовки всей развёртки (init стирает DOM
  // и теряет раскрытые карточки).
  //
  // Источник данных — тот же batch-snapshot эндпоинт, что отдаёт развёртку
  // при клике; отдельный «лёгкий» эндпоинт не нужен.
  //
  // stop() — снять таймер (страница уехала / прогон завершён).
  function startLiveUpdates(els, snapshotUrl, opts) {
    opts = opts || {};
    var intervalMs = opts.intervalMs || 2000;
    var maxErrors = opts.maxErrors || 5;
    var timer = null;
    var stopped = false;
    var errors = 0;
    var inFlight = false;

    function stop() {
      stopped = true;
      if (timer) { clearTimeout(timer); timer = null; }
    }

    function tick() {
      if (stopped) { return; }
      if (inFlight) { schedule(); return; }
      inFlight = true;
      fetch(snapshotUrl, {
        credentials: 'same-origin',
        headers: { 'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
      })
        .then(function (response) { return parseJsonResponse(response); })
        .then(function (snapshot) {
          inFlight = false;
          errors = 0;
          if (!snapshot) { schedule(); return; }
          // Сервер ответил ошибкой (прогон стёрт из БД, нет доступа и т.п.).
          // Крутить поллинг вечно бессмысленно — считаем это как сетевую
          // ошибку и останавливаемся после maxErrors попыток.
          if (snapshot.error) {
            errors += 1;
            if (errors >= maxErrors) { stop(); return; }
            schedule();
            return;
          }
          // Прогон завершён (или стёрт) — финальный рендер и стоп.
          var status = snapshot.run_status || '';
          if (opts.onStatus) { opts.onStatus(status, snapshot); }
          if (status && status !== 'running' && status !== 'paused') {
            renderReport(snapshot.report || null, els.report);
            renderResults(snapshot.results || [], els.resultsEl, els.resultsCard, els.filterHost);
            stop();
            return;
          }
          renderReport(snapshot.report || null, els.report);
          renderResults(snapshot.results || [], els.resultsEl, els.resultsCard, els.filterHost);
          schedule();
        })
        .catch(function () {
          inFlight = false;
          errors += 1;
          if (errors >= maxErrors) { stop(); return; }
          schedule();
        });
    }

    function schedule() {
      if (stopped) { return; }
      timer = setTimeout(tick, intervalMs);
    }

    if (!snapshotUrl || !els || !els.report) { return { stop: stop }; }
    schedule();
    return { stop: stop };
  }

  return {
    escapeHtml: escapeHtml, verdictBadge: verdictBadge,
    renderBucketTable: renderBucketTable, renderReport: renderReport,
    renderResults: renderResults, init: init,
    startLiveUpdates: startLiveUpdates,
  };
})();
