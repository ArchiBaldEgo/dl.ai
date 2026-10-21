// Логика страницы «Поиск ошибки» (ARM): выбор языка/темы, запуск прогона,
// поллинг статуса, разбор ответа модели.
//
// Вынесена из ai/templates/admin/ai/arm_find_error.html.
// Шаблонных переменных здесь нет: значения приходят через json_script-блоки
// arm-find-error-*, данные — через langs-data / topics-data /
// arm-find-error-bindings / arm-initial-run-data (их рендерит шаблон
// ДО подключения этого файла).
  (function () {
    // Значения из json_script-блоков шаблона (id → значение).
    function readJsonScript(id, fallback) {
      var el = document.getElementById(id);
      if (!el) { return fallback; }
      try { var v = JSON.parse(el.textContent || 'null'); return v === null ? fallback : v; }
      catch (e) { return fallback; }
    }
    const statusUrl = readJsonScript('arm-find-error-status-url', '');
    const startUrl = readJsonScript('arm-find-error-start-url', '');
    const modelStatusRefreshUrl = readJsonScript('arm-model-status-refresh-url', '');
    const langSelect = document.getElementById('programming_language');
    const topicSelect = document.getElementById('topic');

    // Значения приходят из шаблона через json_script-блоки
    // (arm-find-error-selected-lang / arm-find-error-selected-topic) —
    // в .js-файле шаблонных переменных быть не может.
    const selectedLang = readJsonScript('arm-find-error-selected-lang', '');
    const selectedTopic = readJsonScript('arm-find-error-selected-topic', '');

    const languages = JSON.parse(document.getElementById('langs-data').textContent || '[]');
    let topics = JSON.parse(document.getElementById('topics-data').textContent || '[]');
    // Привязки «промпт по умолчанию» (ArmPromptBinding): ручной выбор
    // промпта убран — привязка (mode=find_error) резолвится серверно по
    // языку и теме; нет привязки — прогон без промпта. Клиент показывает
    // только подпись о том, что применится.
    const promptBindings = JSON.parse(
      document.getElementById('arm-find-error-bindings').textContent || '[]'
    ) || [];
    const boundPromptLabel = document.getElementById('armBoundPromptLabel');

    // Резолв как на сервере (ArmPromptBinding.resolve): точная (язык+тема),
    // иначе привязка «на весь язык» (topic_id === null — языки без тем).
    function findBoundPromptName(languageId, topicId) {
      const exact = promptBindings.find(function (item) {
        return String(item.language_id) === String(languageId)
          && item.topic_id !== null && String(item.topic_id) === String(topicId)
          && item.mode === 'find_error';
      });
      const binding = exact || promptBindings.find(function (item) {
        return String(item.language_id) === String(languageId)
          && item.topic_id === null && item.mode === 'find_error';
      });
      return binding ? (binding.prompt_name || ('Prompt #' + binding.prompt_id)) : '';
    }

    // Подпись под темой: какой «промпт по умолчанию» подтянется из
    // привязки — чтобы сразу видеть, что применилось (или что прогона
    // не будет с промптом). Тема необязательна: без темы срабатывает
    // привязка «на весь язык».
    function updateBoundPromptLabel() {
      if (!boundPromptLabel) { return; }
      const name = langSelect.value
        ? findBoundPromptName(langSelect.value, topicSelect.value)
        : '';
      if (!name) {
        boundPromptLabel.style.display = 'none';
        boundPromptLabel.textContent = '';
        return;
      }
      boundPromptLabel.style.display = '';
      boundPromptLabel.textContent = 'Промпт по умолчанию: ' + name;
    }

    function fillLanguages() {
      langSelect.innerHTML = '<option value="">Выберите язык</option>';
      languages.forEach(function (item) {
        const option = document.createElement('option');
        option.value = String(item.id);
        option.textContent = item.language_name;
        if (selectedLang && String(item.id) === selectedLang) {
          option.selected = true;
        }
        langSelect.appendChild(option);
      });
    }

    function fillTopics(languageId) {
      topicSelect.innerHTML = '<option value="">Выберите тему</option>';
      if (!languageId) {
        return;
      }

      topics
        .filter(function (item) { return String(item.programming_language) === String(languageId); })
        .forEach(function (item) {
          const option = document.createElement('option');
          option.value = String(item.id);
          option.textContent = item.name || item.topic_name;
          if (selectedTopic && String(item.id) === selectedTopic) {
            option.selected = true;
          }
          topicSelect.appendChild(option);
        });
    }

    langSelect.addEventListener('change', function () {
      fillTopics(langSelect.value);
      updateBoundPromptLabel();
    });

    topicSelect.addEventListener('change', function () {
      updateBoundPromptLabel();
    });

    const interfaceLangSelect = document.getElementById('interface_language');
    async function reloadProblemData() {
      try {
        const uiLang = interfaceLangSelect.value;
        const response = await fetch(`/ai/api/problem-data/?ui_language=${encodeURIComponent(uiLang)}`);
        if (!response.ok) throw new Error('HTTP error');
        const data = await response.json();
        topics = data.topics || [];
        fillLanguages();
        fillTopics(langSelect.value);
        updateBoundPromptLabel();
      } catch (error) {
        console.error('Failed to reload problem data:', error);
      }
    }
    if (interfaceLangSelect) {
      interfaceLangSelect.addEventListener('change', reloadProblemData);
    }

    fillLanguages();
    fillTopics(langSelect.value);
    updateBoundPromptLabel();
  })();

  function openArmModal(card) {
    const titleEl = card.querySelector('strong');
    const rawEl = card.querySelector('.arm-full-raw');
    const modal = document.getElementById('armModal');
    const modalTitle = document.getElementById('armModalTitle');
    const modalBody = document.getElementById('armModalBody');
    if (!modal || !modalTitle || !modalBody) {
      return;
    }

    modalTitle.textContent = titleEl ? titleEl.textContent : 'Полный ответ модели';
    modalBody.textContent = rawEl ? rawEl.textContent : 'Нет полного ответа.';
    modal.classList.add('open');
  }

  function closeArmModal() {
    const modal = document.getElementById('armModal');
    if (modal) {
      modal.classList.remove('open');
    }
  }

  function closeArmModalOnBackdrop(event) {
    if (event.target && event.target.id === 'armModal') {
      closeArmModal();
    }
  }

  // Привязка обработчиков модалки. Раньше — инлайновые onclick в разметке
  // (правило проекта: в шаблонах только разметка, весь JS — в файлах).
  // Крестик закрытия — по классу .arm-modal-close, подложка — сам #armModal.
  document.addEventListener('click', function (event) {
    var target = event.target;
    if (!target || !target.closest) return;
    if (target.closest('.arm-modal-close')) {
      closeArmModal();
      return;
    }
    if (target.id === 'armModal') {
      closeArmModalOnBackdrop(event);
    }
  });

  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape') {
      closeArmModal();
    }
  });

  // AJAX админки всегда ждёт JSON; HTML (например, редирект на страницу
  // логина при протухшей сессии — fetch молча следует за 302) даёт сырую
  // ошибку JSON.parse. Показываем внятный текст вместо неё. Объявлен в
  // общем scope: отсюда его видят оба вложенных IIFE (форма прогона и
  // кнопка обновления моделей).
  async function parseJsonResponse(response) {
    const contentType = response.headers.get('content-type') || '';
    if (contentType.indexOf('application/json') === -1) {
      throw new Error('Сессия истекла или сервер вернул неожиданный ответ. Обновите страницу и войдите заново.');
    }
    return response.json();
  }

  (function () {
    const runForm = document.getElementById('armRunForm');
    const submitButton = document.getElementById('armRunSubmitBtn');
    const runError = document.getElementById('armRunError');
    const runProgress = document.getElementById('armRunProgress');
    const reportCard = document.getElementById('armReportCard');
    const reportList = document.getElementById('armReportList');
    const resultsCard = document.getElementById('armResultsCard');
    const resultsList = document.getElementById('armResultsList');
    const initialRunRaw = document.getElementById('arm-initial-run-data');
    const csrfInput = runForm ? runForm.querySelector('input[name="csrfmiddlewaretoken"]') : null;

    if (!runForm || !submitButton || !runError || !runProgress || !reportCard || !reportList || !resultsCard || !resultsList || !csrfInput) {
      return;
    }

    let pollTimer = null;
    let currentRunId = readJsonScript('arm-find-error-active-run-id', '');
    let initialRun = {};
    try {
      initialRun = initialRunRaw ? JSON.parse(initialRunRaw.textContent || '{}') : {};
    } catch (error) {
      initialRun = {};
    }

    function setSubmitDisabled(isDisabled) {
      submitButton.disabled = Boolean(isDisabled);
      submitButton.textContent = isDisabled ? 'Проверяем модели...' : 'Запустить отчет';
      submitButton.setAttribute('aria-busy', isDisabled ? 'true' : 'false');
    }

    function setRunError(text) {
      if (!text) {
        runError.textContent = '';
        runError.style.display = 'none';
        return;
      }

      runError.textContent = text;
      runError.style.display = 'block';
    }

    function setRunProgress(text) {
      const progressText = runProgress.querySelector('.arm-progress-text');
      if (!text) {
        if (progressText) {
          progressText.textContent = '';
        } else {
          runProgress.textContent = '';
        }
        runProgress.style.display = 'none';
        runProgress.classList.remove('active');
        return;
      }

      if (progressText) {
        progressText.textContent = text;
      } else {
        runProgress.textContent = text;
      }
      runProgress.style.display = 'flex';
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

    function renderReport(report) {
      if (!report) {
        reportList.innerHTML = '';
        reportCard.style.display = 'none';
        const summaryWrap = document.getElementById('armSummaryWrap');
        if (summaryWrap) summaryWrap.innerHTML = '';
        return;
      }

      const fastestDuration = Number(report.fastest_duration || 0).toFixed(2);
      reportList.innerHTML = [
        `Всего моделей: ${Number(report.models_total || 0)}`,
        `Успешно: ${Number(report.success_count || 0)}`,
        `С ошибкой: ${Number(report.error_count || 0)}`,
        `Суммарно токенов: ${Number(report.tokens_total || 0)}`,
        `Самая быстрая: ${report.fastest_model || '-'} (${fastestDuration} сек)`,
      ].map(function (line) { return `<li>${escapeHtml(line)}</li>`; }).join('');
      renderSummary(report.summary || []);
      reportCard.style.display = 'block';
    }

    function renderSummary(summary) {
      const wrap = document.getElementById('armSummaryWrap');
      if (!wrap) return;
      if (!Array.isArray(summary) || summary.length === 0) {
        wrap.innerHTML = '';
        return;
      }
      const head = '<thead><tr><th>Модель</th><th>% решённых</th><th>Среднее время, сек</th><th>Токены</th><th>Статус</th></tr></thead>';
      const body = summary.map(function (row) {
        return '<tr>'
          + `<td>${escapeHtml(row.model_title || '')}</td>`
          + `<td>${escapeHtml(String(row.percent_solved))}</td>`
          + `<td>${escapeHtml(String(row.avg_duration))}</td>`
          + `<td>${Number(row.tokens || 0)}</td>`
          + `<td>${escapeHtml((row.solved || 0) + '/' + (row.total || 0))}</td>`
          + '</tr>';
      }).join('');
      wrap.innerHTML = '<h3>Итоговая таблица по моделям</h3>'
        + '<p class="arm-section-note">Сортировка: процент решённых ↓, среднее время ответа ↑</p>'
        + `<table class="arm-summary-table">${head}<tbody>${body}</tbody></table>`;
    }

    function renderResults(results) {
      if (!Array.isArray(results) || results.length === 0) {
        resultsList.innerHTML = '';
        resultsCard.style.display = 'none';
        return;
      }

      resultsList.innerHTML = '';
      results.forEach(function (item) {
        const status = item.status === 'ok' ? 'ok' : 'error';
        const card = document.createElement('details');
        card.className = `arm-result ${status}`;
        card.innerHTML = [
          '<summary>',
          '<div class="arm-result-top">',
          `<span class="arm-result-title">${escapeHtml(item.model_title || item.model_key || '-')}</span>`,
          `<span class="arm-badge ${status}">${status === 'ok' ? 'Успешно' : 'Ошибка'}</span>`,
          '</div>',
          `<div class="arm-meta">Время: ${escapeHtml(item.duration)} сек | Токены: ${escapeHtml(item.tokens)}</div>`,
          `<div class="arm-short-response">${escapeHtml(item.short_response || '')}</div>`,
          '</summary>',
          '<div class="arm-result-body"><pre></pre><div class="arm-full-raw" hidden></div></div>',
        ].join('');

        const raw = card.querySelector('.arm-full-raw');
        if (raw) {
          raw.textContent = item.raw_response || '';
        }
        const pre = card.querySelector('pre');
        if (pre) {
          pre.textContent = item.raw_response || 'Нет полного ответа.';
        }

        resultsList.appendChild(card);
      });

      resultsCard.style.display = 'block';
    }

    function applyRunSnapshot(run) {
      if (!run) {
        return;
      }

      const completed = Number(run.completed_models || 0);
      const total = Number(run.total_models || 0);
      const currentModelTitle = run.current_model_title || '';

      renderResults(run.results || []);
      renderReport(run.report || null);

      if (run.status === 'running') {
        const suffix = currentModelTitle ? ` | Сейчас: ${currentModelTitle}` : '';
        runProgress.classList.add('active');
        setRunProgress(`Проверка моделей: ${completed}/${total}${suffix}`);
        setRunError('');
        setSubmitDisabled(true);
        return;
      }

      if (run.status === 'completed') {
        runProgress.classList.remove('active');
        setRunProgress(`Проверка завершена: ${completed}/${total}`);
        setRunError('');
        setSubmitDisabled(false);
        return;
      }

      if (run.status === 'failed') {
        runProgress.classList.remove('active');
        setRunProgress('');
        setRunError(run.error_message || 'ARM процесс завершился с ошибкой');
        setSubmitDisabled(false);
      }
    }

    async function pollRunStatus() {
      if (!currentRunId) {
        return;
      }

      try {
        const response = await fetch(`${statusUrl}?run_id=${encodeURIComponent(currentRunId)}`, {
          method: 'GET',
          credentials: 'same-origin',
          headers: {
            'X-Requested-With': 'XMLHttpRequest',
          },
        });

        const data = await parseJsonResponse(response);
        if (!response.ok || !data.ok) {
          throw new Error(data.message || 'Не удалось получить статус ARM');
        }

        const run = data.run || {};
        applyRunSnapshot(run);

        if (run.status === 'running') {
          pollTimer = window.setTimeout(pollRunStatus, 1500);
        }
      } catch (error) {
        setRunError(error.message || 'Ошибка обновления статуса ARM');
        setSubmitDisabled(false);
      }
    }

    runForm.addEventListener('submit', async function (event) {
      event.preventDefault();

      if (submitButton.disabled) {
        return;
      }

      if (pollTimer) {
        window.clearTimeout(pollTimer);
        pollTimer = null;
      }

      setSubmitDisabled(true);
      setRunError('');
      runProgress.classList.add('active');
      setRunProgress('Запускаем ARM процесс...');
      renderReport(null);
      renderResults([]);

      const formData = new FormData(runForm);
      const body = new URLSearchParams(formData);

      try {
        const response = await fetch(startUrl, {
          method: 'POST',
          credentials: 'same-origin',
          headers: {
            'X-CSRFToken': getCsrfToken(),
            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
            'X-Requested-With': 'XMLHttpRequest',
          },
          body: body.toString(),
        });

        const data = await parseJsonResponse(response);
        if (!response.ok || !data.ok) {
          throw new Error(data.message || 'Не удалось запустить ARM процесс');
        }

        currentRunId = data.run_id || '';
        if (currentRunId && window.history && window.history.replaceState) {
          const newUrl = `${window.location.pathname}?run_id=${encodeURIComponent(currentRunId)}`;
          window.history.replaceState({}, '', newUrl);
        }

        applyRunSnapshot(data.run || {});
        if (data.run && data.run.status === 'running') {
          pollRunStatus();
        }
        // Прогон должен попасть в меню «Процессы» в шапке сразу, а не на
        // следующем 15-секундном тике поллинга.
        document.dispatchEvent(new Event('ai-processes:refresh'));
      } catch (error) {
        setRunError(error.message || 'Ошибка запуска ARM процесса');
        setRunProgress('');
        setSubmitDisabled(false);
      }
    });

    if (initialRun && initialRun.run_id) {
      currentRunId = initialRun.run_id;
      applyRunSnapshot(initialRun);
      if (initialRun.status === 'running') {
        pollRunStatus();
      }
    }
  })();

  (function () {
    const form = document.getElementById('armRefreshModelsForm');
    const button = document.getElementById('armRefreshModelsBtn');
    const message = document.getElementById('armRefreshAsyncMessage');

    if (!form || !button || !message) {
      return;
    }

    const csrfInput = form.querySelector('input[name="csrfmiddlewaretoken"]');
    if (!csrfInput) {
      return;
    }

    function setMessage(text, isError) {
      message.textContent = text || '';
      message.className = isError ? 'arm-error' : 'arm-success';
    }

    function getCookie(name) {
      const cookieValue = document.cookie
        .split('; ')
        .find(function (row) { return row.startsWith(name + '='); });
      return cookieValue ? decodeURIComponent(cookieValue.split('=').slice(1).join('=')) : '';
    }

    button.addEventListener('click', async function () {
      if (button.disabled) {
        return;
      }

      button.disabled = true;
      setMessage('Запускаем обновление моделей...', false);

      try {
        const response = await fetch(modelStatusRefreshUrl, {
          method: 'POST',
          credentials: 'same-origin',
          headers: {
            'X-CSRFToken': getCookie('csrftoken') || csrfInput.value,
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
        // Обновление моделей — глобальный процесс: даём меню «Процессы» в шапке
        // узнать о нём сразу, не дожидаясь планового 15-секундного поллинга.
        document.dispatchEvent(new Event('ai-processes:refresh'));
      } catch (error) {
        setMessage(error.message || 'Ошибка запуска обновления', true);
      } finally {
        button.disabled = false;
      }
    });
  })();
