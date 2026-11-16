// =============================================================
// Живые цифры пакетных строк журнала запросов
// =============================================================
// Одна и та же строка пакетного прогона (admin/ai/_ai_batch_row.html)
// показывается на «Журнале запросов» и на «Настройке ИИ-приложения».
// Пока прогон идёт, «Пары» (решено/не решено), статус и время меняются;
// скрипт раз в несколько секунд забирает счётчики
// (/ai/admin/ai/request_logs/counters/) и правит ТОЛЬКО цифры в уже
// отрисованных строках.
//
// Почему не перезапрашиваем таблицу целиком: замена #logs-table-wrapper
// схлопывает раскрытые развёртки (детали прогона/запроса) и сбрасывает
// прокрутку — а оператор как раз смотрит на идущий прогон. Точечное
// обновление сохраняет состояние страницы.
// =============================================================

(function () {
  'use strict';

  var COUNTERS_URL = '/ai/admin/ai/request_logs/counters/';
  var INTERVAL_MS = 5000;
  var ROW_SEL = '.ai-batch-row[data-run-id]';

  // Завершённые прогоны больше не меняются — снимаем их с опроса, чтобы
  // не ходить в БД впустую (страница может висеть открытой часами).
  var finishedRuns = new Set();
  var inflight = false;

  function batchRows() {
    return Array.prototype.slice.call(document.querySelectorAll(ROW_SEL));
  }

  function activeRunIds() {
    var seen = new Set();
    batchRows().forEach(function (row) {
      var id = row.getAttribute('data-run-id') || '';
      if (id && !finishedRuns.has(id)) seen.add(id);
    });
    return Array.from(seen);
  }

  // Ответ админки на протухшую сессию — HTML логина, а не JSON: не даём
  // JSON.parse упасть невнятной ошибкой (тот же приём, что parseJsonResponse
  // в ai-common.js).
  function readJson(response) {
    return response.text().then(function (text) {
      var data = null;
      try { data = JSON.parse(text); } catch (err) { data = null; }
      if (!response.ok) {
        var reason = data && (data.error || data.message);
        throw new Error(reason || ('HTTP ' + response.status));
      }
      if (!data) {
        throw new Error('Сервер вернул не JSON (вероятно, сессия истекла)');
      }
      return data;
    });
  }

  function setPairs(cell, info) {
    var empty = cell.querySelector('.ai-live-pairs-empty');
    var values = cell.querySelector('.ai-live-pairs-values');
    var solved = cell.querySelector('.ai-live-solved');
    var failed = cell.querySelector('.ai-live-failed');
    if (!empty || !values || !solved || !failed) return;
    var total = Number(info.total || 0);
    var solvedCount = Number(info.solved || 0);
    var failedCount = Number(info.failed || 0);
    empty.hidden = total > 0;
    values.hidden = total <= 0;
    solved.textContent = String(solvedCount);
    failed.textContent = String(failedCount);
    failed.className = 'ai-live-failed'
      + (failedCount ? ' ai-recent-logs-err' : '');
    cell.title = total
      ? 'Решено: ' + solvedCount + ' · не решено: ' + failedCount
        + ' · всего пар: ' + total
      : 'Прогон без пар';
  }

  function setStatus(cell, info) {
    if (!info.status_display) return;
    cell.textContent = info.status_display;
    cell.className = (info.status_class || 'ai-recent-logs-err') + ' ai-nowrap';
  }

  function setTime(row, info) {
    var el = row.querySelector('.ai-live-time');
    if (!el) return;
    var text = '';
    if (info.received_at) text = 'получен ' + info.received_at;
    if (info.time_display) text += (text ? ' · ' : '') + info.time_display;
    el.textContent = text;
  }

  function applyRow(row, info) {
    var pairsCell = row.querySelector('[data-live-pairs]');
    var statusCell = row.querySelector('[data-live-status]');
    if (pairsCell) setPairs(pairsCell, info);
    if (statusCell) setStatus(statusCell, info);
    setTime(row, info);
  }

  function tick() {
    if (document.hidden || inflight) return;
    var ids = activeRunIds();
    if (!ids.length) return;

    inflight = true;
    var url = COUNTERS_URL + '?run_ids=' + encodeURIComponent(ids.join(','));
    fetch(url, {
      credentials: 'same-origin',
      headers: { 'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest' }
    })
      .then(readJson)
      .then(function (data) {
        var byRun = (data && data.runs) || {};
        batchRows().forEach(function (row) {
          var id = row.getAttribute('data-run-id') || '';
          var info = byRun[id];
          if (!info) return;
          applyRow(row, info);
          if (info.finished) finishedRuns.add(id);
        });
      })
      .catch(function (err) {
        // Не алертим: страница может висеть в фоне, а сеть/сессия —
        // восстановиться. Диагностика — в консоли.
        console.warn('live counters:', err.message || err);
      })
      // .finally нет в старых Chromium Portable — обычный then.
      .then(function () {
        inflight = false;
      });
  }

  function start() {
    tick();
    setInterval(tick, INTERVAL_MS);
    // Вернулись на вкладку — обновляем сразу, не ждём тика.
    document.addEventListener('visibilitychange', function () {
      if (!document.hidden) tick();
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
