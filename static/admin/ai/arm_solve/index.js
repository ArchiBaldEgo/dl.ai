// Логика страницы «Пакетное решение» (ARM): выбор языков/тем/моделей,
// запуск прогона, поллинг статуса, мультипроцессы, перезапуск нерешённых пар.
//
// Вынесена из ai/templates/admin/ai/arm_solve.html.
// Шаблонных переменных здесь нет: адреса эндпоинтов приходят через
// json_script-блоки arm-solve-*-url, данные — через arm-solve-initial-run,
// arm-prompt-bindings, arm-prompt-options, arm-topics (их рендерит шаблон
// ДО подключения этого файла). window.ArmBatchResults (общий рендер
// таблицы результатов) подключается ГЛОБАЛЬНО в admin/base_site.html.
  (function () {
    // Значения из json_script-блоков шаблона (id → значение). Фолбэк —
    // пустая строка: адрес без корректного url вернёт ошибку сети.
    function readJsonScript(id, fallback) {
      var el = document.getElementById(id);
      if (!el) { return fallback; }
      try { var v = JSON.parse(el.textContent || 'null'); return v === null ? fallback : v; }
      catch (e) { return fallback; }
    }
    const form = document.getElementById('armSolveForm');
    const submitBtn = document.getElementById('armSolveSubmit');
    const errorEl = document.getElementById('armSolveError');
    const progressEl = document.getElementById('armSolveProgress');
    const reportCard = document.getElementById('armSolveReportCard');
    const reportList = document.getElementById('armSolveReportList');
    const perModelWrap = document.getElementById('armSolvePerModelWrap');
    const perTopicWrap = document.getElementById('armSolvePerTopicWrap');
    const errorsWrap = document.getElementById('armSolveErrorsWrap');
    // Контейнер секции «Сбои по причинам» (таксономия сбоев). Без него
    // renderFailureReasons молча ничего не рисует — на этой странице
    // раздел «Сводка» должен показывать то же, что журнал и настройки.
    const reasonsWrap = document.getElementById('armSolveReasonsWrap');
    const resultsCard = document.getElementById('armSolveResultsCard');
    const resultsEl = document.getElementById('armSolveResults');
    // Адреса эндпоинтов приходят из шаблона через json_script-блоки
    // (arm-solve-start-url / arm-solve-status-url) — в .js-файле шаблонных
    // переменных быть не может.
    const startUrl = readJsonScript('arm-solve-start-url', '');
    // Режим страницы (общая форма): batch — «Пакетное решение»,
    // prompt_debug — «Отладка промтов» (попытки на пару, никакого
    // перезапуска нерешённых пар и выгрузки XLSX).
    const armMode = readJsonScript('arm-solve-mode', 'batch');
    const isPromptDebug = armMode === 'prompt_debug';
    const modeLabel = isPromptDebug ? 'отладку промтов' : 'пакет';
    const statusUrl = readJsonScript('arm-solve-status-url', '');
    // «Отладка промтов»: «Далее» — пропустить зацикленную пару (попыток = 0).
    const skipPairUrl = readJsonScript('arm-prompt-debug-skip-url', '');
    const processesCard = document.getElementById('armSolveProcessesCard');
    const processesList = document.getElementById('armSolveProcessesList');
    // Мультипроцессы: страница ведёт список запущенных прогонов (по одному
    // на каждый выбранный язык), поллит их параллельно и переключает основную
    // таблицу на выбранный процесс. focusedRunId — чей снапшот в таблице.
    let focusedRunId = '';
    const runStates = new Map();   // runId -> {timer, running, snapshot, ext, name, errors}
    const procRows = new Map();    // runId -> {row, nameEl, statusEl, showBtn, cancelBtn}
    const topicSelect = document.getElementById('armTopicSelect');
    const boundPromptLabel = document.getElementById('armBoundPromptLabel');
    const promptSelect = document.getElementById('armPromptSelect');
    const runNameInput = document.getElementById('armRunName');
    const filterHost = document.getElementById('armSolveFilterHost');
    const selectAllModelsCb = document.getElementById('armSelectAllModels');
    const extensionInput = document.getElementById('armExtensionInput');
    function getCsrfToken() {
      const input = form.querySelector('input[name="csrfmiddlewaretoken"]');
      return (document.cookie.split('; ').find(function (r) { return r.startsWith('csrftoken='); }) || '').split('=').slice(1).join('=') || (input ? input.value : '');
    }
    // Статусы задач по языку: дерево читает ТОЛЬКО флаги из локальной БД
    // (в DL при загрузке/смене языка не ходим). Адреса — из json_script.
    const TASK_STATUS_URL = readJsonScript('arm-solve-task-status-url', '');
    const REFRESH_TASK_STATUS_URL = readJsonScript('arm-solve-refresh-task-status-url', '');
    // nodeId → 0|1|2 для текущего языка. Пересоздаётся при смене языка.
    let taskStatusCache = new Map();
    // Сервер обновляет ≤100 узлов за запрос; клиент режет по 10 (ровно размер
    // пула сервера — пачка уходит одним кругом). Причина — внешний nginx на
    // dl.gsu.by без явного proxy_read_timeout (дефолт 60 с): пачка из 50 узлов
    // при 5 потоках и таймауте узла 10 с тянулась дольше минуты, nginx отдавал
    // HTML 504, а клиент показывал его как «сессия истекла».
    const TASK_REFRESH_CHUNK = 10;

    function statusToClass(status) {
        if (status === 1) return 'arm-tree-task-good';
        if (status === 2) return 'arm-tree-task-bad';
        return 'arm-tree-task-undefined';
    }

    function statusTitle(status) {
        if (status === 1) return 'Решена на DL: есть верное решение';
        if (status === 2) return 'Были попытки, но ни одна не прошла';
        return 'Посылок не было или статусы ещё не обновлялись';
    }

    function treeNodeIds() {
        return Array.from(
            treeContainer.querySelectorAll('input.arm-task-check')
        ).map(function (cb) { return cb.value; });
    }

    function updateTaskClasses() {
        treeContainer.querySelectorAll('input.arm-task-check').forEach(function (cb) {
            const nameEl = cb.parentElement.querySelector('.arm-tree-task-name');
            if (!nameEl) return;

            const status = taskStatusCache.get(String(cb.value));
            const value = status == null ? 0 : status;
            nameEl.className = 'arm-tree-task-name ' + statusToClass(value);
            nameEl.title = statusTitle(value);
        });
    }

    function setStatusMessage(text, color) {
        const el = document.getElementById('armStatusMsg');
        if (!el) return;
        el.textContent = text || '';
        el.style.color = color || '';
    }

    function formatRefreshedAt(iso) {
        if (!iso) return '';
        const date = new Date(iso);
        if (isNaN(date.getTime())) return '';
        return date.toLocaleString('ru-RU', {
            day: '2-digit', month: '2-digit', year: 'numeric',
            hour: '2-digit', minute: '2-digit',
        });
    }

    async function loadTaskStatuses(nodeIds) {
        const courseId = courseIdInput ? courseIdInput.value.trim() : '';
        const lang = selectedLanguage();
        if (!courseId || !lang || !nodeIds.length) {
            taskStatusCache = new Map();
            updateTaskClasses();
            return;
        }

        const body = new URLSearchParams();
        body.set('course_id', courseId);
        body.set('language_id', lang.id);
        body.set('node_ids', nodeIds.join(','));

        const response = await fetch(TASK_STATUS_URL, {
            method: 'POST',
            credentials: 'same-origin',
            headers: {
                'X-CSRFToken': getCsrfToken(),
                'X-Requested-With': 'XMLHttpRequest',
                'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
            },
            body: body.toString(),
        });

        const data = await parseJsonResponse(response);

        if (!response.ok || !data.ok) {
            throw new Error((data.message || 'Не удалось получить статусы задач')
                + (response.ok ? '' : ' (HTTP ' + response.status + ')'));
        }

        taskStatusCache = new Map(Object.entries(data.statuses || {}));
        updateTaskClasses();
        const when = formatRefreshedAt(data.refreshed_at);
        setStatusMessage(when
            ? ('Статусы от ' + when)
            : 'Статусы ещё не обновлялись — нажмите «Обновить статусы из DL».');
    }

    // Прогон обновления флагов по DL (/get-solutions). Пачки узлов идут
    // последовательно; после каждой пачки перекрашиваем дерево. Кнопка
    // блокируется, чтобы не запускать два прогона параллельно.
    async function refreshTaskStatuses() {
        const btn = document.getElementById('armRefreshStatusesBtn');
        const courseId = courseIdInput ? courseIdInput.value.trim() : '';
        const lang = selectedLanguage();
        const nodeIds = treeNodeIds();
        if (!courseId || !lang || !nodeIds.length) {
            setStatusMessage('Сначала загрузите дерево и выберите язык', '#b42318');
            return;
        }
        if (btn) { btn.disabled = true; btn.textContent = 'Обновляем...'; }
        const summary = { solved: 0, attempted: 0, none: 0 };
        let errors = 0;
        let done = 0;
        let refreshedAt = null;
        setStatusMessage('Обновление статусов: 0 из ' + nodeIds.length + '...');
        try {
            for (let i = 0; i < nodeIds.length; i += TASK_REFRESH_CHUNK) {
                const chunk = nodeIds.slice(i, i + TASK_REFRESH_CHUNK);
                const body = new URLSearchParams();
                body.set('course_id', courseId);
                body.set('language_id', lang.id);
                body.set('node_ids', chunk.join(','));
                const response = await fetch(REFRESH_TASK_STATUS_URL, {
                    method: 'POST',
                    credentials: 'same-origin',
                    headers: {
                        'X-CSRFToken': getCsrfToken(),
                        'X-Requested-With': 'XMLHttpRequest',
                        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                    },
                    body: body.toString(),
                });
                const data = await parseJsonResponse(response);
                if (!response.ok || !data.ok) {
                    // Статус-код не теряем: 502 (DL недоступен) и 400 (кривые
                    // параметры) лечатся по-разному.
                    throw new Error((data.message || 'Не удалось обновить статусы')
                        + (response.ok ? '' : ' (HTTP ' + response.status + ')'));
                }
                Object.entries(data.statuses || {}).forEach(function ([nodeId, status]) {
                    taskStatusCache.set(String(nodeId), status);
                });
                const chunkSummary = data.summary || {};
                summary.solved += chunkSummary.solved || 0;
                summary.attempted += chunkSummary.attempted || 0;
                summary.none += chunkSummary.none || 0;
                errors += Object.keys(data.errors || {}).length;
                if (data.refreshed_at) { refreshedAt = data.refreshed_at; }
                done += chunk.length;
                updateTaskClasses();
                if (done < nodeIds.length) {
                    setStatusMessage('Обновление статусов: ' + done + ' из ' + nodeIds.length + '...');
                }
            }
            let text = 'Готово: решено ' + summary.solved
                + ', попытки ' + summary.attempted
                + ', нет ' + summary.none;
            if (errors) { text += ', ошибок ' + errors; }
            const when = formatRefreshedAt(refreshedAt);
            if (when) { text += ' · статусы от ' + when; }
            setStatusMessage(text, errors ? '#b54708' : '#067647');
        } catch (error) {
            // Обрыв на середине не отменяет уже записанные пачки — честно
            // показываем, сколько узлов успело обновиться.
            const partial = done ? ' (обновлено ' + done + ' из ' + nodeIds.length + ')' : '';
            setStatusMessage((error.message || 'Ошибка обновления статусов') + partial, '#b42318');
        } finally {
            if (btn) {
                btn.disabled = false;
                btn.textContent = 'Обновить статусы из DL';
            }
        }
    }
    // AJAX админки всегда ждёт JSON; HTML (например, редирект на страницу
    // логина при протухшей сессии — fetch молча следует за 302) даёт сырую
    // ошибку JSON.parse. Не-JSON с другим статусом (CSRF 403, 500, 502/504 от
    // прокси) показываем со статусом и началом тела: маскировать всё под
    // «сессию истекла» нельзя — это скрывает настоящую причину (см. ту же
    // конвенцию в tasks/index.js).
    async function parseJsonResponse(response) {
      const text = await response.text();
      try {
        return JSON.parse(text);
      } catch (e) {
        const head = text.replace(/\s+/g, ' ').slice(0, 120);
        if (response.status === 403) {
          throw new Error('CSRF-проверка не прошла (HTTP 403): ' + head +
            ' — обновите страницу (Ctrl+F5) и повторите');
        }
        throw new Error('Сервер вернул не JSON, HTTP ' + response.status + ': ' + head +
          (head.indexOf('<!DOCTYPE') === 0 || head.indexOf('<html') === 0
            ? ' — Сессия истекла, войдите заново'
            : ''));
      }
    }
    function setSubmitDisabled(disabled) {
      // Блокировка только на время ЗАПУСКА: сами прогоны идут параллельно и
      // форму не блокируют — можно запускать следующие пакеты, не дожидаясь.
      // Лейблы — из data-атрибутов шаблона (у отладки промтов своя кнопка).
      submitBtn.disabled = disabled;
      submitBtn.textContent = disabled
        ? (submitBtn.dataset.busyLabel || 'Запускаем...')
        : (submitBtn.dataset.idleLabel || 'Запустить пакет');
    }
    function setError(text) {
      if (!text) { errorEl.textContent = ''; errorEl.style.display = 'none'; return; }
      errorEl.textContent = text; errorEl.style.display = 'block';
    }
    function setProgress(text, active) {
      const span = progressEl.querySelector('.arm-progress-text');
      if (span) { span.textContent = text || ''; }
      if (text) {
        progressEl.style.display = 'flex';
        progressEl.classList.toggle('active', !!active);
      } else {
        progressEl.style.display = 'none';
        progressEl.classList.remove('active');
      }
    }
    function stopProgress() {
      progressEl.style.display = 'none';
      progressEl.classList.remove('active');
    }
    // Мини-таблица результатов (сводка + ответы моделей) и CSV-экспорт
    // вынесены в общий partial _ai_batch_results.html (window.ArmBatchResults) —
    // тот же рендер используется детальной страницей Журнала запросов в режиме
    // «Пакетное решение». Локальные обёртки пробрасывают closure-элементы;
    // filterHost — куда partial ставит переключатель «Все / Не пройденные».
    function renderReport(report) {
      ArmBatchResults.renderReport(report, { card: reportCard, list: reportList, perModel: perModelWrap, perTopic: perTopicWrap, errors: errorsWrap, reasons: reasonsWrap });
    }
    function renderResults(results) {
      ArmBatchResults.renderResults(results, resultsEl, resultsCard, filterHost);
    }
    function applyRunSnapshot(run) {
      if (!run) { return; }
      renderReport(run.report || null);
      renderResults(run.results || []);
      // Название прогона (ручное, задаётся при запуске) — в примечании «Сводки».
      const reportNote = document.getElementById('armSolveReportNote');
      if (reportNote) {
        const base = reportNote.dataset.base || '';
        const name = run.run_name || (run.run_params && run.run_params.run_name) || '';
        reportNote.textContent = name ? (base + ' · Название прогона: ' + name) : base;
      }
      // Show download link only when run is done and has results; href —
      // серверная xlsx-выгрузка по run_id (живой прогон и DB-fallback).
      const downloadBtn = document.getElementById('armSolveDownloadXlsxBtn');
      if (downloadBtn) {
        const hasResults = (run.results || []).length > 0;
        const isDone = run.status === 'completed' || run.status === 'failed' || run.status === 'cancelled';
        // У отладки промтов выгрузки нет: в таблице не код, а правки промпта.
        downloadBtn.style.display = (!isPromptDebug && hasResults && isDone) ? '' : 'none';
        if (run.run_id) {
          downloadBtn.href = '/ai/admin/arm/solve/report/' + encodeURIComponent(run.run_id) + '/xlsx/';
        }
      }
      updateRetryUnsolvedButton(run);
      if (run.status === 'running') {
        const done = run.completed_pairs != null ? run.completed_pairs : 0;
        const total = run.total_pairs != null ? run.total_pairs : 0;
        const cur = perModelProgressText(run) ||
          (run.current_task_name ? (' · ' + run.current_task_name + ' / ' + (run.current_model_title || '')) : '');
        setProgress('Выполняется: ' + done + ' / ' + total + cur, true);
        // Форму больше не блокируем: прогоны идут параллельно, можно запускать
        // следующие пакеты. Отмена — по строкам «Процессов» и кнопке ниже
        // (отменяет ФОКУСИРОВАННЫЙ прогон).
        setCancelVisible(true);
      } else if (run.status === 'paused') {
        const done = run.completed_pairs != null ? run.completed_pairs : 0;
        const total = run.total_pairs != null ? run.total_pairs : 0;
        setProgress('Пауза: ' + done + ' / ' + total + ' (текущая пара завершится)', true);
        setCancelVisible(true);
      } else if (run.status === 'cancelled') {
        // Мгновенная отмена: cancel_arm_run уже перевёл прогон в cancelled.
        // Частичные результаты/report уже отрендерены выше — показываем «Прервано».
        setProgress('Прервано.', false);
        setCancelVisible(false);
      } else if (run.status === 'completed') {
        const cancelled = run.cancelled || run.error_message === 'Прервано пользователем';
        if (cancelled) { setProgress('Прервано.', false); } else { stopProgress(); }
        setCancelVisible(false);
      } else if (run.status === 'failed') {
        stopProgress();
        setError(run.error_message || 'Batch solve завершился с ошибкой');
        setCancelVisible(false);
      }
    }
    // ── Мультипроцессы: параллельные прогоны этой страницы ─────────────────
    // Каждый выбранный язык — отдельный процесс (отдельный run в БД, свой
    // отчёт/XLSX/журнал/отмена). runStates держит снапшоты и таймеры, строки
    // «Процессов» обновляются по каждому тику; focusedRunId решает, чей
    // снапшот рендерится в основной таблице.
    function trackRun(runId, meta) {
      if (!runId) { return null; }
      let st = runStates.get(runId);
      if (!st) {
        st = { timer: null, running: true, snapshot: null, ext: '', name: '', lang: '', errors: 0 };
        runStates.set(runId, st);
        addProcessesRow(runId);
      }
      if (meta) {
        if (meta.ext != null) { st.ext = meta.ext; }
        if (meta.name != null) { st.name = meta.name; }
        if (meta.lang != null) { st.lang = meta.lang; }
        if (meta.snapshot) { st.snapshot = meta.snapshot; st.running = ['running', 'paused'].indexOf(st.snapshot.status || '') !== -1; updateProcessesRow(runId); }
      }
      if (st.running && !st.timer) {
        st.timer = window.setTimeout(function () { st.timer = null; pollRunOnce(runId); }, 1000);
      }
      return st;
    }
    function addProcessesRow(runId) {
      if (procRows.has(runId)) { return; }
      if (processesCard) { processesCard.style.display = ''; }
      const row = document.createElement('div');
      row.className = 'arm-proc-row';
      row.dataset.run = runId;
      const nameEl = document.createElement('span');
      nameEl.className = 'arm-proc-name';
      const statusEl = document.createElement('span');
      statusEl.className = 'arm-proc-status';
      const actions = document.createElement('div');
      actions.className = 'arm-proc-actions';
      const showBtn = document.createElement('button');
      showBtn.type = 'button'; showBtn.className = 'arm-button';
      showBtn.textContent = 'Открыть';
      showBtn.title = 'Показать результаты этого прогона в таблице ниже';
      const cancelBtnEl = document.createElement('button');
      cancelBtnEl.type = 'button'; cancelBtnEl.className = 'arm-button'; cancelBtnEl.textContent = 'Прервать';
      cancelBtnEl.style.display = 'none';
      // Приостановка на границе пар (running → Пауза) и продолжение (paused →
      // Продолжить); видимость ведёт updateProcessesRow по статусу снапшота.
      const pauseBtnEl = document.createElement('button');
      pauseBtnEl.type = 'button'; pauseBtnEl.className = 'arm-button';
      pauseBtnEl.style.display = 'none';
      // «Далее» (только отладка промтов): пропустить текущую пару и перейти к
      // следующей задаче — выход из «зацикленной» задачи при попытках = 0.
      const skipBtnEl = document.createElement('button');
      skipBtnEl.type = 'button'; skipBtnEl.className = 'arm-button';
      skipBtnEl.textContent = 'Далее';
      skipBtnEl.title = 'Пропустить текущую задачу и перейти к следующей (правки промпта не сохраняются)';
      skipBtnEl.style.display = 'none';
      actions.appendChild(showBtn); actions.appendChild(pauseBtnEl);
      actions.appendChild(skipBtnEl); actions.appendChild(cancelBtnEl);
      row.appendChild(nameEl); row.appendChild(statusEl); row.appendChild(actions);
      if (processesList) { processesList.appendChild(row); }
      showBtn.addEventListener('click', function () { focusRun(runId); });
      cancelBtnEl.addEventListener('click', async function () {
        cancelBtnEl.disabled = true;
        try { await cancelRunRequest(runId); await pollRunOnce(runId); }
        catch (e) { setError(e.message || 'Ошибка прерывания'); }
        cancelBtnEl.disabled = false;
      });
      skipBtnEl.addEventListener('click', async function () {
        skipBtnEl.disabled = true;
        try {
          const response = await fetch(skipPairUrl, {
            method: 'POST', credentials: 'same-origin',
            headers: {
              'X-CSRFToken': getCsrfToken(), 'X-Requested-With': 'XMLHttpRequest',
              'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
            },
            body: new URLSearchParams({ run_id: runId }),
          });
          const data = await parseJsonResponse(response);
          if (!response.ok || !data.ok) {
            throw new Error(data.message || 'Не удалось пропустить задачу');
          }
          setError('');
          if (data.run && runStates.has(runId)) {
            runStates.get(runId).snapshot = data.run;
            updateProcessesRow(runId);
            if (runId === focusedRunId) {
              try { applyRunSnapshot(data.run); } catch (e) { console.error('ARM render error:', e); }
            }
          }
        } catch (e) {
          setError(e.message || 'Не удалось пропустить задачу');
        }
        skipBtnEl.disabled = false;
      });
      pauseBtnEl.addEventListener('click', async function () {
        pauseBtnEl.disabled = true;
        const resuming = pauseBtnEl.textContent.indexOf('Продолжить') !== -1;
        try {
          const data = await pauseRunRequest(runId, resuming);
          setError('');
          if (data.run && runStates.has(runId) && runStates.get(runId).snapshot) {
            Object.assign(runStates.get(runId).snapshot, data.run);
            runStates.get(runId).running = ['running', 'paused'].indexOf((data.run.status || '')) !== -1;
            updateProcessesRow(runId);
          }
          document.dispatchEvent(new Event('ai-processes:refresh'));
          await pollRunOnce(runId);
        } catch (e) { setError(e.message || 'Ошибка паузы'); }
        pauseBtnEl.disabled = false;
      });
      procRows.set(runId, { row: row, nameEl: nameEl, statusEl: statusEl, showBtn: showBtn, cancelBtn: cancelBtnEl, pauseBtn: pauseBtnEl, skipBtn: skipBtnEl });
    }
    function procRunLabel(runId, st) {
      const langLabel = st.lang || st.ext;
      return (st.name || 'Пакет') + (langLabel ? ' · ' + langLabel : '') + ' · #' + String(runId).slice(0, 8);
    }
    // Per-model прогресс параллельного прогона: run.model_progress — список по
    // моделям ({model_title, done, total, current_task_name, status}); модели
    // решают независимо, поэтому общий «сейчас: X / Y» заменяется строками
    // «Модель: done/total («текущая задача»)». Возвращаем null, если поля нет
    // (старые job/DB-снапшоты) — вызывающий код падает обратно на current_*.
    function perModelProgressText(snapshot) {
      const mp = snapshot.model_progress || [];
      if (!mp.length) { return null; }
      return ' · ' + mp.map(function (m) {
        const task = m.current_task_name ? ' («' + m.current_task_name + '»)' : '';
        return (m.model_title || m.model_key || '?') + ': ' + (m.done || 0) + '/' + (m.total || 0) + task;
      }).join(', ');
    }
    function updateProcessesRow(runId) {
      const st = runStates.get(runId);
      const ui = procRows.get(runId);
      if (!st || !ui) { return; }
      ui.nameEl.textContent = procRunLabel(runId, st);
      const s = st.snapshot || {};
      ui.statusEl.classList.remove('ok', 'no');
      if (s.status === 'running') {
        const done = s.completed_pairs != null ? s.completed_pairs : 0;
        const total = s.total_pairs != null ? s.total_pairs : 0;
        const cur = perModelProgressText(s) ||
          (s.current_task_name ? ' · сейчас: ' + s.current_task_name + ' / ' + (s.current_model_title || '') : '');
        ui.statusEl.textContent = 'Выполняется: ' + done + ' из ' + total + cur;
      } else if (s.status === 'completed') {
        const wasCancelled = s.cancelled || s.error_message === 'Прервано пользователем';
        ui.statusEl.textContent = wasCancelled ? 'Прервано (частичные результаты сохранены)' : 'Выполнен';
        ui.statusEl.classList.add(wasCancelled ? 'no' : 'ok');
      } else if (s.status === 'paused') {
        const done = s.completed_pairs != null ? s.completed_pairs : 0;
        const total = s.total_pairs != null ? s.total_pairs : 0;
        ui.statusEl.textContent = 'Пауза: ' + done + ' из ' + total + ' (текущая пара завершится)';
      } else if (s.status === 'cancelled') {
        ui.statusEl.textContent = 'Прервано';
        ui.statusEl.classList.add('no');
      } else if (s.status === 'failed') {
        ui.statusEl.textContent = 'Ошибка: ' + (s.error_message || 'прогон завершился с ошибкой');
        ui.statusEl.classList.add('no');
      } else {
        ui.statusEl.textContent = s.status || '—';
      }
      ui.cancelBtn.style.display = st.running ? '' : 'none';
      // «Далее» — только на странице «Отладка промтов» и только пока прогон
      // идёт: пропускает текущую пару (актуально при попытках = 0).
      if (ui.skipBtn) {
        ui.skipBtn.style.display = (isPromptDebug && s.status === 'running') ? '' : 'none';
      }
      // Кнопка Пауза/Продолжить: running → Пауза; paused → Продолжить; прочее — скрыта.
      if (ui.pauseBtn) {
        if (s.status === 'running') {
          ui.pauseBtn.textContent = 'Пауза';
          ui.pauseBtn.style.display = '';
        } else if (s.status === 'paused') {
          ui.pauseBtn.textContent = 'Продолжить';
          ui.pauseBtn.style.display = '';
        } else {
          ui.pauseBtn.style.display = 'none';
        }
        ui.pauseBtn.disabled = false;
      }
      ui.showBtn.classList.toggle('active', runId === focusedRunId);
      ui.row.classList.toggle('focused', runId === focusedRunId);
    }
    async function pollRunOnce(runId) {
      const st = runStates.get(runId);
      if (!st) { return; }
      try {
        const response = await fetch(statusUrl + '?run_id=' + encodeURIComponent(runId), {
          method: 'GET', credentials: 'same-origin', headers: { 'X-Requested-With': 'XMLHttpRequest' },
        });
        const data = await parseJsonResponse(response);
        if (!response.ok || !data.ok) {
          const err = new Error(data.message || 'Не удалось получить статус');
          err.fatal = response.status === 404;
          throw err;
        }
        st.errors = 0;
        st.snapshot = data.run || {};
        st.running = ['running', 'paused'].indexOf(st.snapshot.status || '') !== -1;
        updateProcessesRow(runId);
        if (runId === focusedRunId) {
          // Сбой отрисовки не должен ронять поллинг/таймер — логируем и живём.
          try { applyRunSnapshot(st.snapshot); } catch (renderError) { console.error('ARM render error:', renderError); }
        }
      } catch (error) {
        if (error.fatal) {
          // Прогон исчез из памяти и БД — считаем завершённым, поллинг останавливаем.
          st.running = false;
          st.timer = null;
          if (!st.snapshot) { st.snapshot = { status: 'failed', error_message: error.message }; }
          updateProcessesRow(runId);
          return;
        }
        st.errors = (st.errors || 0) + 1;
        updateProcessesRow(runId);
        const ui = procRows.get(runId);
        if (ui) { ui.statusEl.textContent = 'Ошибка обновления (' + (st.errors || 0) + '): ' + error.message; }
        // Сетевые сбои не убивают поллинг — пробуем следующим тиком (до 5 раз подряд).
        if (st.errors > 5) { st.timer = null; return; }
      }
      if (st.running && !st.timer) { st.timer = window.setTimeout(function () { st.timer = null; pollRunOnce(runId); }, 2000); }
    }
    const retryBtnEl = document.getElementById('armSolveRetryUnsolvedBtn');
    const rerunPairsUrl = '/ai/admin/arm/solve/rerun-pairs/';
    if (retryBtnEl) {
      // Попарный перезапуск нерешённых пар со слиянием в ТОТ ЖЕ прогон: сервер
      // сам собирает пары (задача × модель, вердикт != solved) из БД прогона —
      // клиент передаёт только run_id. Отчёт после пересобирается из всех строк.
      retryBtnEl.addEventListener('click', async function () {
        if (retryBtnEl.disabled) { return; }
        const runId = focusedRunId;
        if (!runId) { return; }
        retryBtnEl.disabled = true;
        retryBtnEl.textContent = 'Запускаем перезапуск...';
        try {
          const response = await fetch(rerunPairsUrl, {
            method: 'POST', credentials: 'same-origin',
            headers: { 'X-CSRFToken': getCsrfToken(), 'X-Requested-With': 'XMLHttpRequest', 'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8' },
            body: new URLSearchParams({ run_id: runId }),
          });
          const data = await parseJsonResponse(response);
          if (!response.ok || !data.ok) {
            throw new Error(data.message || 'Не удалось запустить перезапуск');
          }
          setError('');
          if (data.run) {
            try { applyRunSnapshot(data.run); } catch (e) { console.error('ARM render error:', e); }
          }
          // trackRun обновляет снапшот, ставит running и заводит поллер;
          // focusRun держит этот прогон в основной таблице.
          trackRun(runId, { snapshot: data.run || {} });
          focusRun(runId);
          document.dispatchEvent(new Event('ai-processes:refresh'));
        } catch (error) {
          setError(error.message || 'Не удалось запустить перезапуск');
        }
        retryBtnEl.disabled = false;
        retryBtnEl.textContent = 'Перезапустить нерешённые пары';
      });
    }

    function focusRun(runId) {
      if (!runId || !runStates.has(runId)) { return; }
      focusedRunId = runId;
      if (window.history && window.history.replaceState) {
        window.history.replaceState({}, '', window.location.pathname + '?run_id=' + encodeURIComponent(runId));
      }
      runStates.forEach(function (_st, rid) { updateProcessesRow(rid); });
      const st = runStates.get(runId);
      if (st && st.snapshot) {
        try { applyRunSnapshot(st.snapshot); } catch (e) { console.error('ARM render error:', e); }
      } else {
        pollRunOnce(runId); // снапшота нет — тянем немедленно, не ждём тика
      }
    }
    // Load tree from DL
    const loadTreeBtn = document.getElementById('armLoadTreeBtn');
    const loadTreeMsg = document.getElementById('armLoadTreeMsg');
    const courseIdInput = document.getElementById('armCourseId');
    const treeContainer = document.getElementById('armTreeContainer');
    const treeUrl = readJsonScript('arm-solve-tree-url', '');
    const cancelUrl = '/ai/admin/arm/solve/cancel/';
    const cancelBtn = document.getElementById('armSolveCancelBtn');

    // Tri-state выбор поддерева: чекбокс папки переключает всех потомков-листьев,
    // состояние папки (отмечено/indeterminate/снято) пересчитывается снизу вверх.
    function updateFolderState(details) {
      const leaves = details.querySelectorAll('input.arm-task-check');
      let total = 0, checked = 0;
      leaves.forEach(function (c) { total++; if (c.checked) { checked++; } });
      const cb = details.querySelector(':scope > summary > input.arm-tree-folder-check');
      if (!cb) { return; }
      cb.checked = total > 0 && checked === total;
      cb.indeterminate = checked > 0 && checked < total;
    }

    function refreshAllFolderStates() {
      // Снизу вверх: сначала листья, потом родители. details могут быть вложены,
      // поэтому обходим снаружи внутрь — updateFolderState читает все потомки,
      // включая вложенные листья, так что одного прохода достаточно.
      treeContainer.querySelectorAll('details.arm-tree-folder').forEach(updateFolderState);
      updateSelectedCount();
      syncSelectAllCheckbox();
      // Выбранные задачи живут в localStorage (data-no-state, см. ниже) —
      // сохраняем после каждой смены отметок.
      saveSelectedNodes();
    }

    function updateSelectedCount() {
      const countEl = document.getElementById('armTreeSelectedCount');
      if (!countEl) { return; }
      const total = treeContainer.querySelectorAll('input.arm-task-check').length;
      const checked = treeContainer.querySelectorAll('input.arm-task-check:checked').length;
      countEl.textContent = total ? ('Выбрано ' + checked + ' из ' + total + ' задач') : '';
    }

    function syncSelectAllCheckbox() {
      const master = document.getElementById('armTreeSelectAll');
      if (!master) { return; }
      const total = treeContainer.querySelectorAll('input.arm-task-check').length;
      const checked = treeContainer.querySelectorAll('input.arm-task-check:checked').length;
      master.checked = total > 0 && checked === total;
      master.indeterminate = checked > 0 && checked < total;
    }

    function renderTreeNodes(nodes, container) {
      nodes.forEach(function (node) {
        if (node.isFolder) {
          const details = document.createElement('details');
          details.className = 'arm-tree-folder';
          const summary = document.createElement('summary');

          // Tri-state чекбокс папки (без name — не сабмитится).
          const folderCb = document.createElement('input');
          folderCb.type = 'checkbox';
          folderCb.className = 'arm-tree-folder-check';
          // Клик по чекбоксу не должен сворачивать/раскрывать <details>.
          folderCb.addEventListener('click', function (e) { e.stopPropagation(); });
          folderCb.addEventListener('change', function () {
            const checked = folderCb.checked;
            details.querySelectorAll('input.arm-task-check').forEach(function (c) {
              c.checked = checked;
            });
            refreshAllFolderStates();
          });
          summary.appendChild(folderCb);

          const name = document.createElement('span');
          name.className = 'arm-tree-folder-name';
          name.textContent = node.name || '(папка)';
          summary.appendChild(name);
          details.appendChild(summary);

          if (node.children && node.children.length) {
            const ul = document.createElement('ul');
            renderTreeNodes(node.children, ul);
            details.appendChild(ul);
          }
          container.appendChild(details);
        } else {
          const li = document.createElement('li');
          li.className = 'arm-tree-task';
          const cb = document.createElement('input');
          cb.type = 'checkbox';
          cb.name = 'node_ids';
          cb.value = String(node.nodeId);
          cb.className = 'arm-task-check';
          // Исключены из общего сохранения ai_admin_state.js (data-no-state):
          // чекбоксы появляются динамически и восстанавливаются выделенным
          // механизмом localStorage ai_arm_solve_nodes — иначе гонка двойного
          // восстановления и лишние срабатывания change.
          cb.setAttribute('data-no-state', '');
          cb.checked = false;
          cb.addEventListener('change', refreshAllFolderStates);
          li.appendChild(cb);

          const name = document.createElement('span');
          name.className = 'arm-tree-task-name';
          name.textContent = node.name || '(без названия)';
                  
          // Статус подставим позже bulk-запросом
          name.classList.add('arm-tree-task-undefined');
                  
          li.appendChild(name);
          container.appendChild(li);
        }
      });
    }

    function renderTree(tree) {
  taskStatusCache = new Map();
  treeContainer.innerHTML = '';
  treeContainer.classList.remove('arm-empty');
  const toolbar = document.getElementById('armTreeToolbar');
  const statusBar = document.getElementById('armTreeStatusBar');
  if (!tree || !tree.length) {
    treeContainer.classList.add('arm-empty');
    treeContainer.textContent = 'Дерево пусто.';
    if (toolbar) { toolbar.style.display = 'none'; }
    if (statusBar) { statusBar.style.display = 'none'; }
    return;
  }
  const ul = document.createElement('ul');
  ul.className = 'arm-tree';
  renderTreeNodes(tree, ul);
  treeContainer.appendChild(ul);
  if (toolbar) { toolbar.style.display = 'flex'; }
  // Панель языка появляется после загрузки дерева (её выбор управляет и
  // раскраской, и прогоном).
  if (statusBar) { statusBar.style.display = 'flex'; }
  refreshAllFolderStates();

  // Цвета зависят только от языка, а не от отметок задач: один bulk-запрос
  // на все узлы (в БД, не в DL). Без языка — всё серое + подсказка.
  const nodeIds = treeNodeIds();
  if (nodeIds.length && selectedLanguage()) {
    loadTaskStatuses(nodeIds).catch(function (e) {
      console.error('ARM task statuses error:', e);
      setStatusMessage(e.message || 'Не удалось загрузить статусы', '#b42318');
    });
  } else {
    updateTaskClasses();
    setStatusMessage('Выберите язык, чтобы увидеть статусы задач.');
  }
}

    async function loadTree() {
      const courseId = courseIdInput.value.trim();
      if (!courseId) {
        loadTreeMsg.style.color = '#b42318';
        loadTreeMsg.textContent = 'Введите course_id';
        return;
      }
      // 0 = пусто: дерево не грузим, кэш не трогаем (соглашение /arm/solve/).
      if (parseInt(courseId, 10) === 0) {
        renderTree([]);
        loadTreeMsg.style.color = '#6b7280';
        loadTreeMsg.textContent = 'nodeId = 0 (пусто)';
        return;
      }
      loadTreeMsg.style.color = '';
      loadTreeMsg.textContent = 'Загрузка дерева...';
      loadTreeBtn.disabled = true;
      try {
        const body = new URLSearchParams();
        body.set('course_id', courseId);
        const response = await fetch(treeUrl, {
          method: 'POST', credentials: 'same-origin',
          headers: { 'X-CSRFToken': getCsrfToken(), 'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8', 'X-Requested-With': 'XMLHttpRequest' },
          body: body.toString(),
        });
        const data = await parseJsonResponse(response);
        if (!response.ok || !data.ok) { throw new Error(data.message || 'Ошибка загрузки дерева'); }
        loadTreeMsg.style.color = data.stale ? '#92400e' : '#067647';
        loadTreeMsg.textContent = data.message
          ? data.message + ' · задач: ' + (data.task_count || 0)
          : '✓ Загружено задач: ' + (data.task_count || 0);
        // Снапшот localStorage читаем ДО renderTree: renderTree →
        // refreshAllFolderStates → saveSelectedNodes перезаписал бы ключ
        // пустым выбором раньше, чем мы успели прочитать сохранённое.
        const savedNodes = pendingNodeIds === null ? readSavedNodes() : null;
        renderTree(data.tree || []);
        // Восстановление отметок: на странице прогона (?run_id=) — из
        // run_params, иначе — из localStorage. Потом сохраняем текущее
        // состояние (пустой выбор тоже валиден).
        if (pendingNodeIds !== null) {
          restoreNodes(pendingNodeIds);
          pendingNodeIds = null;
        } else if (savedNodes && String(savedNodes.courseId) === courseId) {
          restoreNodes(savedNodes.nodeIds || []);
        }
        saveSelectedNodes();
      } catch (error) {
        loadTreeMsg.style.color = '#b42318';
        loadTreeMsg.textContent = error.message || 'Ошибка';
      } finally {
        loadTreeBtn.disabled = false;
      }
    }

    if (loadTreeBtn) {
      loadTreeBtn.addEventListener('click', loadTree);
    }
    // Смена курса перезагружает дерево автоматически: во-первых, restore
    // состояния страницы (ai_admin_state.js) выставляет сохранённый course_id
    // и диспатчит change — без листенера сохранённые задачи неотмечаемы
    // (дерево грузится позже, чекбоксы создаются только после loadTree);
    // во-вторых, вручную менять курс без лишнего клика по кнопке удобнее.
    if (courseIdInput) {
      courseIdInput.addEventListener('change', function () {
        if (courseIdInput.value.trim()) { loadTree(); }
      });
    }

    const selectAllCb = document.getElementById('armTreeSelectAll');
    if (selectAllCb) {
      selectAllCb.addEventListener('click', function (e) { e.stopPropagation(); });
      selectAllCb.addEventListener('change', function () {
        const checked = selectAllCb.checked;
        treeContainer.querySelectorAll('input.arm-task-check').forEach(function (c) {
          c.checked = checked;
        });
        refreshAllFolderStates();
      });
    }

    // Промпт: по умолчанию тема подтягивает привязку ArmPromptBinding
    // (mode=solve) — «промпт по умолчанию» суперюзера; можно выбрать любой
    // промпт вручную (селект armPromptSelect, value="" = по привязке).
    // Список тем отдаётся сервером целиком (json_script) и фильтруется на
    // клиенте по языку.
    let allTopics = [];
    try {
      const topicsEl = document.getElementById('arm-topics');
      allTopics = JSON.parse(topicsEl ? topicsEl.textContent : '[]') || [];
    } catch (e) { allTopics = []; }

    // Все промпты для ручного выбора (сервер, json_script; промпты —
    // студенческий контент, список полный, как в chat-facing API).
    let allPrompts = [];
    try {
      const promptsEl = document.getElementById('arm-prompt-options');
      allPrompts = JSON.parse(promptsEl ? promptsEl.textContent : '[]') || [];
    } catch (e) { allPrompts = []; }

    // Привязки «промпт по умолчанию» отдаются сервером в json_script.
    // Резолв как на сервере (ArmPromptBinding.resolve): точная (язык+тема),
    // иначе привязка «на весь язык» (topic_id === null — языки без тем).
    let promptBindings = [];
    try {
      const bindingsEl = document.getElementById('arm-prompt-bindings');
      promptBindings = JSON.parse(bindingsEl ? bindingsEl.textContent : '[]') || [];
    } catch (e) { promptBindings = []; }

    function findBoundPrompt(languageId, topicId, mode) {
      const exact = promptBindings.find(function (x) {
        return String(x.language_id) === String(languageId)
          && x.topic_id !== null && String(x.topic_id) === String(topicId)
          && x.mode === mode;
      });
      if (exact) { return exact; }
      return promptBindings.find(function (x) {
        return String(x.language_id) === String(languageId)
          && x.topic_id === null && x.mode === mode;
      }) || null;
    }

    // Подпись под селектом промпта: что фактически применится — ручной
    // выбор, привязка «по умолчанию» (или что прогон пойдёт без промпта).
    function updateBoundPromptLabel() {
      if (!boundPromptLabel) { return; }
      const manualId = promptSelect ? String(promptSelect.value || '') : '';
      if (manualId) {
        const manual = allPrompts.find(function (p) { return String(p.id) === manualId; });
        boundPromptLabel.style.display = '';
        boundPromptLabel.textContent = 'Промпт (выбран вручную): '
          + (manual ? (manual.name || 'Prompt #' + manual.id) : 'Prompt #' + manualId);
        return;
      }
      const lang = selectedLanguage();
      if (!lang) {
        boundPromptLabel.style.display = 'none';
        return;
      }
      const languageId = lang.id;
      const topicId = topicSelect ? topicSelect.value : '';
      const binding = languageId ? findBoundPrompt(languageId, topicId, 'solve') : null;
      if (!binding) {
        boundPromptLabel.style.display = '';
        boundPromptLabel.textContent = 'Промпт по привязке не задан — прогон без промпта';
        return;
      }
      boundPromptLabel.style.display = '';
      boundPromptLabel.textContent = 'Промпт по умолчанию: ' + (binding.prompt_name || 'Prompt #' + binding.prompt_id);
    }

    if (promptSelect) {
      promptSelect.addEventListener('change', updateBoundPromptLabel);
    }

    // Тема меняется → пересчитать фактический промпт (ручной выбор
    // в селекте НЕ сбрасывается: value="" лишь значит «по привязке»).
    function applyTopicSelection(topicId) {
      updateBoundPromptLabel();
    }

    function setTopicOptions(topics) {
      if (!topicSelect) { return; }
      topicSelect.innerHTML = '';
      const ph = document.createElement('option');
      ph.value = '';
      ph.textContent = topics.length ? '— выберите тему (необязательно) —' : '— нет тем для этого языка —';
      topicSelect.appendChild(ph);
      topics.forEach(function (t) {
        const o = document.createElement('option');
        o.value = String(t.id);
        o.textContent = t.name || t.topic_name;
        topicSelect.appendChild(o);
      });
      topicSelect.disabled = !topics.length;
    }

    // Ручной выбор промпта фильтруется по выбранному языку: показываются
    // только промпты этого языка (topic.programming_language) или промпты
    // «на весь язык» (programming_language у самого промпта). Placeholder
    // остаётся всегда: value="" = «по привязке» (промпт по умолчанию).
    // Без выбранного языка селект выключен — выбирать нечего.
    function promptOptionLabel(p) {
      let suffix = '';
      if (p.topic_name) {
        suffix = ' — ' + p.topic_name;
      } else if (p.language_name) {
        suffix = ' — на весь язык (' + p.language_name + ')';
      }
      return (p.name || ('Prompt #' + p.id)) + suffix;
    }
    function rebuildPromptOptions() {
      if (!promptSelect) { return; }
      const lang = selectedLanguage();
      const previous = String(promptSelect.value || '');
      promptSelect.innerHTML = '';
      const ph = document.createElement('option');
      ph.value = '';
      ph.textContent = lang ? 'По привязке (по умолчанию)' : '— выберите язык —';
      promptSelect.appendChild(ph);
      if (lang) {
        allPrompts
          .filter(function (p) {
            // Промпт без языка (ни своего, ни через тему) — общий для всех
            // языков: показываем его при любом выборе. Промпты другого языка
            // скрываем — в селекте только выбранный язык.
            if (p.language_id === null || p.language_id === undefined || p.language_id === '') {
              return true;
            }
            return String(p.language_id) === String(lang.id);
          })
          .slice()
          .sort(function (a, b) { return promptOptionLabel(a).localeCompare(promptOptionLabel(b), 'ru'); })
          .forEach(function (p) {
            const o = document.createElement('option');
            o.value = String(p.id);
            o.textContent = promptOptionLabel(p);
            promptSelect.appendChild(o);
          });
      }
      promptSelect.disabled = !lang;
      // Если прежний ручной выбор выпал из нового набора языков — сброс на
      // «по привязке», чтобы на сервер не ушёл чужой язык промпт.
      const stillValid = Array.prototype.some.call(promptSelect.options, function (o) {
        return o.value === previous && previous !== '';
      });
      promptSelect.value = stillValid ? previous : '';
      updateBoundPromptLabel();
    }

    // Единственный язык страницы: он же управляет статусами дерева и
    // отправкой прогона (CMPA → статистика и прогон строго по CMPA).
    function selectedLanguage() {
      const select = document.getElementById('armLanguageSelect');
      if (!select || !select.value) { return null; }
      const opt = select.options[select.selectedIndex];
      return {
        id: select.value,
        ext: opt ? (opt.getAttribute('data-ext') || '') : '',
        name: opt ? (opt.getAttribute('data-name') || opt.textContent || '') : '',
      };
    }
    function syncLanguageSelection() {
      const lang = selectedLanguage();
      if (extensionInput) { extensionInput.value = lang ? lang.ext : ''; }
      const langTopics = lang
        ? allTopics
          .filter(function (t) { return String(t.programming_language) === String(lang.id); })
          .sort(function (a, b) {
            return String(a.name || a.topic_name || '').localeCompare(
              String(b.name || b.topic_name || ''), 'ru');
          })
        : [];
      if (topicSelect) {
        if (lang) {
          setTopicOptions(langTopics);
        } else {
          topicSelect.innerHTML = '';
          const ph = document.createElement('option');
          ph.value = '';
          ph.textContent = '— выберите язык —';
          topicSelect.appendChild(ph);
          topicSelect.disabled = true;
        }
      }
      rebuildPromptOptions();
      applyTopicSelection('');
      // Смена языка делает прежние флаги недействительными: перекрашиваем
      // (без перерисовки дерева — раскрытые папки и отметки сохраняются).
      taskStatusCache = new Map();
      updateTaskClasses();
      const nodeIds = treeNodeIds();
      if (lang && nodeIds.length) {
        setStatusMessage('Загрузка статусов...');
        loadTaskStatuses(nodeIds).catch(function (e) {
          console.error('ARM task statuses error:', e);
          setStatusMessage(e.message || 'Не удалось загрузить статусы', '#b42318');
        });
      } else if (!lang) {
        setStatusMessage('Выберите язык, чтобы увидеть статусы задач.');
      }
    }

    if (topicSelect) {
      topicSelect.addEventListener('change', function () {
        applyTopicSelection(this.value);
      });
    }

    // Единственный селектор языка: смена пересобирает темы/промпты и статусы.
    const languageSelect = document.getElementById('armLanguageSelect');
    if (languageSelect) {
      languageSelect.addEventListener('change', syncLanguageSelection);
    }
    // Кнопка прогона обновления флагов по DL (/get-solutions).
    const refreshStatusesBtn = document.getElementById('armRefreshStatusesBtn');
    if (refreshStatusesBtn) {
      refreshStatusesBtn.addEventListener('click', refreshTaskStatuses);
    }
    // Начальное состояние: расширение/тема/подпись привязки по выбранному языку.
    syncLanguageSelection();

    // «Выбрать все модели» одним кликом; три состояния чекбоса отражают
    // ручные отметки (всё / часть / ничего).
    function syncSelectAllModelsCheckbox() {
      if (!selectAllModelsCb) { return; }
      const boxes = document.querySelectorAll('input.arm-model-check');
      const total = boxes.length;
      let checked = 0;
      boxes.forEach(function (c) { if (c.checked) { checked++; } });
      selectAllModelsCb.checked = total > 0 && checked === total;
      selectAllModelsCb.indeterminate = checked > 0 && checked < total;
    }

    if (selectAllModelsCb) {
      selectAllModelsCb.addEventListener('click', function (e) { e.stopPropagation(); });
      selectAllModelsCb.addEventListener('change', function () {
        document.querySelectorAll('input.arm-model-check').forEach(function (c) {
          c.checked = selectAllModelsCb.checked;
        });
        syncSelectAllModelsCheckbox();
      });
    }
    document.querySelectorAll('input.arm-model-check').forEach(function (c) {
      c.addEventListener('change', syncSelectAllModelsCheckbox);
    });

    // Cancel button — отменяет ФОКУСИРОВАННЫЙ прогон (в «Процессах» можно
    // прервать любой построчно).
    async function pauseRunRequest(runId, resume) {
      const response = await fetch(resume ? '/ai/admin/arm/solve/resume/' : '/ai/admin/arm/solve/pause/', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'X-CSRFToken': getCsrfToken(), 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
        body: JSON.stringify({ run_id: runId }),
      });
      const data = await parseJsonResponse(response);
      if (!response.ok || !data.ok) { throw new Error(data.message || 'Ошибка'); }
      return data;
    }
    async function cancelRunRequest(runId) {
      const response = await fetch(cancelUrl, {
        method: 'POST', credentials: 'same-origin',
        headers: { 'X-CSRFToken': getCsrfToken(), 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
        body: JSON.stringify({ run_id: runId }),
      });
      const data = await parseJsonResponse(response);
      if (!response.ok || !data.ok) { throw new Error(data.message || 'Ошибка'); }
      return data;
    }
    if (cancelBtn) {
      cancelBtn.addEventListener('click', async function () {
        if (!focusedRunId) { return; }
        cancelBtn.disabled = true;
        cancelBtn.textContent = 'Прерывание...';
        try {
          await cancelRunRequest(focusedRunId);
          // Мгновенная отмена: бэкенд уже перевёл прогон в cancelled. Тянем
          // свежий snapshot сразу (без ожидания 2с поллинга) и показываем
          // «Прервано» с частичными результатами.
          await pollRunOnce(focusedRunId);
        } catch (error) {
          setError(error.message || 'Ошибка прерывания');
        }
        cancelBtn.disabled = false;
        cancelBtn.textContent = 'Прервать тестирование';
      });
    }

    function setCancelVisible(visible) {
      if (cancelBtn) {
        cancelBtn.style.display = visible ? '' : 'none';
        cancelBtn.disabled = false;
        cancelBtn.textContent = 'Прервать тестирование';
      }
    }

    // ── Download results (xlsx-матрица задача×модель) — серверный эндпоинт
    // /ai/admin/arm/solve/report/<run_id>/xlsx/, ссылка обновляется в
    // applyRunSnapshot. Клиентская CSV-генерация удалена (DRY: один
    // серверный генератор в ai/admin/export.py). ──

    async function postStart(body) {
      const response = await fetch(startUrl, {
        method: 'POST', credentials: 'same-origin',
        headers: { 'X-CSRFToken': getCsrfToken(), 'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8', 'X-Requested-With': 'XMLHttpRequest' },
        body: body.toString(),
      });
      const data = await parseJsonResponse(response);
      if (!response.ok || !data.ok) { throw new Error(data.message || 'Не удалось запустить batch solve'); }
      return data;
    }

    // ── «Перезапустить нерешённые пары»: попарный (задача × модель) rerun
    // нерешённых пар со слиянием в тот же прогон. Сервер берёт пары из БД
    // (verdict != solved) — клиент передаёт только run_id. Появляется после
    // завершения прогона; счётчик — число нерешённых пар.
    function countUnsolvedPairs(run) {
      const pairs = (run.results || []).filter(function (r) {
        return r && r.verdict !== 'solved';
      }).length;
      if (pairs > 0) { return pairs; }
      // Фолбэк для снапшотов без results: счётчики отчёта.
      const report = run.report || {};
      return (report.failed || 0) + (report.skipped || 0);
    }

    function updateRetryUnsolvedButton(run) {
      const retryBtn = document.getElementById('armSolveRetryUnsolvedBtn');
      if (!retryBtn) { return; }
      // У отладки промтов перезапуска нерешённых пар нет: прогон повторяется
      // целиком из формы (правки промпта уже применены).
      if (isPromptDebug) { retryBtn.style.display = 'none'; return; }
      const isDone = run.status === 'completed' || run.status === 'failed' || run.status === 'cancelled';
      // Во время перезапуска прогон снова 'running' — кнопка скрыта isDone-веткой.
      const pairs = isDone ? countUnsolvedPairs(run) : 0;
      const canRetry = isDone && pairs > 0 && run.run_id;
      if (!canRetry) { retryBtn.style.display = 'none'; return; }
      retryBtn.style.display = '';
      retryBtn.textContent = 'Перезапустить нерешённые пары (' + pairs + ')';
    }

    form.addEventListener('submit', async function (event) {
      event.preventDefault();
      if (submitBtn.disabled) { return; }
      // Язык обязателен (дублируем серверную валидацию). Ровно один язык:
      // он же выбран для статусов дерева — прогон идёт строго по нему.
      const lang = selectedLanguage();
      if (!lang) { setError('Выберите язык программирования.'); return; }
      setSubmitDisabled(true);
      setError('');
      // Старую таблицу не гасим: она останется до первого снапшота нового
      // прогона (при неудаче запуска таблица сохраняется — не теряем данные).
      setProgress('Запускаем ' + modeLabel + '...', true);
      const body = new URLSearchParams(new FormData(form));
      body.set('language_id', lang.id);
      body.set('file_extension', lang.ext);
      const runName = runNameInput ? runNameInput.value.trim() : '';
      body.set('run_name', runName);
      try {
        const data = await postStart(body);
        const runId = data.run_id || '';
        if (!runId) { throw new Error('Сервер не вернул run_id'); }
        trackRun(runId, {
          ext: lang.ext, name: runName, lang: lang.name, snapshot: data.run || {},
        });
        focusRun(runId);
        // Прогон должен попасть в меню «Процессы» в шапке сразу, а не на
        // следующем 15-секундном тике поллинга.
        document.dispatchEvent(new Event('ai-processes:refresh'));
      } catch (error) {
        setError(error.message || ('Ошибка запуска: ' + modeLabel));
        stopProgress();
        setCancelVisible(false);
      }
      setSubmitDisabled(false);
    });
    // Surface a server-rendered error (e.g. "процесс не найден") without the
    // now-removed |tojson filter — read the auto-escaped text content directly.
    const serverError = errorEl ? errorEl.textContent.trim() : '';
    if (serverError) { setError(serverError); }

    const initialEl = document.getElementById('arm-solve-initial-run');
    let initialRun = null;
    try { initialRun = JSON.parse(initialEl ? initialEl.textContent : '{}') || {}; } catch (e) { initialRun = {}; }
    if (initialRun && initialRun.run_id) {
      focusedRunId = initialRun.run_id;
      const rp = initialRun.run_params || {};
      // Строку в «Процессах» заводим сразу (в т.ч. для завершённого прогона):
      // она — якорь для переключения после запуска новых процессов.
      trackRun(initialRun.run_id, {
        ext: rp.file_extension || '',
        name: initialRun.run_name || rp.run_name || '',
        lang: rp.programming_language_name || '',
        snapshot: initialRun,
      });
      try { applyRunSnapshot(initialRun); } catch (e) { console.error('ARM render error:', e); }
      // Поллинг стартует в trackRun, если прогон живой. Форму не блокируем:
      // прогоны идут параллельно.
    }

    // Восстановление формы из снимка запуска (AIModelTestRun.run_params):
    // возврат на страницу прогона (?run_id=) не требует выставлять задачи/
    // модели/расширение/препромт заново. Отсутствующие в списке значения
    // (задача ушла из дерева, промпт скрыт) просто не отмечаются.
    // ── Сохранение выбранных задач дерева (localStorage) ──────────────────
    // Дерево создаётся динамически (после loadTree), поэтому общему механизму
    // ai_admin_state.js оно недоступно надёжно — чекбоксы задач помечены
    // data-no-state и сохраняются выделенно: courseId + node_ids одним ключом.
    const NODES_KEY = 'ai_arm_solve_nodes';

    function readSavedNodes() {
      try { return JSON.parse(localStorage.getItem(NODES_KEY) || 'null'); }
      catch (e) { return null; }
    }

    function saveSelectedNodes() {
      const courseId = courseIdInput ? courseIdInput.value.trim() : '';
      if (!courseId) { return; }
      const nodeIds = [];
      treeContainer.querySelectorAll('input.arm-task-check').forEach(function (cb) {
        if (cb.checked) { nodeIds.push(String(cb.value)); }
      });
      try {
        localStorage.setItem(NODES_KEY, JSON.stringify({ courseId: courseId, nodeIds: nodeIds }));
      } catch (e) { /* quota — состояние не критично */ }
    }

    function restoreNodes(nodeIds) {
      const wanted = new Set((nodeIds || []).map(String));
      let restored = 0;
      treeContainer.querySelectorAll('input.arm-task-check').forEach(function (cb) {
        if (wanted.has(String(cb.value))) { cb.checked = true; restored++; }
      });
      refreshAllFolderStates();
      if (wanted.size && restored < wanted.size) {
        loadTreeMsg.style.color = '#b54708';
        loadTreeMsg.textContent = 'Восстановлено ' + restored + ' из ' + wanted.size + ' задач';
      }
    }

    // ── Восстановление формы из снимка запуска (AIModelTestRun.run_params) ─
    // Только на странице прогона (?run_id=): возврат не требует выставлять
    // задачи/модели/язык/тему заново. Отсутствующие в списке значения (задача
    // ушла из дерева) просто не отмечаются. Старые run_params без language_id
    // деградируют мягко: язык подбирается по сохранённому расширению.
    // pendingNodeIds — узлы для отметки после loadTree (?run_id=).
    let pendingNodeIds = null;

    function restoreFormFromRunParams(rp) {
      try {
        if (rp.course_id) { courseIdInput.value = String(rp.course_id); }
        const wantedModels = new Set((rp.model_keys || []).map(String));
        document.querySelectorAll('input.arm-model-check').forEach(function (cb) {
          cb.checked = wantedModels.has(String(cb.value));
        });
        syncSelectAllModelsCheckbox();
        let languageRestored = false;
        const langSelect = document.getElementById('armLanguageSelect');
        if (langSelect && rp.language_id) {
          const wanted = String(rp.language_id);
          const exists = Array.prototype.some.call(
            langSelect.options, function (o) { return o.value === wanted; }
          );
          if (exists) {
            langSelect.value = wanted;
            languageRestored = true;
          }
        }
        if (langSelect && !languageRestored && rp.file_extension) {
          // Legacy run_params: подбор языка по расширению прогона.
          const byExt = Array.prototype.find.call(
            langSelect.options,
            function (o) { return o.getAttribute('data-ext') === rp.file_extension; }
          );
          if (byExt) {
            langSelect.value = byExt.value;
            languageRestored = true;
          }
        }
        syncLanguageSelection();
        if (languageRestored && rp.topic_id && topicSelect) {
          const topicExists = Array.prototype.some.call(
            topicSelect.options, function (o) { return o.value === String(rp.topic_id); }
          );
          if (topicExists) {
            topicSelect.value = String(rp.topic_id);
            applyTopicSelection(String(rp.topic_id));
          }
        }
        // Промпт прогона: ручной выбор из run_params (если опция ещё есть;
        // иначе «по привязке» — привязка могла смениться). Название прогона —
        // тоже снимок формы запуска.
        if (promptSelect) {
          const rpPromptId = String(rp.prompt_id || '');
          const promptExists = rpPromptId && Array.prototype.some.call(
            promptSelect.options, function (o) { return o.value === rpPromptId; }
          );
          promptSelect.value = promptExists ? rpPromptId : '';
        }
        // Число попыток (только отладка промтов) — снимок формы запуска.
        const attemptsInput = document.getElementById('armPromptAttempts');
        if (attemptsInput && rp.prompt_attempts != null && rp.prompt_attempts !== '') {
          attemptsInput.value = String(rp.prompt_attempts);
        }
        if (runNameInput) { runNameInput.value = String(rp.run_name || ''); }
        updateBoundPromptLabel();
        if (Array.isArray(rp.node_ids)) { pendingNodeIds = rp.node_ids; }
        if (courseIdInput.value.trim()) { loadTree(); }
      } catch (e) { /* восстановление — best effort, не ломаем страницу */ }
    }

    const isRunPage = new URLSearchParams(window.location.search).has('run_id');
    const initialRp = (initialRun && initialRun.run_params) || null;
    if (isRunPage && initialRp && Object.keys(initialRp).length) {
      restoreFormFromRunParams(initialRp);
    } else if (courseIdInput && courseIdInput.value.trim()) {
      // Без ?run_id= (страница «с прошлого запуска» или пустая): задачи
      // восстанавливаются из localStorage после загрузки дерева. course_id
      // и остальные поля восстанавливает ai_admin_state.js (change → loadTree).
      loadTree();
    }
  })();
