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
  // Базовый URL тела результата (ленивая догрузка карточки пары).
  var downloadBase = '/ai/admin/arm/solve/result/';
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
        dl_file_name: b.dl_file_name || '',
        dl_test_data: b.dl_test_data || {},
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
    var head = '<thead><tr><th>' + escapeHtml(labelHeader) + '</th><th class="arm-col-num">% решённых</th><th class="arm-col-num">Ср. время, с</th><th class="arm-col-num">Токены</th><th class="arm-col-num">Решено / Всего</th></tr></thead>';
    var body = rows.map(function (r) {
      return '<tr><td>' + escapeHtml(r.label) + '</td>'
        + '<td class="arm-col-num">' + escapeHtml(r.percent_solved) + '</td>'
        + '<td class="arm-col-num">' + escapeHtml(r.avg_duration) + '</td>'
        + '<td class="arm-col-num">' + escapeHtml(r.tokens) + '</td>'
        + '<td class="arm-col-num">' + escapeHtml(r.solved) + ' / ' + escapeHtml(r.total) + '</td></tr>';
    }).join('');
    wrapEl.insertAdjacentHTML('beforeend', '<table class="arm-summary-table arm-summary-table--bucket">' + head + '<tbody>' + body + '</tbody></table>');
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
      (report.hints || []).forEach(function (hint) {
        lines.push('<strong class="arm-report-hint">' + escapeHtml(hint) + '</strong>');
      });
      els.list.innerHTML = lines.map(function (line) { return '<li>' + line + '</li>'; }).join('');
    }
    if (els.perModel) {
      els.perModel.innerHTML = '<h3>Итог по моделям</h3>';
      renderBucketTable(report.per_model, 'Модель', els.perModel);
      // Сноска про сортировку — ПОД таблицей: иначе она сдвигала таблицу
      // вниз относительно соседней («Итог по темам») — колонки «плыли».
      var sortNote = document.createElement('p');
      sortNote.className = 'arm-section-note';
      sortNote.textContent = 'Сортировка: процент решённых ↓, среднее время ответа ↑';
      els.perModel.appendChild(sortNote);
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
      + '<table class="arm-summary-table arm-summary-table--reasons"><thead><tr><th>Причина</th>'
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
      // Причины сбоя внутри темы (компиляция / неверный ответ / нет кода /
      // сбой модели) — считает сервер (arm_runner::_build_batch_report).
      // КАЖДАЯ ПРИЧИНА — С НОВОЙ СТРОКИ (было одной строкой через запятую).
      const reasons = (row.reasons || []).map(function (r) {
        return '<div class="arm-error-reason">' + escapeHtml(r.label) + ' — ' + escapeHtml(r.count) + '</div>';
      }).join('');
      // Позиции — мини-ссылки на карточку пары в «Ответах моделей»
      // (data-key = task_node_id:model_key): причина сбоя — прямо здесь,
      // клик — скролл к паре (см. jumpToResult).
      const items = (row.items || []).map(function (it) {
        if (!it || typeof it === 'string') {
          return '<div class="arm-error-item">' + escapeHtml(it || '') + '</div>';
        }
        // Номер проваленного теста (если вердикт DL его назвал) — рядом с
        // причиной: видно, КАКОЙ тест упал (вход/выход — в карточке пары).
        const testNote = it.test_number ? ' (тест № ' + escapeHtml(it.test_number) + ')' : '';
        return '<div class="arm-error-item">'
          + '<a href="#" class="arm-error-link" data-arm-result-key="' + escapeHtml(it.key || '') + '"'
          + ' title="Перейти к этой паре в списке «Ответы моделей»">'
          + escapeHtml(it.task || '') + '</a>'
          + '<span class="arm-error-item-reason">' + escapeHtml(it.reason || '') + testNote + '</span>'
          + '<span class="arm-error-item-model">' + escapeHtml(it.model || '') + '</span>'
          + '</div>';
      }).join('');
      const hidden = row.hidden > 0
        ? '<div class="arm-error-item"><span class="arm-section-note">+ ещё ' + escapeHtml(row.hidden) + '</span></div>'
        : '';
      return '<tr><td>' + escapeHtml(row.topic) + '</td>'
        + '<td class="arm-col-num">' + escapeHtml(row.count) + '</td>'
        + '<td>' + reasons + '</td>'
        + '<td>' + items + hidden + '</td></tr>';
    });
    wrapEl.innerHTML = '<h3>Ошибки по темам</h3>'
      + '<table class="arm-summary-table arm-summary-table--errors"><thead><tr><th>Тема</th>'
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

  // ── Инкрементальное обновление списка результатов ─────────────────────
  // Список «Ответы моделей» НЕ пересобирается целиком: новая пара
  // (+1 задача) ДОБАВЛЯЕТСЯ в таблицу, а уже показанные карточки
  // обновляются на месте (вердикт / причина / время / токены). Иначе
  // каждый тик поллинга (2 с) пересоздавал бы тысячу DOM-узлов, страница
  // «прыгала» бы, а раскрытые карточки теряли бы загруженное тело.
  function rowSignature(r) {
    return [
      r.verdict || '', r.failure_reason || '', r.failure_reason_label || '',
      r.truncated ? '1' : '0',
      r.duration == null ? '' : r.duration,
      r.tokens == null ? '' : r.tokens,
    ].join('|');
  }

  function resultsState(resultsEl) {
    if (!resultsEl) { return null; }
    if (!resultsEl._armState) {
      resultsEl._armState = { cards: {}, groups: {} };
    }
    return resultsEl._armState;
  }

  // Бейджи карточки (вердикт / причина / «обрезан») — единая точка сборки:
  // и при создании карточки, и при точечном обновлении, и после догрузки
  // тела (там выясняется, что ответ обрезан лимитом генерации).
  function renderBadges(card, row) {
    var badges = card.querySelector('.arm-result-badges');
    if (!badges || !row) { return; }
    var truncated = !!row.truncated || !!card._armBodyTruncated;
    badges.innerHTML = verdictBadge(row.verdict)
      + (row.failure_reason ? failureReasonBadge(row.failure_reason, row.failure_reason_label) : '')
      + (truncated ? truncatedBadge() : '');
  }

  // Карточка пары (задача × модель). Возвращает DOM-узел; у узла есть
  // _armUpdate(row) — точечное обновление без пересоздания.
  // Мета-строка карточки: «12 сек | 340 ток. | тест № 2» — номер проваленного
  // теста (если вердикт DL его назвал) виден сразу, без раскрытия карточки.
  function cardMetaText(row) {
    var text = (row.duration != null ? row.duration : '') + ' сек | '
      + (row.tokens != null ? row.tokens : '') + ' ток.';
    if (row.dl_test_number) {
      text += ' | тест № ' + escapeHtml(row.dl_test_number);
    }
    return text;
  }

  function createResultCard(r, openKeys) {
    var cls = r.verdict === 'solved' ? 'solved' : (r.verdict === 'failed' ? 'failed' : 'skipped');
    var card = document.createElement('details');
    card.className = 'arm-result ' + cls;
    var rkey = (r.task_node_id || '') + ':' + (r.model_key || '');
    card.setAttribute('data-key', rkey);
    card._armKey = rkey;
    card._armRow = r;
    card._armSig = rowSignature(r);
    if (openKeys.has(rkey)) { card.setAttribute('open', ''); }

    var lazy = !!r.lazy_body;               // тело подтягивается по AJAX
    var dlComment = r.dl_comment || '';
    var dlError = r.dl_error || '';
    var dlQueueId = r.dl_queue_id || 0;
    // Имя файла решения, отправленного в DL («<модель>_<задача>.<ext>»).
    var dlFileName = r.dl_file_name || '';
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
    // Бейджи — ОДНИМ контейнером .arm-result-badges (иначе строка «прыгала»:
    // вердикт улетал влево, причина — вправо, «обрезан» — ещё дальше).
    var badges = document.createElement('span');
    badges.className = 'arm-result-badges';
    top.appendChild(badges);
    var meta = document.createElement('div');
    meta.className = 'arm-meta';
    meta.innerHTML = cardMetaText(r);
    summary.appendChild(top);
    summary.appendChild(meta);

    var body = document.createElement('div');
    body.className = 'arm-result-body';

    var dlBlock = document.createElement('div');
    dlBlock.style.cssText = 'padding:8px 12px; margin-bottom:8px; border-radius:4px; font-size:13px; color:#1f2937; background:' + (r.verdict === 'solved' ? '#dcfce7' : '#fee2e2') + ';';
    var dlHtml = '<strong>DL тест:</strong> ';
    if (dlComment) { dlHtml += escapeHtml(dlComment); } else { dlHtml += '<span style="color:#6b7280;">итог тестов отсутствует</span>'; }
    if (dlQueueId) { dlHtml += ' <span style="color:#6b7280;">· queueId: ' + escapeHtml(dlQueueId) + '</span>'; }
    if (dlFileName) { dlHtml += ' <span style="color:#6b7280;">· файл: ' + escapeHtml(dlFileName) + '</span>'; }
    dlHtml += deltaLabel(deltaId, deltaName);
    if (r.saved_solution) { dlHtml += ' <span style="color:#166534;">· ✓ сохранено в «Решённые задачи»</span>'; }
    if (dlError) { dlHtml += '<div style="margin-top:4px; color:#991b1b;"><strong>Ошибка DL:</strong> ' + escapeHtml(dlError) + '</div>'; }
    dlBlock.innerHTML = dlHtml;
    body.appendChild(dlBlock);

    // Ответ модели + извлечённый код — ЛЕВАЯ колонка (уже): справа, на одном
    // уровне с ними, входные/эталонные данные проваленного теста (тесно?
    // нет: колонки грида, см. .arm-card-cols). На узком экране — стопкой.
    var colsEl = document.createElement('div');
    colsEl.className = 'arm-card-cols';
    var colMain = document.createElement('div');
    colMain.className = 'arm-card-col-main';
    var colData = document.createElement('div');
    colData.className = 'arm-card-col-data';
    colsEl.appendChild(colMain);
    colsEl.appendChild(colData);
    // У решённой пары данных теста нет по определению — грид из двух колонок
    // оставил бы пустое пространство справа, значит одна колонка.
    if (r.verdict === 'solved') { colsEl.classList.add('arm-card-cols--single'); }
    body.appendChild(colsEl);

    var answerLabel = document.createElement('div');
    answerLabel.style.cssText = 'margin:8px 0 4px; font-weight:600; font-size:13px;';
    answerLabel.textContent = 'Полный ответ модели:';
    colMain.appendChild(answerLabel);
    var answerPre = document.createElement('pre');
    colMain.appendChild(answerPre);

    var codeLabel = document.createElement('div');
    codeLabel.style.cssText = 'margin:10px 0 4px; font-weight:600; font-size:13px;';
    codeLabel.textContent = 'Извлечённый код программы:';
    colMain.appendChild(codeLabel);
    var codePre = document.createElement('pre');
    colMain.appendChild(codePre);
    var downloadLink = null;
    if (resultId) {
      downloadLink = document.createElement('a');
      downloadLink.href = downloadBase + encodeURIComponent(resultId) + '/download/';
      downloadLink.className = 'arm-button';
      downloadLink.style.cssText = 'display:inline-block; margin-top:6px;';
      downloadLink.textContent = 'Скачать код';
      colMain.appendChild(downloadLink);
    }

    // Входные данные и эталонный ответ проваленного теста — ПРАВАЯ колонка
    // (только у НЕ решённых пар, у которых DL разрешил выдачу теста). Данные
    // компактные — обрезаны сервером.
    var testDataEl = document.createElement('div');
    testDataEl.className = 'arm-test-data';
    testDataEl.hidden = true;
    colData.appendChild(testDataEl);
    // Пояснение «почему блока нет»: DL отдаёт данные только подтверждённого
    // проваленного теста с разрешённой выдачей (спека /get-test-data); у
    // ошибок компиляции подтверждённого провала обычно нет вовсе. Без этого
    // текста отсутствие блока выглядит как баг.
    var testDataEmptyEl = document.createElement('div');
    testDataEmptyEl.hidden = true;
    testDataEmptyEl.style.cssText = 'margin-top:2px; font-size:12px; color:#6b7280;';
    testDataEmptyEl.textContent =
      'Входные/эталонные данные теста DL не выдал — тест не выполнялся (например, ошибка компиляции) или выдача запрещена настройками курса.';
    colData.appendChild(testDataEmptyEl);
    var renderTestData = function (data) {
      if (!data || (!data.input && !data.expected_output)) { testDataEl.hidden = true; return; }
      var parts = [];
      if (data.test_number) {
        parts.push('<div style="font-weight:600; font-size:13px; margin-bottom:4px;">Данные проваленного теста № '
          + escapeHtml(data.test_number) + '</div>');
      }
      if (data.input) {
        parts.push('<div style="font-size:12px; color:#6b7280;">Входные данные'
          + (data.input_truncated ? ' (начало)' : '') + ':</div>');
        parts.push('<pre>' + escapeHtml(data.input) + '</pre>');
      }
      if (data.expected_output) {
        parts.push('<div style="font-size:12px; color:#6b7280;">Эталонный ответ'
          + (data.expected_truncated ? ' (начало)' : '') + ':</div>');
        parts.push('<pre>' + escapeHtml(data.expected_output) + '</pre>');
      }
      testDataEl.innerHTML = parts.join('');
      testDataEl.hidden = false;
    };
    var renderTestDataWithHint = function (data, bodyDone) {
      renderTestData(data);
      // Подсказка — только когда тело УЖЕ догружено (lazy-пара до первого
      // раскрытия не знает, чем ответит body-эндпоинт) и данных нет, у
      // НЕ решённой пары (у решённой данных по определению нет).
      var noData = !data || (!data.input && !data.expected_output);
      testDataEmptyEl.hidden = !(noData && bodyDone && r.verdict !== 'solved');
    };
    renderTestDataWithHint(r.dl_test_data, !lazy);

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
        dlFileName = st.dl_file_name || dlFileName;
        if (dlFileName) { dlHtml2 += ' <span style="color:#6b7280;">· файл: ' + escapeHtml(dlFileName) + '</span>'; }
        // Номер проваленного теста приходит с телом (ленивая загрузка).
        if (st.dl_test_number) {
          card._armRow = Object.assign({}, card._armRow || r, { dl_test_number: st.dl_test_number });
          var metaEl = card.querySelector('.arm-meta');
          if (metaEl) { metaEl.innerHTML = cardMetaText(card._armRow); }
        }
        dlHtml2 += deltaLabel(deltaId, deltaName);
        if (r.saved_solution) { dlHtml2 += ' <span style="color:#166534;">· ✓ сохранено в «Решённые задачи»</span>'; }
        if (dlError) { dlHtml2 += '<div style="margin-top:4px; color:#991b1b;"><strong>Ошибка DL:</strong> ' + escapeHtml(dlError) + '</div>'; }
        dlBlock.innerHTML = dlHtml2;
        answerPre.textContent = st.raw || st.short_response || '';
        codePre.textContent = st.code || '— код не извлечён —';
        if (downloadLink) { downloadLink.style.display = st.code ? '' : 'none'; }
        // Бейдж «обрезан», если в снапшоте его не было (легаси-строки БД).
        card._armBodyTruncated = !!st.truncated;
        renderBadges(card, card._armRow || r);
        renderTestDataWithHint(st.dl_test_data, true);
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
    renderBadges(card, r);

    // Точечное обновление карточки (без пересоздания DOM).
    card._armUpdate = function (row) {
      var sig = rowSignature(row);
      if (sig === card._armSig) { return; }
      card._armSig = sig;
      card._armRow = row;
      card.className = 'arm-result '
        + (row.verdict === 'solved' ? 'solved' : (row.verdict === 'failed' ? 'failed' : 'skipped'));
      renderBadges(card, row);
      var metaEl = card.querySelector('.arm-meta');
      if (metaEl) {
        metaEl.innerHTML = cardMetaText(row);
      }
      var titleEl = card.querySelector('.arm-result-title');
      if (titleEl && row.task_name) {
        var dlLink = titleEl.querySelector('.arm-result-task-link');
        var titleText = '#' + (row.task_node_id || '') + ' ' + row.task_name;
        if (dlLink) { dlLink.textContent = titleText; }
        else { titleEl.textContent = titleText; }
      }
    };
    return card;
  }

  // Группа «модель → её задачи» (раскрывающийся блок).
  function createModelGroup(group, openKeys) {
    var modelCard = document.createElement('details');
    modelCard.className = 'arm-model-group';
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
    mSummary.appendChild(mHead);
    mSummary.appendChild(mMeta);
    modelCard.appendChild(mSummary);

    var tasksEl = document.createElement('div');
    tasksEl.className = 'arm-model-tasks';
    modelCard.appendChild(tasksEl);

    return { el: modelCard, tasksEl: tasksEl, meta: mMeta, key: group.key };
  }

  function updateModelGroup(g, group) {
    var solvedCount = group.tasks.filter(function (t) { return t.verdict === 'solved'; }).length;
    var totalTokens = group.tasks.reduce(function (s, t) { return s + (t.tokens || 0); }, 0);
    var withTime = group.tasks.filter(function (t) { return t.duration != null; });
    var avgTime = withTime.length
      ? (withTime.reduce(function (s, t) { return s + (t.duration || 0); }, 0) / withTime.length).toFixed(1)
      : '';
    // Кромка группы — итог модели: зелёная (всё решено), красная (ни одной),
    // серая (частично).
    g.el.className = 'arm-model-group '
      + (solvedCount === group.tasks.length ? 'all-solved'
        : (solvedCount === 0 ? 'none-solved' : 'part-solved'));
    g.meta.textContent = 'Решено: ' + solvedCount + ' из ' + group.tasks.length
      + (avgTime ? ' · среднее время: ' + avgTime + ' сек' : '')
      + (totalTokens ? ' · токенов: ' + totalTokens : '');
  }

  // renderResults(results, resultsEl, resultsCard, filterHost): рендер списка
  // «Ответы моделей» с учётом filterMode. filterHost — контейнер для
  // переключателя (шапка карточки); без него список рисуется целиком.
  // Раскрытие карточек: ручные открытия сохраняются при перерисовке
  // (и переключении фильтров), но по умолчанию ВСЕ карточки свёрнуты —
  // раскрываются только кликом.
  //
  // Обновление — ИНКРЕМЕНТАЛЬНОЕ: карточки переиспользуются, новые пары
  // добавляются, изменившиеся — обновляются на месте (см. createResultCard).
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
    // прошлого тика) — трогать DOM незачем.
    // В сигнатуру входит число пар с ПРИЧИНОЙ сбоя: вердикт и причина —
    // разные оси, и пара может сменить причину, не сменив вердикт.
    // Плюс — ХЕШ пар (задача×модель→вердикт): пара может перейти
    // failed→solved (число записей то же), и без этого карточка осталась бы
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

    // Раскрытие (см. докстринг): domOpen читается ДО правок DOM.
    var domOpen = collectOpenKeys(resultsEl);
    var openKeys = new Set();
    if (filterMode === 'all') {
      if (prevRenderedMode === 'all') { allOpenKeys = domOpen; }
      openKeys = allOpenKeys;
    } else {
      openKeys = domOpen; // внутри режима — ручные сворачивания сохраняются
    }
    prevRenderedMode = filterMode;

    var state = resultsState(resultsEl);
    if (!visible.length) {
      if (resultsEl) {
        resultsEl.innerHTML = '<p class="arm-section-note arm-results-empty">'
          + (filterMode === 'failed' ? 'Не пройденных результатов нет.' : 'нет данных')
          + '</p>';
      }
      if (state) { state.cards = {}; state.groups = {}; }
      return;
    }
    if (resultsEl) {
      var emptyNote = resultsEl.querySelector('.arm-results-empty');
      if (emptyNote) { emptyNote.remove(); }
    }

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

    var seenCards = {};
    var seenGroups = {};
    groups.forEach(function (group) {
      var g = state.groups[group.key];
      if (!g) {
        g = createModelGroup(group, openKeys);
        state.groups[group.key] = g;
      }
      updateModelGroup(g, group);
      seenGroups[group.key] = true;

      // Карточки пар этой модели: новые — создаются, существующие —
      // обновляются на месте (точечно).
      var desired = [];
      group.tasks.forEach(function (r) {
        var rkey = (r.task_node_id || '') + ':' + (r.model_key || '');
        seenCards[rkey] = true;
        var card = state.cards[rkey];
        if (!card) {
          card = createResultCard(r, openKeys);
          state.cards[rkey] = card;
        } else {
          card._armUpdate(r);
        }
        desired.push(card);
      });
      // Порядок карточек — как в массиве (свежие пары сверху): двигаем
      // только те узлы, что стоят не на своём месте.
      var tasksEl = g.tasksEl;
      desired.forEach(function (el, i) {
        var cur = tasksEl.children[i];
        if (cur !== el) { tasksEl.insertBefore(el, cur || null); }
      });
      Array.prototype.slice.call(tasksEl.children).forEach(function (el) {
        if (desired.indexOf(el) === -1) { el.remove(); }
      });
    });

    // Группы, которых больше нет в списке (фильтр/перезапуск) — убираем.
    Object.keys(state.groups).forEach(function (gkey) {
      if (seenGroups[gkey]) { return; }
      var g = state.groups[gkey];
      if (g.el && g.el.parentNode) { g.el.remove(); }
      delete state.groups[gkey];
    });
    // Порядок групп в списке.
    if (resultsEl) {
      groups.forEach(function (group, i) {
        var g = state.groups[group.key];
        var cur = resultsEl.children[i];
        if (cur !== g.el) { resultsEl.insertBefore(g.el, cur || null); }
      });
    }
    // Карточки, которых больше нет в списке, — из кэша.
    Object.keys(state.cards).forEach(function (rkey) {
      if (!seenCards[rkey]) { delete state.cards[rkey]; }
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
    // Сводные таблицы — блоком, одна за другой, каждая во всю ширину
    // карточки (модели, темы, ошибки, причины).
    var summaryGrid = document.createElement('div');
    summaryGrid.className = 'arm-summary-grid';
    reportCard.appendChild(summaryGrid);
    var perModelWrap = document.createElement('div'); summaryGrid.appendChild(perModelWrap);
    var perTopicWrap = document.createElement('div'); summaryGrid.appendChild(perTopicWrap);
    var errorsWrap = document.createElement('div'); summaryGrid.appendChild(errorsWrap);
    var reasonsWrap = document.createElement('div'); summaryGrid.appendChild(reasonsWrap);
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

  // ── Переход к паре из «Ошибок по темам» ───────────────────────────────
  // Позиция в отчёте — ссылка на карточку пары в списке «Ответы моделей»
  // (data-key = task_node_id:model_key). Клик: раскрыть модель, раскрыть
  // карточку, подскроллить к ней. Кнопка «↑ Наверх» возвращает к сводке.
  var backTopBtn = null;
  var backTopTarget = null;

  function findResultCard(key) {
    if (!key) { return null; }
    var cards = document.querySelectorAll('details[data-key]');
    for (var i = 0; i < cards.length; i++) {
      if (cards[i].getAttribute('data-key') === key) { return cards[i]; }
    }
    return null;
  }
  function ensureBackTop() {
    if (backTopBtn) { return backTopBtn; }
    backTopBtn = document.createElement('button');
    backTopBtn.type = 'button';
    backTopBtn.className = 'arm-backtop';
    backTopBtn.textContent = '↑ Наверх';
    backTopBtn.addEventListener('click', function () {
      if (backTopTarget && backTopTarget.scrollIntoView) {
        backTopTarget.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
      hideBackTop();
    });
    document.body.appendChild(backTopBtn);
    return backTopBtn;
  }
  function showBackTop(target) {
    backTopTarget = target || null;
    ensureBackTop().classList.add('visible');
  }
  function hideBackTop() {
    if (backTopBtn) { backTopBtn.classList.remove('visible'); }
  }
  function jumpToResult(key, sourceEl) {
    var card = findResultCard(key);
    if (!card) { return; }
    // Раскрыть группу модели (карточка пары лежит внутри <details> модели).
    var group = card.parentElement ? card.parentElement.closest('details.arm-model-group') : null;
    if (group) { group.open = true; }
    card.open = true;
    card.classList.add('arm-result-flash');
    window.setTimeout(function () { card.classList.remove('arm-result-flash'); }, 2000);
    window.requestAnimationFrame(function () {
      card.scrollIntoView({ behavior: 'smooth', block: 'center' });
    });
    var target = sourceEl && sourceEl.closest ? sourceEl.closest('.arm-card') : null;
    showBackTop(target);
  }
  // Делегированный обработчик: ссылки-минимумы живут в отчёте, который
  // перерисовывается целиком — слушатель вешаем один раз на документ.
  document.addEventListener('click', function (ev) {
    var link = ev.target && ev.target.closest ? ev.target.closest('.arm-error-link') : null;
    if (!link) { return; }
    ev.preventDefault();
    jumpToResult(link.getAttribute('data-arm-result-key'), link);
  });

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
