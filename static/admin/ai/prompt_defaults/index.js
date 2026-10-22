// Логика страницы «Промпты по умолчанию (ARM)».
// Вынесена из ai/templates/admin/ai/prompt_defaults.html.
// Шаблонных переменных здесь нет: данные приходят через json_script-блоки
// (pd-languages-data / pd-topics-data / pd-prompts-data / pd-editing-data),
// которые рендерит шаблон ДО подключения этого файла.
// NB: скрипт подключается в конце блока content — в extrahead он
// выполнялся бы в <head> до отрисовки body, и getElementById вернул бы null.
(function () {
  'use strict';
  // Скрипт обязан лежать в конце content (после json_script-блоков):
  // в extrahead он выполняется в <head> до отрисовки body, все
  // getElementById возвращают null и IIFE умирает целиком.
  // Языки отрендерены сервером прямо в <select> — JS их не строит.

  // Тот же контракт, что у всех AJAX-хендлеров админки: протухшая сессия
  // редиректит fetch на страницу логина (HTML) — показываем понятную ошибку.
  function parseJsonResponse(response) {
    return response.text().then(function (text) {
      if (!text || !text.trim()) { throw new Error('Сессия истекла. Обновите страницу и войдите заново.'); }
      try { return JSON.parse(text); }
      catch (e) { throw new Error('Сессия истекла. Обновите страницу и войдите заново.'); }
    });
  }

  function showMessage(text, isError) {
    var el = document.getElementById('pdMessage');
    el.style.display = '';
    el.style.background = isError ? '#fee2e2' : '#dcfce7';
    el.style.color = isError ? '#991b1b' : '#166534';
    el.textContent = text;
  }

  var languageSelect = document.getElementById('pdLanguage');
  var topicSelect = document.getElementById('pdTopic');
  var promptSelect = document.getElementById('pdPrompt');
  var saveBtn = document.getElementById('pdSaveBtn');
  var form = document.getElementById('pdForm');
  var topicsData = JSON.parse(document.getElementById('pd-topics-data').textContent || '[]');
  var promptsData = JSON.parse(document.getElementById('pd-prompts-data').textContent || '[]');
  var editing = JSON.parse(document.getElementById('pd-editing-data').textContent || 'null');

  function fillTopics(languageId, selectedId) {
    topicSelect.innerHTML = '';
    // Пустая тема — валидный выбор: привязка «на весь язык».
    topicSelect.appendChild(new Option('— для всего языка —', ''));
    topicsData.forEach(function (t) {
      if (String(t.language_id) === String(languageId)) {
        topicSelect.appendChild(new Option(t.name, String(t.id)));
      }
    });
    topicSelect.disabled = !languageId;
    if (selectedId) {
      topicSelect.value = String(selectedId);
    } else {
      topicSelect.value = '';
    }
  }

  var modeSelect = document.getElementById('pdMode');

  function fillPrompts(topicId, selectedId) {
    promptSelect.innerHTML = '';
    promptSelect.appendChild(new Option('— выберите промпт —', ''));
    // Для темы — только её промпты; «на весь язык» — любой промпт.
    // Плюс фильтр по режиму привязки: у привязки «Поиск ошибки» предлагаются
    // только промпты режима «В чём ошибка» (и наоборот).
    var selectedMode = modeSelect ? modeSelect.value : '';
    promptsData.forEach(function (p) {
      if (selectedMode && p.mode && String(p.mode) !== String(selectedMode)) { return; }
      if (!topicId || String(p.topic_id) === String(topicId)) {
        promptSelect.appendChild(new Option(p.name, String(p.id)));
      }
    });
    promptSelect.disabled = promptSelect.options.length <= 1;
    if (selectedId) { promptSelect.value = String(selectedId); }
  }

  languageSelect.addEventListener('change', function () {
    fillTopics(this.value);
    fillPrompts('');
  });
  topicSelect.addEventListener('change', function () { fillPrompts(this.value); });
  if (modeSelect) {
    modeSelect.addEventListener('change', function () {
      fillPrompts(topicSelect.value);
    });
  }

  // Prefill при редактировании ?edit=<id>: язык уже в HTML, ставим value.
  // Режим — ДО fillPrompts, чтобы дропдаун сразу был отфильтрован по нему.
  if (editing) {
    languageSelect.value = String(editing.language_id);
    if (modeSelect) { modeSelect.value = editing.mode; }
    fillTopics(editing.language_id, editing.topic_id);
    fillPrompts(editing.topic_id, editing.prompt_id);
  }

  function getCookie(name) {
    var match = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
    return match ? decodeURIComponent(match[1]) : '';
  }

  function getCsrfToken() {
    // Сперва СВЕЖАЯ кука csrftoken (middleware ротирует токен при ребинде
    // сессии — токен в уже отрендеренной форме успевает устареть, и все
    // прочие AJAX-хендлеры админки читают именно куку), потом инпут.
    var csrfInput = document.querySelector('input[name="csrfmiddlewaretoken"]');
    return getCookie('csrftoken') || (csrfInput ? csrfInput.value : '');
  }

  function post(action, fields) {
    var body = new URLSearchParams();
    body.set('action', action);
    Object.keys(fields).forEach(function (key) { body.set(key, fields[key]); });
    // getAttribute, а НЕ form.action: в форме есть <input name="action">,
    // и именованные поля затеняют свойства DOM — form.action возвращал бы
    // сам инпут ("[object HTMLInputElement]"), и POST уходил на 404.
    return fetch(form.getAttribute('action'), {
      method: 'POST', credentials: 'same-origin',
      headers: {
        'X-CSRFToken': getCsrfToken(),
        'Content-Type': 'application/x-www-form-urlencoded',
        'X-Requested-With': 'XMLHttpRequest',
      },
      body: body.toString(),
    }).then(parseJsonResponse);
  }

  form.addEventListener('submit', function (event) {
    event.preventDefault();
    // Тема необязательна: пустая = привязка «на весь язык».
    if (!languageSelect.value || !promptSelect.value) {
      showMessage('Выберите язык и промпт.', true);
      return;
    }
    saveBtn.disabled = true;
    post('save', {
      language_id: languageSelect.value,
      topic_id: topicSelect.value,
      mode: document.getElementById('pdMode').value,
      prompt_id: promptSelect.value,
    }).then(function (data) {
      if (!data.ok) { throw new Error(data.error || 'Не удалось сохранить привязку'); }
      showMessage(data.message || 'Сохранено', false);
      // Перегрузка обновляет таблицу привязок (простой и честный путь).
      window.setTimeout(function () { window.location.reload(); }, 400);
    }).catch(function (error) {
      showMessage(error.message || 'Ошибка', true);
      saveBtn.disabled = false;
    });
  });

  document.querySelectorAll('.pd-delete-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      if (!window.confirm('Удалить привязку?')) { return; }
      post('delete', { binding_id: btn.getAttribute('data-id') })
        .then(function (data) {
          if (!data.ok) { throw new Error(data.error || 'Не удалось удалить'); }
          window.location.reload();
        })
        .catch(function (error) { showMessage(error.message || 'Ошибка', true); });
    });
  });
})();
