// Логика страницы «Состояние моделей»: обновление статусов моделей
// (кнопка «Обновить состояние моделей») + авто-обновление таблицы раз в 8 с
// (раз в 2 с, пока идёт обновление).
// Вынесена из ai/templates/admin/ai/model_status.html.
// Шаблонных переменных здесь нет: адреса эндпоинтов и признак того,
// что обновление уже идёт, приходят одним json_script-блоком
// model-status-config, который рендерит шаблон ДО подключения этого файла.
  (function () {
    const form = document.getElementById('refreshModelsForm');
    const button = document.getElementById('refreshModelsBtn');
    const message = document.getElementById('refreshAsyncMessage');
    const rowsBody = document.getElementById('modelStatusRowsBody');
    const healthWindowDate = document.getElementById('statusHealthWindowDate');
    const inProgressHint = document.getElementById('refreshInProgressHint');
    const progressLine = document.getElementById('refreshProgressLine');
    const progressFill = document.getElementById('msProgressFill');
    const progressText = document.getElementById('msProgressText');

    if (!form || !button || !message || !rowsBody || !healthWindowDate || !inProgressHint) {
      return;
    }

    const csrfInput = form.querySelector('input[name="csrfmiddlewaretoken"]');
    if (!csrfInput) {
      return;
    }

    let pollTimer = null;
    // Конфиг приходит из шаблона тремя json_script-блоками —
    // в .js-файле шаблонных переменных быть не может.
    function readJsonScript(id, fallback) {
      var el = document.getElementById(id);
      if (!el) { return fallback; }
      try { var v = JSON.parse(el.textContent); return v === null ? fallback : v; }
      catch (e) { return fallback; }
    }
    const stateUrl = readJsonScript('model-status-state-url', '');
    const refreshUrl = readJsonScript('model-status-refresh-url', '');
    let refreshInProgress = !!readJsonScript('model-status-refresh-in-progress', false);

    function setMessage(text, isError) {
      message.textContent = text || '';
      message.className = isError ? 'status-error' : 'status-success';
    }

    function setInProgressHint(isVisible) {
      inProgressHint.style.display = isVisible ? 'block' : 'none';
    }

    // AJAX админки всегда ждёт JSON; HTML (например, редирект на логин при
    // протухшей сессии) даёт сырую ошибку JSON.parse — показываем внятный текст.
    async function parseJsonResponse(response) {
      const contentType = response.headers.get('content-type') || '';
      if (contentType.indexOf('application/json') === -1) {
        throw new Error('Сессия истекла или сервер вернул неожиданный ответ. Обновите страницу и войдите заново.');
      }
      return response.json();
    }

    // Бар прогресса sweep'а: N / M · P% | Сейчас: <модель>.
    function renderRefreshProgress(progress) {
      if (!progressLine || !progressFill || !progressText) return;
      const total = Number((progress && progress.total) || 0);
      const completed = Number((progress && progress.completed) || 0);
      if (!progress || !total) {
        progressLine.hidden = true;
        return;
      }
      const percent = Math.min(100, Math.round((completed / total) * 100));
      progressFill.style.width = percent + '%';
      const current = progress.current ? ' | Сейчас: ' + progress.current : '';
      progressText.textContent = completed + ' / ' + total + ' · ' + percent + '%' + current;
      progressLine.hidden = false;
    }

    function escapeHtml(value) {
      return String(value || '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
    }

    function getCookie(name) {
      const cookieValue = document.cookie
        .split('; ')
        .find(function (row) { return row.startsWith(name + '='); });
      return cookieValue ? decodeURIComponent(cookieValue.split('=').slice(1).join('=')) : '';
    }

    function getCsrfToken() {
      return getCookie('csrftoken') || csrfInput.value;
    }

    function renderStatusRows(rows) {
      rowsBody.innerHTML = '';

      (rows || []).forEach(function (row) {
        const tr = document.createElement('tr');
        const statusBadge = row.is_active
          ? '<span class="badge up">Активна</span>'
          : '<span class="badge down">Неактивна</span>';

        const windowDateCell = row.window_date
          ? `${escapeHtml(row.window_date)}${row.is_current_window ? '' : '<div class="quiet">данные из предыдущего окна</div>'}`
          : '-';

        const caps = row.capabilities || {};
        const capLabels = { text: 'Текст', vision: 'Зрение', reasoning: 'Рассуждения' };
        const capCell = ['text', 'vision', 'reasoning']
          .map(function (k) {
            const on = !!caps[k];
            const label = capLabels[k] || k;
            return `<span class="cap ${on ? 'cap-on' : 'cap-off'}">${label}</span>`;
          })
          .join(' ');

        tr.innerHTML = [
          `<td><div>${escapeHtml(row.title)}</div><div class="quiet">${escapeHtml(row.key)}</div></td>`,
          `<td>${statusBadge}</td>`,
          `<td class="cap-cell">${capCell}</td>`,
          `<td>${row.last_http_code !== null && row.last_http_code !== undefined ? escapeHtml(String(row.last_http_code)) : '-'}</td>`,
          `<td>${escapeHtml(row.last_http_code_label || '—')}</td>`,
          `<td>${windowDateCell}</td>`,
          `<td>${row.checked_at_msk ? escapeHtml(row.checked_at_msk) : '-'}</td>`,
        ].join('');

        rowsBody.appendChild(tr);
      });
    }

    function schedulePoll(delayMs) {
      if (pollTimer) {
        window.clearTimeout(pollTimer);
      }

      pollTimer = window.setTimeout(fetchState, delayMs);
    }

    async function fetchState() {
      try {
        const response = await fetch(stateUrl, {
          method: 'GET',
          credentials: 'same-origin',
          headers: {
            'X-Requested-With': 'XMLHttpRequest',
          },
        });

        const data = await parseJsonResponse(response);
        if (!response.ok || !data.ok) {
          throw new Error(data.message || 'Не удалось получить состояние моделей');
        }

        if (data.health_window_date) {
          healthWindowDate.textContent = data.health_window_date;
        }

        renderStatusRows(data.model_status_rows || []);

        const nextRefreshInProgress = Boolean(data.refresh_in_progress);
        setInProgressHint(nextRefreshInProgress);
        renderRefreshProgress(nextRefreshInProgress ? (data.refresh_progress || null) : null);

        if (refreshInProgress && !nextRefreshInProgress) {
          setMessage('Обновление завершено. Таблица обновлена.', false);
        }

        refreshInProgress = nextRefreshInProgress;
        schedulePoll(refreshInProgress ? 2000 : 8000);
      } catch (error) {
        setMessage(error.message || 'Ошибка обновления статусов', true);
        schedulePoll(8000);
      }
    }

    // Формы на странице — без action и без собственного сабмита: запрос
    // уходит fetch'ем по кнопке. Раньше это гасил инлайновый
    // onsubmit="return false;" в разметке; теперь гасим здесь (в т.ч. Enter
    // в поле — иначе нативный POST ушёл бы на текущий URL).
    form.addEventListener('submit', function (event) {
      event.preventDefault();
    });

    button.addEventListener('click', async function () {
      if (button.disabled) {
        return;
      }

      button.disabled = true;
      setMessage('Запускаем обновление состояния моделей...', false);

      try {
        const response = await fetch(refreshUrl, {
          method: 'POST',
          credentials: 'same-origin',
          headers: {
            'X-CSRFToken': getCsrfToken(),
            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
            'X-Requested-With': 'XMLHttpRequest',
          },
          body: 'action=refresh_models',
        });

        const data = await parseJsonResponse(response);
        if (!response.ok || !data.ok) {
          throw new Error(data.message || 'Не удалось запустить обновление');
        }

        setMessage(data.message || 'Обновление запущено', false);
        refreshInProgress = Boolean(data.refresh_in_progress);
        setInProgressHint(refreshInProgress);
        // Обновление моделей — глобальный процесс: даём меню «Процессы» в шапке
        // узнать о нём сразу, не дожидаясь планового 15-секундного поллинга.
        document.dispatchEvent(new Event('ai-processes:refresh'));
        fetchState();
      } catch (error) {
        setMessage(error.message || 'Ошибка запуска обновления', true);
      } finally {
        button.disabled = false;
      }
    });

    fetchState();
  })();
