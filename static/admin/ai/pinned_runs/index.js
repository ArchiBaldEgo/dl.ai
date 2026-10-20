// Логика страницы «Закреплённые пакетные решения».
// Вынесена из ai/templates/admin/ai/pinned_runs.html.
// Шаблонных переменных здесь нет: развёртка результатов тянет window.ArmBatchResults
// (общий скрипт подключается ГЛОБАЛЬНО в admin/base_site.html — extrahead),
// адреса прогонов приходят в data-* атрибутах строк таблицы.
(function () {
  // Клик по строке → ленивая развёртка результатов (тот же приём, что на
  // «Настройке ИИ-приложения»: GET data-snapshot-url → ArmBatchResults).
  document.querySelectorAll('.ai-batch-row-clickable').forEach(function (row) {
    row.addEventListener('click', function (event) {
      if (event.target.closest('a')) { return; }
      if (event.target.closest('button')) { return; } // клик по ★ — открепление
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
            window.ArmBatchResults.init(
              body, snapshot, snapshot.run_id || row.getAttribute('data-run-id') || ''
            );
            body.dataset.rendered = '1';
            delete body.dataset.loading;
          })
          .catch(function () {
            body.innerHTML = '<p class="ai-recent-logs-empty">Не удалось загрузить результаты прогона — попробуйте ещё раз.</p>';
            delete body.dataset.loading;
          });
      }
      detailRow.hidden = !willOpen;
      row.classList.toggle('ai-batch-row-open', willOpen);
    });
  });

  // ★ (здесь — открепление): toggle + удаление строки со страницы при снятии.
  function pinCsrfToken() {
    var input = document.querySelector('input[name="csrfmiddlewaretoken"]');
    if (input && input.value) return input.value;
    var m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : '';
  }
  document.querySelectorAll('.ai-log-pin-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      if (btn.disabled || btn.classList.contains('busy')) return;
      btn.classList.add('busy');
      fetch(btn.getAttribute('data-pin-url'), {
        method: 'POST',
        headers: { 'X-CSRFToken': pinCsrfToken(), 'X-Requested-With': 'XMLHttpRequest' },
        credentials: 'same-origin',
      })
        .then(function (resp) {
          return resp.json().catch(function () { throw new Error('HTTP ' + resp.status); });
        })
        .then(function (data) {
          if (!data.ok) { throw new Error(data.message || 'Не удалось изменить закрепление'); }
          if (data.pinned) {
            btn.classList.add('ai-log-pin-btn-active');
            btn.textContent = '★';
            btn.title = 'Убрать из «Закреплённых»';
          } else {
            // Сняли закрепление — строка больше не относится к этой странице.
            var tr = btn.closest('tr');
            var detail = tr && tr.nextElementSibling;
            if (detail && detail.classList.contains('ai-batch-detail')) { detail.remove(); }
            if (tr) { tr.remove(); }
          }
        })
        .catch(function (err) {
          alert('Закрепление: ' + err.message);
        })
        .finally(function () { btn.classList.remove('busy'); });
    });
  });
})();
