/**
 * Фронтенд страницы «Реши задачу» — WebSocket-чат с AI для решения задач dl.gsu.by.
 *
 * Общие функции (cookies, CSRF, voice, accordion, markdown, localization, resize,
 * sidebar, Enter handler, clearContext, initWebSocket default) берутся из ai-common.js,
 * который подключается ПЕРЕД этим файлом.
 *
 * Здесь только специфичная для «Реши задачу» логика:
 * - override initWebSocket() — type=2 и accordion logic в onmessage.
 * - override sendMessage() / simulateSend() — nodeId + progLng/topic/preprompt
 *   (выбор языка/темы/препромта теперь есть на этой странице; условие задачи
 *   подставляется из DL-ссылки через loadTaskFromUrl). Селекторы опциональны.
 * - selectLang change handler — обновление UI элементов + перелокализация селекторов.
 * - testOnDl() — кнопка «Тестирование»: последний код модели отправляется в DL
 *   (send-solution / get-solution-result), вердикт показывается жёлтым
 *   сообщением с подписью «DL» в общей истории (транзитное, без localStorage).
 * - DOMContentLoaded — initProblemSelectors (общая логика из ai-common.js) +
 *   автозагрузка условия задачи из DL-ссылки (loadTaskFromUrl).
 * - window.onload — init для decide_task.
 */

// Селекторы языка программирования / темы / препромпта — общая логика в ai-common.js.
// Файловая переменная, чтобы обработчик selectLang change (отдельная DOMContentLoaded)
// мог дёрнуть repopulateOnUiLanguageChange().
let problemSelectors = null;

// True, если пользователь вручную правил поле условия задачи. Программная
// установка .value событие 'input' не генерирует, поэтому флаг взводится только
// на реальный ввод с клавиатуры — и используется, чтобы не затирать правки
// пользователя при перезагрузке условия после смены языка интерфейса.
let userEditedTaskText = false;

// Безопасное чтение значения селектора (на случай рассинхрона версий JS/шаблона).
function getSelectorValue(id) {
    var el = document.getElementById(id);
    return el ? el.value : "";
}

// === override initWebSocket — type=2, accordion в onmessage ===
function initWebSocket() {
    try {
        var wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        var wsUrl = wsProtocol + '//' + window.location.host + '/ai/chat/ws/' + client_id + window.location.search;

        console.log('Connecting to WebSocket:', wsUrl);
        ws = new WebSocket(wsUrl);

        ws.onopen = function(event) {
            console.log('WebSocket connection established');
            updateVoiceStatus(getVoiceStatusText('connectionEstablished'));
        };

        ws.onmessage = function(event) {
            // Восстановленный [[DL]]-статус (см. persistDlMessage в ai-common.js) —
            // не реплика диалога: рендерим жёлтым DL-сообщением и выходим.
            var dlText = parsePersistedDlMessage(event.data);
            if (dlText !== null) {
                _appendDlMessage(dlText);
                return;
            }
            appendPersistedMessage(event.data);
            var messages = document.getElementById('messages');
            var message = document.createElement('li');
            var parsed = parseThinkTag(event.data);
            var inThinkTag = document.createElement('div');
            inThinkTag.classList.add('think');
            inThinkTag.innerHTML = parsed.thinkContent;

            var mainMess = document.createElement('div');
            mainMess.innerHTML = parsed.remainingText;
            var mainMessText = mainMess.innerText || "";
            var parsedHTML = convertMarkdownToHTML(mainMessText);
            var messageContent = document.createElement('div');
            messageContent.innerHTML = parsedHTML;

            if (parsed.thinkContent) message.appendChild(inThinkTag);
            message.appendChild(messageContent);
            messages.appendChild(message);
            messages.scrollTo({ top: messages.scrollHeight, behavior: 'smooth' });

            if (isTerminalAiMessage(event.data)) {
                setRequestLock(false);
                notEnter = false;
            }

            initAccordionForMessages();
            collapseAllExceptLast();
        };

        ws.onerror = function(error) {
            console.error('WebSocket error:', error);
            updateVoiceStatus(getVoiceStatusText('wsError'));
            setRequestLock(false);
            notEnter = false;
        };

        ws.onclose = function(event) {
            console.log('WebSocket connection closed');
            updateVoiceStatus(getVoiceStatusText('connectionClosed'));
            setRequestLock(false);
            notEnter = false;
        };
    } catch (error) {
        console.error('Error initializing WebSocket:', error);
    }
}

// === override sendMessage — с nodeId (язык/тема/препромт убраны: на этой
//     странице они не выбираются) ===
function sendMessage(event) {
    event.preventDefault();
    if (!ws) {
        console.log("WebSocket is not initialized");
        alert("Соединение не установлено. Пожалуйста, подождите...");
        return;
    }

    if (ws.readyState !== WebSocket.OPEN) {
        console.log("WebSocket is not open. State:", ws.readyState);
        alert("Соединение не установлено. Пожалуйста, подождите...");
        return;
    }

    if (requestInFlight) {
        alert("Дождитесь ответа модели перед новым запросом.");
        return;
    }

    var value = document.querySelector("#select").value;
    var language = document.querySelector("#selectLang").value;
    var input = document.getElementById("messageText");
    var progLng = getSelectorValue("selectProgLng");
    var topic = getSelectorValue("selectTheme");
    var preprompt = getSelectorValue("selectPrompt");

    if (!value) {
        alert("Сегодня нет доступных моделей. Повторите позже.");
        return;
    }

    if (!input.value.trim()) {
        alert("Пожалуйста, введите сообщение");
        return;
    }

    if (!confirmTopicWithoutPreprompt()) return;

    ws.send(JSON.stringify({
        type: '2',
        message: input.value,
        value: value,
        language: language,
        progLng: progLng,
        topic: topic,
        preprompt: preprompt,
        nodeId: window.AI_TASK_NODE_ID || ''
    }));
    setRequestLock(true);
    notEnter = true;
    // НЕ очищаем поле ввода в режиме «Реши задачу» — условие задачи
    // должно сохраняться при смене модели ИИ (пользовательские жалобы).
    saveSharedText();
}

// === override simulateSend — с nodeId (язык/тема/препромт убраны) ===
function simulateSend() {
    if (!ws || ws.readyState !== WebSocket.OPEN) {
        updateVoiceStatus(getVoiceStatusText('connectionError'));
        return;
    }

    if (requestInFlight) {
        updateVoiceStatus(getVoiceStatusText('waitForModel'));
        return;
    }

    var value = document.querySelector("#select").value;
    var language = document.querySelector("#selectLang").value;
    var input = document.getElementById("messageText");
    var progLng = getSelectorValue("selectProgLng");
    var topic = getSelectorValue("selectTheme");
    var preprompt = getSelectorValue("selectPrompt");

    if (!input.value.trim()) {
        return;
    }

    if (!confirmTopicWithoutPreprompt()) return;

    ws.send(JSON.stringify({
        type: '2',
        message: input.value,
        value: value,
        language: language,
        progLng: progLng,
        topic: topic,
        preprompt: preprompt,
        nodeId: window.AI_TASK_NODE_ID || ''
    }));

    setRequestLock(true);
    notEnter = true;
    updateVoiceStatus(getVoiceStatusText('messageSent'));
    // НЕ очищаем поле ввода в режиме «Реши задачу» — условие задачи
    // должно сохраняться при смене модели ИИ.
    saveSharedText();
}

// === selectLang change handler — специфичный для decide_task ===
document.addEventListener("DOMContentLoaded", function() {
    var selectLangEl = document.getElementById("selectLang");
    if (selectLangEl) {
        selectLangEl.addEventListener("change", async function () {
            var selectedLang = this.options[this.selectedIndex].getAttribute("language");

            var submitBtn = document.querySelector("button[type='submit']");
            if (submitBtn) submitBtn.textContent = localization[selectedLang].send;

            var clearBtn = document.querySelector("button[onclick='clearContext()']");
            if (clearBtn) clearBtn.textContent = localization[selectedLang].clear;

            var testOnDlBtn = document.getElementById("testOnDlBtn");
            if (testOnDlBtn) testOnDlBtn.textContent = localization[selectedLang].dlTestButton;

            var messageTextEl = document.getElementById("messageText");
            if (messageTextEl) messageTextEl.setAttribute("placeholder", localization[selectedLang].placeholder);

            var sidebarHeader = document.querySelector(".sidebar-header");
            if (sidebarHeader) sidebarHeader.textContent = localization[selectedLang].adminPanel;

            var testPanelLink = document.getElementById("testPanelLink");
            if (testPanelLink) testPanelLink.textContent = localization[selectedLang].testPanel;

            var selectType1 = document.querySelector("#selectType option:nth-child(1)");
            if (selectType1) selectType1.textContent = localization[selectedLang].chat;
            var selectType2 = document.querySelector("#selectType option:nth-child(2)");
            if (selectType2) selectType2.textContent = localization[selectedLang].decideTask;
            var selectType3 = document.querySelector("#selectType option:nth-child(3)");
            if (selectType3) selectType3.textContent = localization[selectedLang].findError;

            var checkTextEl = document.querySelector(".check-text");
            if (checkTextEl) checkTextEl.textContent = localization[selectedLang].enterHint;

            var prepromptEl = document.querySelector(".preprompt");
            if (prepromptEl) prepromptEl.textContent = localization[selectedLang].preprompt;

            updateAccordionLabels();
            updateLastUpdateLabel();

            var voiceModeBtn = document.getElementById("voiceModeBtn");
            if (voiceModeBtn) voiceModeBtn.textContent = localization[selectedLang].voiceMode;

            var voiceInputBtn = document.getElementById("voiceInputBtn");
            if (voiceInputBtn) voiceInputBtn.textContent = localization[selectedLang].voiceInput;

            var voiceOutputBtn = document.getElementById("voiceOutputBtn");
            if (voiceOutputBtn) voiceOutputBtn.textContent = localization[selectedLang].voiceOutput;

            var speakThinkLabel = document.getElementById("speakThinkLabel");
            if (speakThinkLabel) speakThinkLabel.textContent = localization[selectedLang].speakThinkLabel;

            // Перелокализация селекторов языка/темы/препромпта в новом UI-языке.
            if (problemSelectors) await problemSelectors.repopulateOnUiLanguageChange();
            saveInterfaceLanguage();

            // Условие задачи должно следовать за выбранным языком интерфейса:
            // перезагружаем его в новом языке. Не затираем поле, если пользователь
            // уже вручную его правил (userEditedTaskText).
            if (window.AI_TASK_NODE_ID && !userEditedTaskText) {
                await loadTaskFromUrl();
            }

            updateVoiceStatus(getVoiceStatusText('readyForVoice'));
        });
    }
});

// === DOMContentLoaded — инициализация селекторов языка/темы/препромпта ===
// Общая логика в ai-common.js (initProblemSelectors): populate + change handlers
// + savePageState/restorePageState. Возвращает handle для repopulateOnUiLanguageChange.
document.addEventListener("DOMContentLoaded", function() {
    problemSelectors = initProblemSelectors('solve');
});

// === Автозагрузка условия задачи из DL-ссылки ===
// Объявлено на уровне модуля (а не внутри замыкания DOMContentLoaded), чтобы
// обработчик selectLang change (другое замыкание, ниже) и синтетический change
// в window.onload могли вызывать loadTaskFromUrl() — иначе ReferenceError и
// условие не перезагружалось при смене языка интерфейса.
function currentUiLanguage() {
    var selectLangEl = document.getElementById('selectLang');
    if (selectLangEl && selectLangEl.selectedIndex >= 0) {
        return selectLangEl.options[selectLangEl.selectedIndex].getAttribute('language') || 'Russian';
    }
    return 'Russian';
}

async function loadTaskFromUrl() {
    var messageTextEl = document.getElementById('messageText');
    var nodeId = window.AI_TASK_NODE_ID || (messageTextEl && messageTextEl.dataset.nodeId) || '';
    if (!nodeId) return false;

    try {
        var url = new URL('/ai/api/task-info/', window.location.origin);
        url.searchParams.set('nodeId', nodeId);
        url.searchParams.set('removeHtmlTags', 'true');
        // Текст задачи должен быть на языке интерфейса, выбранном на странице
        // (русский/английский/французский): сервер переведёт условие в этот язык.
        url.searchParams.set('ui_language', currentUiLanguage());
        var response = await fetch(url.toString());
        if (response.status === 404) {
            updateVoiceStatus(getUiString('taskNotFound', 'Задача не найдена'));
            return false;
        }
        if (!response.ok) {
            var errorText = await response.text();
            throw new Error('HTTP ' + response.status + ': ' + errorText);
        }
        var data = await response.json();
        var statement = data.statement || data.currentStatement || '';
        if (messageTextEl) {
            messageTextEl.value = statement;
            saveSharedText();
        }
        return true;
    } catch (error) {
        console.error('Error loading task statement:', error);
        updateVoiceStatus(getUiString('taskLoadError', 'Не удалось загрузить условие задачи'));
        return false;
    }
}

document.addEventListener("DOMContentLoaded", async () => {
    var messageText = document.getElementById('messageText');
    if (messageText) messageText.addEventListener('input', function () {
        userEditedTaskText = true;
        saveSharedText();
    });

    var taskLoaded = await loadTaskFromUrl();
    if (!taskLoaded) {
        restoreSharedText();
    }
});

// === Кнопка «Тестирование» — отправка последнего кода модели в DL ===
// Код последнего ответа ИИ достаётся из сохранённой переписки (localStorage,
// ai-common.js): маркер «Запрос успешно обработан», think-блок вырезается
// вручную (без escapeHtml — код вида #include <iostream> не должен искажаться),
// берётся последний fenced-блок ```. Отправка — POST /ai/api/send-solution/
// (расширение файла резолвится серверно из выбранного языка программирования),
// результат — поллинг /ai/api/get-solution-result/ раз в 3 с. Вердикт DL
// показывается в истории чата сообщением data-role="dl" (жёлтое, подпись
// «DL») и ПЕРСИСТИРУЕТСЯ в localStorage [[DL]]-записью (см. persistDlMessage
// в ai-common.js) — после перезагрузки восстанавливается, а незавершённый
// поллинг возобновляется по сохранённому queueId (_dlQueueKey).

var DL_POLL_INTERVAL_MS = 3000;
var DL_POLL_MAX_ATTEMPTS = 40; // 40 × 3 с ≈ 2 минуты на всю проверку

var _dlTestRunning = false;

// THINK_OPEN/THINK_CLOSE собираются конкатенацией: цельный литерал тега в
// исходнике хрупок (терялся при правках). THINK_OPEN = '<think>',
// THINK_CLOSE = '</think>'.
var THINK_OPEN = '<' + 'think>';
var THINK_CLOSE = '<' + '/think>';

// Прозовые оградки (цепочка рассуждений модели со случайно закрывшейся ```)
// пропускаем при поиске кода — тот же порог, что в ai/arm_runner.py
// (_looks_like_prose): доля функциональных слов EN/RU >= 0.15 при >= 12
// совпадениях и >= 20 словах. Ключевые слова Pascal/ассемблера в стоп-лист
// не входят, чтобы настоящий код не браковался как проза.
function _looksLikeProse(text) {
    var words = String(text).match(/[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё]+/g) || [];
    if (words.length < 20) return false;
    var stop = {
        we:1, need:1, needs:1, the:1, this:1, that:1, these:1, those:1,
        it:1, its:1, is:1, are:1, was:1, were:1, be:1, been:1, being:1,
        our:1, their:1, they:1, them:1, have:1, has:1, had:1,
        can:1, could:1, will:1, would:1, should:1, must:1, shall:1,
        may:1, might:1, also:1, but:1, because:1, which:1, what:1,
        how:1, why:1, when:1, where:1, there:1, here:1, into:1, from:1,
        per:1, via:1, please:1, note:1, just:1, very:1, more:1, most:1,
        some:1, any:1, each:1, both:1, one:1, two:1, now:1, so:1, all:1,
        нужно:1, нужен:1, нужна:1, если:1, чтобы:1, это:1, этот:1, эта:1,
        как:1, или:1, также:1, должен:1, должна:1, можно:1, нельзя:1,
        потом:1, затем:1, поэтому:1, который:1, которая:1, быть:1, было:1,
        будут:1, может:1, наш:1, наши:1, они:1, она:1, его:1, их:1,
        для:1, при:1, всё:1, все:1, так:1, вот:1, есть:1, там:1, где:1,
        когда:1, почему:1, какой:1
    };
    var hits = 0;
    for (var i = 0; i < words.length; i++) {
        if (stop[words[i].toLowerCase()]) hits++;
    }
    return hits >= 12 && hits / words.length >= 0.15;
}

function extractLastAiCode() {
    var arr = loadPersistedMessages();
    for (var i = arr.length - 1; i >= 0; i--) {
        var raw = String(arr[i] || '');
        if (raw.indexOf('Запрос успешно обработан') === -1) continue;
        // Шапка ответа обёрнута в think-тег (consumers.py format_success) —
        // отрезаем всё до первого закрывающего; в самом ответе модели могут
        // быть свои think-блоки — вырезаем и их, чтобы код из «мыслей» не
        // ушёл в тестирование. parseThinkTag из ai-common.js не используем:
        // он прогоняет текст через escapeHtml и искажает код (#include <iostream>).
        var endIdx = raw.indexOf(THINK_CLOSE);
        var text = endIdx !== -1 ? raw.substring(endIdx + THINK_CLOSE.length) : raw;
        var openIdx;
        while ((openIdx = text.indexOf(THINK_OPEN)) !== -1) {
            var closeIdx = text.indexOf(THINK_CLOSE, openIdx);
            text = closeIdx !== -1
                ? text.substring(0, openIdx) + text.substring(closeIdx + THINK_CLOSE.length)
                : text.substring(0, openIdx);
        }
        var re = /```[^\n]*\n?([\s\S]*?)```/g;
        var match, lastCode = null;
        while ((match = re.exec(text)) !== null) {
            if (match[1].trim() && !_looksLikeProse(match[1])) lastCode = match[1];
        }
        if (lastCode) return lastCode.trim();
    }
    return null;
}

// Транзитное DL-сообщение в общей истории: <li data-role="dl"> — accordion
// (initAccordionForMessages) повесит на него жёлтые классы msg-dl/accordion-dl.
// Возвращает текстовый узел-контейнер для последующих обновлений статуса.
function _appendDlMessage(text) {
    var messages = document.getElementById('messages');
    var message = document.createElement('li');
    message.dataset.role = 'dl';
    var content = document.createElement('div');
    content.textContent = text;
    message.appendChild(content);
    messages.appendChild(message);
    messages.scrollTo({ top: messages.scrollHeight, behavior: 'smooth' });
    initAccordionForMessages();
    collapseAllExceptLast();
    return content;
}

// Пользователь мог нажать «Очистить контекст» (или страницу перезагрузили) —
// в этом случае узел оторван от DOM и обновлять/поллить больше нечего.
function _setDlMessageText(node, text) {
    if (!node || !node.isConnected) return;
    node.textContent = text;
}

// Обновление DL-статуса: узел в DOM + та же [[DL]]-запись в localStorage
// (после перезагрузки сообщение восстанавливается уже финальным текстом).
function _setDlMessageTextPersist(node, text) {
    _setDlMessageText(node, text);
    persistDlMessage(text);
}

async function _pollDlResult(queueId, statusNode) {
    // queueId переживает перезагрузку (ai_dl_queue_<nodeId>): вердикт
    // до-поллится при следующем открытии страницы, даже если эту закрыли.
    function _forgetQueue() {
        try { localStorage.removeItem(_dlQueueKey(window.AI_TASK_NODE_ID || '')); } catch (e) {}
    }
    for (var attempt = 0; attempt < DL_POLL_MAX_ATTEMPTS; attempt++) {
        if (!statusNode || !statusNode.isConnected) return;
        try {
            var response = await fetch('/ai/api/get-solution-result/', {
                method: 'POST',
                credentials: 'same-origin',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': getCsrfToken()
                },
                body: JSON.stringify({ queueId: queueId })
            });
            var data = await response.json().catch(function () { return null; });
            if (data && data.isFinished === true) {
                var comment = String(data.comment || '').trim();
                _setDlMessageTextPersist(statusNode,
                    comment ? 'DL: ' + comment
                            : getUiString('dlEmptyComment', 'DL: тестирование завершено'));
                _forgetQueue();
                return;
            }
            // 401/403 (протухшая сессия) и 404 (решения нет в DL) — ждать
            // бессмысленно; остальные ошибки (502/503 DL-апстрима) считаем
            // временными и продолжаем до лимита.
            if (response.status === 401 || response.status === 403 || response.status === 404) {
                _setDlMessageTextPersist(statusNode,
                    'DL: ' + ((data && data.error) || 'результат тестирования недоступен'));
                _forgetQueue();
                return;
            }
        } catch (error) {
            console.error('DL poll error:', error); // разовый сбой сети — не прерываем
        }
        await new Promise(function (resolve) { setTimeout(resolve, DL_POLL_INTERVAL_MS); });
    }
    // Таймаут: ключ НЕ удаляем — DL может завершиться позже, вердикт
    // до-поллится при следующем открытии страницы.
    _setDlMessageTextPersist(statusNode,
        getUiString('dlTimeout', 'DL: тестирование не завершилось за отведённое время'));
}

// Активный poll DL переживает перезагрузку страницы: queueId хранится в
// localStorage, при следующем открытии поллинг возобновляется (record_result
// на сервере сработает по isFinished). «Очистить контекст» запущенный тест
// не отменяет — ключ при clearContext не трогаем.
function _dlQueueKey(nodeId) { return 'ai_dl_queue_' + nodeId; }

async function testOnDl() {
    if (_dlTestRunning) return;

    var nodeId = window.AI_TASK_NODE_ID || '';
    if (!nodeId) {
        alert(getUiString('dlNoNode', 'Страница открыта без задачи — тестирование недоступно'));
        return;
    }

    var langSelect = document.getElementById('selectProgLng');
    var langId = langSelect ? langSelect.value : '';
    if (!langId) {
        alert(getUiString('dlNoLanguage',
            'Выберите язык программирования — по нему определяется расширение файла'));
        return;
    }
    var langName = (langSelect.selectedIndex >= 0)
        ? (langSelect.options[langSelect.selectedIndex].text || '') : '';

    var code = extractLastAiCode();
    if (!code) {
        alert(getUiString('dlNoCode', "В последнем ответе модели не найден блок кода"));
        return;
    }

    var btn = document.getElementById('testOnDlBtn');
    _dlTestRunning = true;
    if (btn) btn.disabled = true;

    var dlTesting = getUiString('dlTesting', 'DL: тестирование…');
    var statusNode = _appendDlMessage(dlTesting);
    persistDlMessage(dlTesting); // переживает перезагрузку ([[DL]]-запись)
    try {
        var response = await fetch('/ai/api/send-solution/', {
            method: 'POST',
            credentials: 'same-origin',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': getCsrfToken()
            },
            body: JSON.stringify({
                nodeId: nodeId,
                code: code,
                progLanguageId: langId,
                progLanguageName: langName
            })
        });
        var data = await response.json().catch(function () { return null; });
        if (!response.ok || !data || !(data.queueId > 0)) {
            var detail = (data && data.error) ? (': ' + data.error) : '';
            _setDlMessageTextPersist(statusNode,
                getUiString('dlSendFailed', 'DL: не удалось отправить решение на тестирование') + detail);
            return;
        }
        try { localStorage.setItem(_dlQueueKey(nodeId), String(data.queueId)); } catch (e) {}
        await _pollDlResult(data.queueId, statusNode);
    } catch (error) {
        console.error('DL test error:', error);
        _setDlMessageTextPersist(statusNode,
            getUiString('dlSendFailed', 'DL: не удалось отправить решение на тестирование'));
    } finally {
        _dlTestRunning = false;
        if (btn) btn.disabled = false;
    }
}

// Незавершённый poll DL (страницу закрыли посреди «тестирования…»):
// возобновляем по сохранённому queueId. DL-сообщение уже восстановлено из
// localStorage ([[DL]]-запись) — обновляем его же узел, а при отсутствии
// (например, после «Очистить контекст») создаём новое.
function _resumePendingDlPoll() {
    var nodeId = window.AI_TASK_NODE_ID || '';
    var queueId = 0;
    try {
        queueId = parseInt(localStorage.getItem(_dlQueueKey(nodeId)), 10) || 0;
    } catch (e) {}
    if (!queueId) return;
    if (_dlTestRunning) return; // параллельный ручной прогон уже идёт
    var dlNodes = document.querySelectorAll('#messages li[data-role="dl"]');
    var content = dlNodes.length ? dlNodes[dlNodes.length - 1].querySelector('div') : null;
    if (!content) content = _appendDlMessage(getUiString('dlTesting', 'DL: тестирование…'));
    _pollDlResult(queueId, content);
}

// === window.onload — init для decide_task ===
window.onload = function () {
    console.log('Initializing WebSocket with client_id:', client_id);
    restoreInterfaceLanguage();
    restoreSelections();
    initWebSocket();
    restorePersistedMessages();
    _resumePendingDlPoll();
    document.getElementById("selectLang").dispatchEvent(new Event("change"));
    initAccordionForMessages();
    updateVoiceStatus(getVoiceStatusText('ready'));
    initSelectionPersistence();
    initModelLimitsWidget();

    var speakThinkCheckbox = document.getElementById('speakThinkContent');
    if (speakThinkCheckbox) {
        speakThinkCheckbox.addEventListener('change', function() {
            speakThinkEnabled = this.checked;
        });
    }
};