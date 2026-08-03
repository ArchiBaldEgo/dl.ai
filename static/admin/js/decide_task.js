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
    problemSelectors = initProblemSelectors();
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

// === window.onload — init для decide_task ===
window.onload = function () {
    console.log('Initializing WebSocket with client_id:', client_id);
    restoreInterfaceLanguage();
    restoreSelections();
    initWebSocket();
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