// Общая логика строк ПАКЕТНЫХ РЕШЕНИЙ в админке.
//
// Раньше эта логика была продублирована в трёх файлах (app_settings/index.js,
// pinned_runs/index.js, request_logs/index.js): развёртка результатов прогона
// по клику + кнопка ★ (закрепить/открепить). ТЗ: «любой лог пакетного
// решения должен выглядеть одинаково» — значит и вести себя должен одинаково,
// а разметка теперь одна (admin/ai/_ai_batch_row.html).
//
// Подключается ГЛОБАЛЬНО в ai/templates/admin/base_site.html (extrahead) —
// рядом с window.ArmBatchResults, который рисует результаты прогона.
//
// Всё на ДЕЛЕГИРОВАНИИ (document.addEventListener): таблица журнала
// перезаменяется целиком при AJAX-обновлении фильтров — прямые обработчики
// на строках после replaceWith терялись бы.
window.AiBatchRows = (function () {
  'use strict';

  // Токен CSRF: из формы (страницы админки её имеют) или из куки.
  function csrfToken() {
    var input = document.querySelector('input[name="csrfmiddlewaretoken"]');
    if (input && input.value) return input.value;
    var m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : '';
  }

  function parseJsonResponse(response) {
    // Протухшая сессия → admin_view отдаёт HTML страницы логина: response.json()
    // падает невнятным «Unexpected token '<'» — читаем текстом и говорим
    // по-человечески (тот же контракт, что в batch_results/index.js).
    return response.text().then(function (text) {
      try { return JSON.parse(text); }
      catch (e) {
        throw new Error('Сессия истекла — обновите страницу и войдите заново.');
      }
    });
  }

  // ── ★ Закрепить / открепить прогон ────────────────────────────────────
  function togglePin(btn) {
    if (btn.disabled || btn.classList.contains('busy')) return;
    btn.classList.add('busy');
    fetch(btn.getAttribute('data-pin-url'), {
      method: 'POST',
      headers: { 'X-CSRFToken': csrfToken(), 'X-Requested-With': 'XMLHttpRequest' },
      credentials: 'same-origin',
    })
      .then(parseJsonResponse)
      .then(function (data) {
        if (!data || !data.ok) {
          throw new Error((data && data.message) || 'Не удалось изменить закрепление');
        }
        btn.classList.toggle('ai-log-pin-btn-active', !!data.pinned);
        btn.textContent = data.pinned ? '★' : '☆';
        btn.title = data.pinned
          ? 'Убрать из «Закреплённых»'
          : 'Закрепить в «Закреплённых пакетных решениях»';
        // Страница «Закреплённые»: сняли закрепление — строка уходит со
        // страницы (и её развёртка). Признак — таблица .ai-pinned-table.
        var tr = btn.closest('tr');
        if (!data.pinned && tr && tr.closest('.ai-pinned-table')) {
          var detail = tr.nextElementSibling;
          if (detail && detail.classList.contains('ai-batch-detail')) { detail.remove(); }
          tr.remove();
        }
      })
      .catch(function (err) {
        window.alert('Закрепление: ' + (err.message || 'не удалось'));
      })
      .finally(function () { btn.classList.remove('busy'); });
  }

  // ── Развёртка результатов прогона ─────────────────────────────────────
  function toggleBatchRow(row) {
    var detailRow = row.nextElementSibling;
    if (!detailRow || !detailRow.classList.contains('ai-batch-detail')) { return; }
    var willOpen = detailRow.hidden;
    var body = detailRow.querySelector('.ai-batch-detail-body');
    if (willOpen && body && !body.dataset.rendered && !body.dataset.loading) {
      var url = row.getAttribute('data-snapshot-url');
      if (!url) { return; }
      body.dataset.loading = '1';
      body.innerHTML = '<p class="ai-recent-logs-empty">Загрузка результатов…</p>';
      fetch(url, { credentials: 'same-origin', headers: { 'Accept': 'application/json' } })
        .then(function (response) {
          if (!response.ok) { throw new Error('HTTP ' + response.status); }
          return response.json();
        })
        .then(function (snapshot) {
          body.innerHTML = '';
          var built = window.ArmBatchResults.init(
            body, snapshot, snapshot.run_id || row.getAttribute('data-run-id') || ''
          );
          body.dataset.rendered = '1';
          delete body.dataset.loading;
          // Живая развёртка: пока прогон идёт, цифры и раздел «Сбои по
          // причинам» обновляются сами (раз в 2 с). Перерисовка идёт по ТЕМ
          // ЖЕ карточкам — раскрытые модели не схлопываются (init не зовём
          // заново).
          var status = snapshot.run_status || '';
          if (built && built.reportEls && (status === 'running' || status === 'paused')) {
            window.ArmBatchResults.startLiveUpdates(
              {
                report: built.reportEls,
                resultsEl: built.resultsEl,
                resultsCard: built.resultsCard,
                filterHost: built.filterHost,
              },
              url,
              { intervalMs: 2000 }
            );
          }
        })
        .catch(function () {
          body.innerHTML = '<p class="ai-recent-logs-empty">Не удалось загрузить результаты прогона — попробуйте ещё раз.</p>';
          delete body.dataset.loading; // повторная попытка при следующем клике
        });
    }
    detailRow.hidden = !willOpen;
    row.classList.toggle('ai-batch-row-open', willOpen);
  }

  // ── Делегирование (работает и после replaceWith таблицы) ───────────────
  document.addEventListener('click', function (event) {
    var pinBtn = event.target.closest('.ai-log-pin-btn');
    if (pinBtn) {
      event.stopPropagation();
      togglePin(pinBtn);
      return;
    }
    var row = event.target.closest('.ai-batch-row-clickable');
    if (!row) { return; }
    if (event.target.closest('a')) { return; }     // «Детали →» — обычный переход
    if (event.target.closest('button')) { return; } // ★ — закрепление
    toggleBatchRow(row);
  });

  return { togglePin: togglePin, toggleBatchRow: toggleBatchRow };
})();
