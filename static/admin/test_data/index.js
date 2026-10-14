// Тестовые данные (страница «/ai/admin/test_data/»): POST emit_request через
// fetch — раньше форма улетала в скрытый iframe, ответ (redirect) гас в нём
// и кнопка выглядела мёртвой; теперь — статус прямо на странице (CSRF из form).
(function () {
  document.addEventListener('DOMContentLoaded', function () {
    var form = document.querySelector('form[data-emit-request]');
    var statusEl = document.getElementById('emit-status');
    if (!form) { return; }
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      var btn = form.querySelector('button[type="submit"]');
      var csrf = form.querySelector('input[name="csrfmiddlewaretoken"]');
      btn.disabled = true;
      if (statusEl) { statusEl.textContent = 'Отправка…'; statusEl.className = 'emit-status'; }
      fetch(form.getAttribute('action'), {
        method: 'POST',
        headers: { 'X-CSRFToken': (csrf && csrf.value) || '' },
        body: new FormData(form),
        credentials: 'same-origin',
      })
        .then(function (resp) {
          if (!(resp.status === 200 || resp.status === 302 || resp.ok)) {
            throw new Error('HTTP ' + resp.status);
          }
          if (statusEl) {
            statusEl.textContent = 'Запись добавлена: ' + new Date().toLocaleTimeString('ru-RU');
            statusEl.className = 'emit-status ok';
          }
        })
        .catch(function (err) {
          if (statusEl) {
            statusEl.textContent = 'Не удалось создать запись: ' + err.message;
            statusEl.className = 'emit-status error';
          }
        })
        .finally(function () { btn.disabled = false; });
    });
  });
})();