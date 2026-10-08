const FILTER_KEY = 'ai_logs_filters';
  const FILTER_FIELDS = ['q', 'id', 'status', 'mode', 'model', 'user', 'task', 'date_from', 'date_to'];
  // DOM id suffix per filter name (most are 'id_<name>'; the record-id field
  // uses 'id_record_id' to avoid the awkward 'id_id').
  const FILTER_DOM_ID = { id: 'id_record_id' };
  const domIdFor = (name) => FILTER_DOM_ID[name] || ('id_' + name);

  // Restore filters from localStorage on page load (unless a hard reset was requested).
  document.addEventListener('DOMContentLoaded', function () {
    const params = new URLSearchParams(window.location.search);

    // Explicit "Сбросить" → drop saved filters and load clean. Without this the
    // localStorage auto-restore below would immediately re-apply the saved filters
    // right after the reset link navigated to a clean URL.
    if (params.get('reset')) {
      localStorage.removeItem(FILTER_KEY);
      return;
    }

    const hasFilters = FILTER_FIELDS.some(f => params.has(f) && params.get(f));
    if (!hasFilters) {
      try {
        const saved = JSON.parse(localStorage.getItem(FILTER_KEY) || '{}');
        let restored = false;
        FILTER_FIELDS.forEach(function (name) {
          const el = document.getElementById(domIdFor(name));
          if (el && saved[name]) { el.value = saved[name]; restored = true; }
        });
        if (restored) {
          document.querySelector('.ai-logs-filters').submit();
          return;
        }
      } catch (e) {}
    }
    const form = document.querySelector('.ai-logs-filters');
    if (form) {
      form.addEventListener('submit', function () {
        const data = {};
        FILTER_FIELDS.forEach(function (name) {
          const el = document.getElementById(domIdFor(name));
          if (el) data[name] = el.value;
        });
        localStorage.setItem(FILTER_KEY, JSON.stringify(data));
      });
    }
  });

  // Accordion toggle + task-text modal.
  // Разметка модалки находится НИЖЕ этого <script> в шаблоне, поэтому поиск
  // элементов нужно делать после разбора DOM — иначе getElementById вернёт null,
  // openModal молча выйдет и клик по задаче ничего не сделает.
  document.addEventListener('DOMContentLoaded', function () {
    const modal = document.getElementById('aiTaskModal');
    const modalTitle = document.getElementById('aiTaskModalTitle');
    const modalBody = document.getElementById('aiTaskModalBody');
    const modalStatus = document.getElementById('aiTaskModalStatus');

    function openModal(nodeId) {
      if (!modal) return;
      modal.classList.add('open');
      if (modalTitle) modalTitle.textContent = 'Задача #' + nodeId;
      if (modalBody) modalBody.textContent = '';
      if (modalStatus) {
        modalStatus.textContent = 'Загрузка условия задачи…';
        modalStatus.classList.remove('error');
      }
      fetch('/ai/admin/ai/request_logs/task-text/?node_id=' + encodeURIComponent(nodeId))
        .then(function (r) { return r.json().then(function (d) { return {ok: r.ok, d: d}; }); })
        .then(function (res) {
          const d = res.d || {};
          if (d.ok) {
            if (modalTitle) modalTitle.textContent = d.name || ('Задача #' + nodeId);
            // Security: statement may contain HTML/think-blocks — never innerHTML.
            if (modalBody) modalBody.textContent = d.statement || '';
            if (modalStatus) modalStatus.textContent = d.source === 'dl'
              ? 'Из DL (live)' : 'Из локального кэша';
          } else {
            if (modalBody) modalBody.textContent = '';
            if (modalStatus) {
              modalStatus.textContent = d.error || 'Не удалось загрузить условие';
              modalStatus.classList.add('error');
            }
          }
        })
        .catch(function () {
          if (modalStatus) {
            modalStatus.textContent = 'Сетевая ошибка при загрузке условия';
            modalStatus.classList.add('error');
          }
        });
    }

    function closeModal() {
      if (modal) modal.classList.remove('open');
    }

    document.addEventListener('click', function (event) {
      // Task-text modal trigger.
      const taskLink = event.target.closest('.ai-log-task-link');
      if (taskLink) {
        event.stopPropagation();
        event.preventDefault();
        openModal(taskLink.dataset.taskNode);
        return;
      }
      if (modal && event.target === modal) { closeModal(); return; }
      if (event.target.closest('.ai-task-modal-close')) { closeModal(); return; }

      // Accordion toggle — click on row to expand/collapse detail.
      const row = event.target.closest('.ai-log-row');
      if (!row) return;
      if (event.target.tagName === 'A') return;  // don't toggle on links
      if (event.target.tagName === 'BUTTON') return;  // don't toggle on the task button
      if (event.target.closest('.ai-log-detail-row')) return;  // don't toggle from detail

      const detailRow = row.nextElementSibling;
      if (!detailRow || !detailRow.classList.contains('ai-log-detail-row')) return;

      const isOpen = detailRow.classList.contains('open');
      document.querySelectorAll('.ai-log-detail-row.open').forEach(function (r) {
        r.classList.remove('open');
      });
      if (!isOpen) {
        detailRow.classList.add('open');
      }
    });

    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape') closeModal();
    });

    // Перезапуск batch-solve прогона из строки журнала: rerun-arm — JSON
    // endpoint, поэтому POST идёт через fetch, затем переход на страницу
    // нового прогона (как в детали записи).
    document.querySelectorAll('.ai-batch-rerun-form').forEach(function (form) {
      form.addEventListener('submit', function (event) {
        event.preventDefault();
        const statusEl = form.querySelector('.ai-batch-rerun-status');
        const btn = form.querySelector('button[type="submit"]');
        const csrfInput = form.querySelector('input[name="csrfmiddlewaretoken"]');
        btn.disabled = true;
        btn.textContent = 'Отправка...';
        statusEl.textContent = '';
        // getAttribute, а НЕ form.action: именованное поле формы с тем же именем
        // затенило бы свойство DOM (как в pdForm с <input name="action">).
        fetch(form.getAttribute('action'), {
          method: 'POST',
          headers: {
            'X-CSRFToken': (csrfInput && csrfInput.value) || '',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({}),
          credentials: 'same-origin',
        })
          .then(function (resp) { return resp.json(); })
          .then(function (data) {
            if (data.run_id) {
              statusEl.textContent = 'Перезапуск ARM...';
              window.location.href = '/ai/admin/arm/solve/?run_id=' + encodeURIComponent(data.run_id);
            } else {
              btn.disabled = false;
              btn.textContent = '↻ Повторить запрос';
              statusEl.textContent = 'Ошибка: ' + (data.error || 'Неизвестная ошибка');
            }
          })
          .catch(function (err) {
            btn.disabled = false;
            btn.textContent = '↻ Повторить запрос';
            statusEl.textContent = 'Сетевая ошибка: ' + err.message;
          });
      });
    });

    // ★ Закрепить/открепить batch-прогон из журнала (POST toggle,
    // ai/admin/pinned.py). Кнопка вне форм — CSRF берём из скрытых input-ов
    // страницы (rerun-формы), с фолбэком на куку csrftoken.
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
            btn.classList.toggle('ai-log-pin-btn-active', data.pinned);
            btn.textContent = data.pinned ? '★' : '☆';
            btn.title = data.pinned
              ? 'Убрать из «Закреплённых»'
              : 'Закрепить в «Закреплённых пакетных решениях»';
          })
          .catch(function (err) {
            alert('Закрепление: ' + err.message);
          })
          .finally(function () { btn.classList.remove('busy'); });
      });
    });
  });