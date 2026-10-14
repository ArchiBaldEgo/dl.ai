// =============================================================
// AI request logs admin — JS
// =============================================================
// Содержание:
//   1. Авто-применение фильтров (AJAX, debounce для текстовых полей)
//   2. Кнопка «Обновить таблицу» (AJAX, те же фильтры)
//   3. Модалка условия задачи + аккордеон строк
//   4. Rerun batch-solve (делегирование)
//   5. Pin/Unpin batch-прогона (делегирование)
//   6. «Показать все задачи» — ленивый batch-снапшот (делегирование)
// =============================================================

document.addEventListener('DOMContentLoaded', function () {

  // -----------------------------------------------------------
  // 1а. localStorage-персистентность фильтров (из main): рестор на загрузке,
  // сохранение при обновлении таблицы; «?reset=1» сбрасывает хранилище.
  // -----------------------------------------------------------
  const FILTER_KEY = 'ai_logs_filters';
  const FILTER_FIELDS = ['mode', 'model', 'user', 'date_from', 'date_to'];
  // DOM id суффикс для каждого фильтра (всё — 'id_<name>'). Устаревшие ключи
  // старых фильтров (q/id/status/task) в хранилище самоочищаются: рестор
  // читает только поля из FILTER_FIELDS, первый saveFilters перезаписывает.
  const domIdFor = (name) => 'id_' + name;

  function saveFilters() {
    if (!form) return;
    try {
      const data = {};
      FILTER_FIELDS.forEach(function (name) {
        const el = document.getElementById(domIdFor(name));
        if (el) { data[name] = el.value; }
      });
      localStorage.setItem(FILTER_KEY, JSON.stringify(data));
    } catch (e) { /* localStorage недоступен — не критично */ }
  }

  (function restoreSavedFilters() {
    const params = new URLSearchParams(window.location.search);
    // Явный «Сбросить» → стереть сохранённые фильтры и показать чистые.
    // Без этого рестор ниже тут же перe-applied бы сохранённое после перехода
    // по ссылке сброса.
    if (params.get('reset')) {
      try { localStorage.removeItem(FILTER_KEY); } catch (e) {} // ignore
      return;
    }
    const hasFilters = FILTER_FIELDS.some(f => params.has(f) && params.get(f));
    if (form && !hasFilters) {
      try {
        const saved = JSON.parse(localStorage.getItem(FILTER_KEY) || '{}');
        let restored = false;
        FILTER_FIELDS.forEach(function (name) {
          const el = document.getElementById(domIdFor(name));
          if (el && saved[name]) { el.value = saved[name]; restored = true; }
        });
        if (restored) {
          form.submit();  // полный переход: в URL появятся фильтры
          return;
        }
      } catch (e) {}
    }
  })();

  // -----------------------------------------------------------
  // 1. Авто-применение фильтров и кнопка «Обновить таблицу»
  // -----------------------------------------------------------
  const form = document.querySelector('.ai-logs-filters');
  const WRAPPER_SEL = '#logs-table-wrapper';
  const TEXT_DEBOUNCE_MS = 350;

  // Параметры берём из формы, а не из URL: при смене select/даты
  // в URL ещё старые значения, а в форме — уже новые.
  function buildParams() {
    const params = new URLSearchParams();
    new FormData(form).forEach((value, key) => {
      const v = String(value ?? '').trim();
      if (v !== '') params.append(key, v);
    });
    return params;
  }
  let inflight = null;

  async function refreshTable() {
    if (!form) return;

    // Отменяем предыдущий незавершённый запрос: при быстром наборе
    // букв в «Пользователе» ответы могут прийти вразнобой, и в DOM
    // окажется устаревший.
    if (inflight) inflight.abort();
    inflight = new AbortController();

    const url = new URL(window.location.pathname, window.location.origin);
    url.search = buildParams().toString();

    try {
      const resp = await fetch(url, {
        headers: { 'X-Requested-With': 'XMLHttpRequest' },
        signal: inflight.signal,
      });
      if (!resp.ok) throw new Error('HTTP ' + resp.status);

      const html = await resp.text();
      const doc = new DOMParser().parseFromString(html, 'text/html');
      const fresh = doc.querySelector(WRAPPER_SEL);
      if (!fresh) throw new Error('Таблица не найдена в ответе');

      document.querySelector(WRAPPER_SEL).replaceWith(fresh);

      // Обновляем адресную строку: F5 и «поделиться ссылкой» сохранят фильтры.
      history.replaceState(null, '', url.toString());
      saveFilters();  // автоприменение тоже должно запоминаться (раньше сохраняло только submit)
    } catch (err) {
      if (err.name === 'AbortError') return;
      console.error('refresh table failed:', err);
      alert('Не удалось обновить таблицу: ' + err.message);
    } finally {
      inflight = null;
    }
  }

  if (form) {
    // Текстовые поля → debounce (не гоняем запрос на каждую букву).
    form.querySelectorAll('input[type="text"], input[type="search"]').forEach((el) => {
      let t = null;
      el.addEventListener('input', () => {
        clearTimeout(t);
        t = setTimeout(refreshTable, TEXT_DEBOUNCE_MS);
      });
      // Enter — применить немедленно.
      el.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
          e.preventDefault();
          clearTimeout(t);
          refreshTable();
        }
      });
    });

    // select / date → применяем сразу.
    form.querySelectorAll('select, input[type="date"]').forEach((el) => {
      el.addEventListener('change', refreshTable);
    });

    // Enter в любом месте формы — не навигировать, а применить AJAX.
    form.addEventListener('submit', (e) => {
      e.preventDefault();
      refreshTable();
    });
  }

  // Кнопка «Обновить таблицу» — тот же refreshTable() с текущими фильтрами.
  const refreshBtn = document.getElementById('ai-logs-table-update-button');
  if (refreshBtn) {
    refreshBtn.addEventListener('click', (e) => {
      e.preventDefault();
      refreshTable();
    });
  }

  // -----------------------------------------------------------
  // 2. Модалка условия задачи + аккордеон строк
  // -----------------------------------------------------------
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
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
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

  // Делегирование: работает и после replaceWith таблицы.
  document.addEventListener('click', function (event) {
    // Модалка условия задачи.
    const taskLink = event.target.closest('.ai-log-task-link');
    if (taskLink) {
      event.stopPropagation();
      event.preventDefault();
      openModal(taskLink.dataset.taskNode);
      return;
    }
    if (modal && event.target === modal) { closeModal(); return; }
    if (event.target.closest('.ai-task-modal-close')) { closeModal(); return; }

    // Аккордеон строки: клик по строке раскрывает/сворачивает детали.
    const row = event.target.closest('.ai-log-row');
    if (!row) return;
    if (event.target.tagName === 'A') return;
    if (event.target.tagName === 'BUTTON') return;
    if (event.target.closest('.ai-log-detail-row')) return;

    const detailRow = row.nextElementSibling;
    if (!detailRow || !detailRow.classList.contains('ai-log-detail-row')) return;

    const isOpen = detailRow.classList.contains('open');
    document.querySelectorAll('.ai-log-detail-row.open').forEach(function (r) {
      r.classList.remove('open');
    });
    if (!isOpen) detailRow.classList.add('open');
  });

  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape') closeModal();
  });

  // -----------------------------------------------------------
  // 3. Rerun batch-solve — делегирование, работает после replaceWith
  // -----------------------------------------------------------
  document.addEventListener('submit', function (event) {
    const rerunForm = event.target.closest('.ai-batch-rerun-form');
    if (!rerunForm) return;
    event.preventDefault();

    const statusEl = rerunForm.querySelector('.ai-batch-rerun-status');
    const btn = rerunForm.querySelector('button[type="submit"]');
    const csrfInput = rerunForm.querySelector('input[name="csrfmiddlewaretoken"]');
    btn.disabled = true;
    btn.textContent = 'Отправка...';
    statusEl.textContent = '';

    // getAttribute, а НЕ form.action: именованное поле формы с тем же именем
    // затенило бы свойство DOM.
    fetch(rerunForm.getAttribute('action'), {
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

  // -----------------------------------------------------------
  // 4. Pin/Unpin — делегирование
  // -----------------------------------------------------------
  function pinCsrfToken() {
    var input = document.querySelector('input[name="csrfmiddlewaretoken"]');
    if (input && input.value) return input.value;
    var m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : '';
  }

  document.addEventListener('click', function (event) {
    const btn = event.target.closest('.ai-log-pin-btn');
    if (!btn) return;
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
        if (!data.ok) throw new Error(data.message || 'Не удалось изменить закрепление');
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

  // -----------------------------------------------------------
  // 6. «Показать все задачи»: ленивый загруз batch-снапшота (решённые И
  //    нерешённые задачи с пометками моделей). Делегирование — после
  //    replaceWith таблицы исходные биндинги мертвы. Рендер только
  //    createElement+textContent — ответ может нести текст модели.
  // -----------------------------------------------------------
  document.addEventListener('click', function (event) {
    const btn = event.target.closest('.ai-log-all-tasks-btn');
    if (!btn || btn.disabled) return;
    const list = btn.parentElement.querySelector('.ai-log-all-tasks-list');
    if (!list) return;
    event.stopPropagation();

    if (!list.hidden) { list.hidden = true; btn.textContent = 'Показать все задачи'; return; }
    if (list.dataset.loaded === '1') { list.hidden = false; btn.textContent = 'Скрыть задачи'; return; }
    btn.disabled = true;
    btn.textContent = 'Загрузка…';
    fetch(btn.getAttribute('data-snapshot-url'), {
      method: 'GET',
      credentials: 'same-origin',
    })
      .then(function (resp) {
        return resp.json().catch(function () { throw new Error('HTTP ' + resp.status); });
      })
      .then(function (snap) {
        if (snap && snap.error) { throw new Error('Прогон не найден'); }
        list.textContent = '';
        (snap.tasks || []).forEach(function (t) {
          const line = document.createElement('div');
          line.className = 'ai-alltask';
          const mark = document.createElement('span');
          mark.className = t.solved ? 'ai-alltask-solved' : 'ai-alltask-failed';
          const solvedCount = (t.solved_models || []).length;
          const totalCount = (t.total_models || []).length;
          mark.textContent = t.solved
            ? '✓ решено ' + solvedCount + '/' + totalCount
            : '✗ решено ' + solvedCount + '/' + totalCount;
          const link = document.createElement('a');
          if (t.dl_url) { link.href = t.dl_url; link.target = '_blank'; link.rel = 'noopener'; }
          link.textContent = t.name + ' (' + t.node_id + ')';
          line.appendChild(mark);
          line.appendChild(link);
          list.appendChild(line);
        });
        if (!(snap.tasks || []).length) {
          const empty = document.createElement('div');
          empty.textContent = '—';
          list.appendChild(empty);
        }
        list.dataset.loaded = '1';
        list.hidden = false;
        btn.disabled = false;
        btn.textContent = 'Скрыть задачи';
      })
      .catch(function (err) {
        btn.disabled = false;
        btn.textContent = 'Показать все задачи';
        const msg = document.createElement('div');
        msg.className = 'ai-log-all-tasks-error';
        msg.textContent = 'Не удалось загрузить задачи: ' + err.message;
        // Перезагружаем содержимое блока ошибки при каждом клике.
        list.textContent = '';
        list.appendChild(msg);
        list.hidden = false;
      });
  });

});