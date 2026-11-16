// Логика детальной страницы записи «Журнала запросов»:
//   1) для пакетного прогона (ARM) — развёртка таблицы результатов через
//      window.ArmBatchResults (общий скрипт подключается ГЛОБАЛЬНО в
//      admin/base_site.html → extrahead, т.е. он определён ДО этого файла);
//   2) кнопка «Повторить запрос / Повторить прогон» — POST на resend/rerun-arm.
//
// Вынесена из ai/templates/admin/ai/request_logs_detail.html.
// Шаблонных переменных здесь нет: id записи берётся из data-log-id кнопки,
// CSRF-токен — из input[name="csrfmiddlewaretoken"] (его рендерит шаблон).
      (function () {
        // Блок только для пакетного прогона (ARM): на обычной записи журнала
        // нет ни #batch-snapshot-data, ни #batch-results-card — выходим.
        var raw = document.getElementById('batch-snapshot-data');
        var card = document.getElementById('batch-results-card');
        if (!raw || !card) { return; }
        var logId = (document.getElementById('resend-btn') || {}).dataset
          ? document.getElementById('resend-btn').dataset.logId
          : '';
        var snapshot = raw ? JSON.parse(raw.textContent) : {};
        var built = window.ArmBatchResults.init(
          card,
          snapshot,
          snapshot.run_id || ''
        );
        // Живая развёртка: пока прогон идёт, цифры в «Сводке» и раздел
        // «Сбои по причинам» обновляются сами (раз в 2 с) — тот же
        // эндпоинт, что отдал первоначальный снапшот.
        var runStatus = snapshot.run_status || '';
        if (built && built.reportEls && (runStatus === 'running' || runStatus === 'paused')) {
          window.ArmBatchResults.startLiveUpdates(
            {
              report: built.reportEls,
              resultsEl: built.resultsEl,
              resultsCard: built.resultsCard,
              filterHost: built.filterHost,
            },
            '/ai/admin/ai/request_logs/' + (logId || '') + '/batch-snapshot/',
            { intervalMs: 2000 }
          );
        }
        var btn = document.getElementById('resend-btn');
        if (btn && built && built.downloadBtn && built.downloadBtn.parentNode) {
          btn.style.display = '';
          // Группа справа: «Повторить прогон» + «Скачать результаты (XLSX)»
          // рядом (space-between шапки иначе разнёс бы их по краям).
          var group = document.createElement('span');
          group.className = 'arm-head-actions';
          built.downloadBtn.parentNode.insertBefore(group, built.downloadBtn);
          group.appendChild(btn);
          group.appendChild(built.downloadBtn);
        }
      })();
    

  document.addEventListener('DOMContentLoaded', function () {
    const btn = document.getElementById('resend-btn');
    if (!btn) return;
    // Восстановление подписи после клика: на batch-странице кнопка называется
    // «Повторить прогон», на обычной — «Повторить запрос» — верная подпись —
    // та, что отрендерил шаблон (литерал в коде раньше затирал прогонный).
    const originalLabel = btn.textContent.trim() || 'Повторить запрос';

    btn.addEventListener('click', function () {
      const logId = btn.dataset.logId;
      const source = btn.dataset.source || '';
      const mode = btn.dataset.mode || '';
      const logMessage = btn.dataset.message || '';
      // Rerun-лог («Batch solve rerun <hex>») — тоже ARM batch: после
      // попарного перезапуска кнопка должна вести в rerun-arm, а не в resend.
      const isArmBatch = source === 'arm' && (mode === 'batch_solve' || mode === 'solve') && (logMessage.indexOf('Batch solve run') !== -1 || logMessage.indexOf('Batch solve rerun') !== -1);
      const endpoint = isArmBatch
        ? '/ai/admin/ai/request_logs/' + logId + '/rerun-arm/'
        : '/ai/admin/ai/request_logs/' + logId + '/resend/';

      // Disable button and show loading state.
      btn.disabled = true;
      btn.textContent = 'Отправка...';

      // Create status container if it doesn't exist.
      let statusDiv = document.querySelector('.resend-status');
      if (!statusDiv) {
        statusDiv = document.createElement('div');
        statusDiv.className = 'resend-status';
        btn.parentElement.insertAdjacentElement('afterend', statusDiv);
      }
      statusDiv.className = 'resend-status';
      statusDiv.textContent = '';
      statusDiv.style.display = 'none';

      // Get CSRF token from the DOM (rendered by the csrf_token tag).
      const csrfInput = document.querySelector('input[name="csrfmiddlewaretoken"]');
      const csrftoken = csrfInput ? csrfInput.value : '';

      fetch(endpoint, {
        method: 'POST',
        headers: {
          'X-CSRFToken': csrftoken,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({}),
      })
      .then(function (response) {
        // При протухшей сессии POST редиректит на страницу логина (HTML) —
        // показываем внятное сообщение вместо «Unexpected token '<'».
        return response.text().then(function (text) {
          try { return JSON.parse(text); }
          catch (e) {
            throw new Error('Сессия истекла — обновите страницу и войдите заново.');
          }
        });
      })
      .then(function (data) {
        btn.disabled = false;
        btn.textContent = originalLabel;
        if (data.success || data.run_id) {
          statusDiv.className = 'resend-status success';
          if (isArmBatch && data.run_id) {
            statusDiv.textContent = 'Перезапуск ARM... ';
            const link = document.createElement('a');
            link.href = '/ai/admin/arm/solve/?run_id=' + data.run_id;
            link.textContent = 'Открыть прогон →';
            link.style.marginLeft = '8px';
            statusDiv.appendChild(link);
            // Auto-redirect to the ARM solve page with the new run_id.
            window.location.href = '/ai/admin/arm/solve/?run_id=' + data.run_id;
          } else {
            statusDiv.textContent = 'Запрос успешно отправлен. ';
            if (data.new_log_id) {
              const link = document.createElement('a');
              link.href = '/ai/admin/ai/request_logs/' + data.new_log_id + '/';
              link.textContent = 'Открыть новый лог →';
              link.style.marginLeft = '8px';
              statusDiv.appendChild(link);
            }
          }
        } else {
          statusDiv.className = 'resend-status error';
          statusDiv.textContent = 'Ошибка: ' + (data.error || 'Неизвестная ошибка');
          if (data.new_log_id) {
            const link = document.createElement('a');
            link.href = '/ai/admin/ai/request_logs/' + data.new_log_id + '/';
            link.textContent = 'Открыть лог ошибки →';
            link.style.marginLeft = '8px';
            statusDiv.appendChild(link);
          }
        }
      })
      .catch(function (err) {
        btn.disabled = false;
        btn.textContent = originalLabel;
        statusDiv.className = 'resend-status error';
        statusDiv.textContent = 'Сетевая ошибка: ' + err.message;
      });
    });
  });
