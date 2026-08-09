/**
 * Фронтенд страницы «В чём ошибка?» — WebSocket-чат с AI для поиска ошибок в коде.
 *
 * Общие функции (cookies, CSRF, voice, accordion, markdown, localization, etc.)
 * берутся из ai-common.js. Включая логику селекторов языка программирования /
 * темы / препромпта (initProblemSelectors / fetchProblemData) — она расшарена
 * с decide_task.js (DRY, см. CLAUDE.md). Этот файл содержит только переопределения:
 * - initWebSocket() — type=3 и accordion logic в onmessage
 * - sendMessage() — taskText, codeText, progLng, topic, preprompt, type=3
 * - simulateSend() — taskText, codeText, progLng, type=3
 * - selectLang change handler — специфичный для find_error
 * - DOMContentLoaded — initProblemSelectors + input listeners + selectLang handler
 * - window.onload — init для find_error
 */

// === Переопределение initWebSocket (type=3, accordion logic в onmessage) ===

function initWebSocket() {
    try {
        var wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        var wsUrl = `${wsProtocol}//${window.location.host}/ai/chat/ws/${client_id}${window.location.search}`;

        console.log('Connecting to WebSocket:', wsUrl);
        ws = new WebSocket(wsUrl);

        ws.onopen = function(event) {
            console.log('WebSocket connection established');
            updateVoiceStatus(getVoiceStatusText('connectionEstablished'));
        };

        ws.onmessage = function (event) {
            var messages = document.getElementById('messages');
            var message = document.createElement('li');
            var inThinkTag = document.createElement('div');
            inThinkTag.classList.add('think');
            inThinkTag.innerHTML = parseThinkTag(event.data).thinkContent;
            var mainMess = document.createElement('div');
            mainMess.innerHTML = parseThinkTag(event.data).remainingText;
            const mainMessText = mainMess.innerText || "";
            const parsedHTML = convertMarkdownToHTML(mainMessText);
            const messageContent = document.createElement('div');
            messageContent.innerHTML = parsedHTML;

            // Accordion logic
            const allMessages = messages.querySelectorAll(':scope > li');
            const roles = [];
            for (let i = 0; i < allMessages.length; i++) {
                if (i % 2 === 0) roles.push('user');
                else roles.push('assistant');
            }

            allMessages.forEach(function (li, idx) {
                if (!li.classList.contains('accordion-li') && !li.querySelector('.accordion')) {
                    li.classList.add('accordion-li');
                    const role = roles[idx] || 'other';
                    li.classList.remove('msg-user', 'msg-assistant');
                    if (role === 'user') li.classList.add('msg-user');
                    if (role === 'assistant') li.classList.add('msg-assistant');

                    const btn = document.createElement('button');
                    btn.className = 'accordion';
                    if (role === 'user') btn.classList.add('accordion-user');
                    if (role === 'assistant') btn.classList.add('accordion-assistant');

                    const selectLang = document.getElementById('selectLang');
                    const langAttr = selectLang.options[selectLang.selectedIndex].getAttribute('language');
                    const roleLabels = {
                        Russian: { user: 'Вы', assistant: 'Ассистент', other: 'Други' },
                        English: { user: 'You', assistant: 'Assistant', other: 'Others' },
                        French: { user: 'Vous', assistant: 'Assistant', other: 'Autres' }
                    };

                    function getRoleLabel(role, lang) {
                        return (roleLabels[lang] && roleLabels[lang][role]) ? roleLabels[lang][role] : role;
                    }

                    btn.textContent = `Показать: ${getRoleLabel(role, langAttr)}`;
                    const panel = document.createElement('div');
                    panel.className = 'panel';

                    while (li.firstChild) {
                        panel.appendChild(li.firstChild);
                    }
                    li.appendChild(btn);
                    li.appendChild(panel);

                    btn.addEventListener('click', function () {
                        panel.classList.toggle('open');
                        btn.classList.toggle('active');
                        btn.textContent = panel.classList.contains('open')
                            ? `Скрыть: ${getRoleLabel(role, langAttr)}`
                            : `Показать: ${getRoleLabel(role, langAttr)}`;
                    });
                }
            });

            if (parseThinkTag(event.data).thinkContent) {
                message.appendChild(inThinkTag);
            }
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

// === Переопределение sendMessage (type=3, taskText + codeText + progLng + topic + preprompt) ===

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
    var taskInput = document.getElementById("taskText");
    var codeInput = document.getElementById("codeText");
    var progLng = document.querySelector("#selectProgLng").value;
    var topic = document.querySelector("#selectTheme").value;
    var preprompt = document.querySelector("#selectPrompt").value;

    if (!value) {
        alert("Сегодня нет доступных моделей. Повторите позже.");
        return;
    }

    if (!taskInput.value.trim() && !codeInput.value.trim()) {
        alert("Пожалуйста, введите условие задачи или код программы");
        return;
    }
    if (!progLng) {
        alert("Выберите язык программирования перед отправкой");
        return;
    }

    if (!confirmTopicWithoutPreprompt()) return;

    ws.send(JSON.stringify({
        type: '3',
        message: taskInput.value,
        value: value,
        language: language,
        progLng: progLng,
        topic: topic,
        code: codeInput.value,
        preprompt: preprompt
    }));

    setRequestLock(true);
    notEnter = true;
    // НЕ очищаем поля ввода в режиме «В чём ошибка» — условие задачи и код
    // должны сохраняться при смене модели ИИ (пользовательские жалобы).
    saveSharedText();
}

// === Переопределение simulateSend (type=3, taskText + codeText + progLng) ===

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
    var input = document.getElementById("taskText");
    var progLng = document.querySelector("#selectProgLng").value;
    var topic = document.querySelector("#selectTheme").value;
    var preprompt = document.querySelector("#selectPrompt").value;

    if (!input.value.trim()) {
        return;
    }
    if (!progLng) {
        updateVoiceStatus(getVoiceStatusText('select_prog_lang'));
        return;
    }

    if (!confirmTopicWithoutPreprompt()) return;

    ws.send(JSON.stringify({
        type: '3',
        message: input.value,
        value: value,
        language: language,
        progLng: progLng,
        topic: topic,
        preprompt: preprompt,
        code: document.getElementById("codeText").value
    }));

    setRequestLock(true);
    notEnter = true;
    updateVoiceStatus(getVoiceStatusText('messageSent'));
    // НЕ очищаем поля ввода в режиме «В чём ошибка» — условие задачи и код
    // должны сохраняться при смене модели ИИ (пользовательские жалобы).
    saveSharedText();
}

// === DOMContentLoaded: initProblemSelectors + input listeners + selectLang handler ===

document.addEventListener("DOMContentLoaded", async () => {
    // Селекторы языка программирования / темы / препромпта — общая логика в ai-common.js (DRY).
    var problemSelectors = initProblemSelectors();

    const taskText = document.getElementById('taskText');
    const codeText = document.getElementById('codeText');
    if (taskText) taskText.addEventListener('input', saveSharedText);
    if (codeText) codeText.addEventListener('input', saveSharedText);

    // === selectLang change handler (специфичный для find_error) ===
    document.getElementById("selectLang").addEventListener("change", async function () {
        const selectedLang = this.options[this.selectedIndex].getAttribute("language");
        const submitBtn = document.querySelector("button[type='submit']");
        if (submitBtn) submitBtn.textContent = localization[selectedLang].send;
        const clearBtn = document.querySelector("button[onclick='clearContext()']");
        if (clearBtn) clearBtn.textContent = localization[selectedLang].clear;
        const codeTextEl = document.getElementById("codeText");
        if (codeTextEl) codeTextEl.setAttribute("placeholder", localization[selectedLang].placeholder);
        const taskTextEl = document.getElementById("taskText");
        if (taskTextEl) taskTextEl.setAttribute("placeholder", getUiString('taskplace', 'Вставьте сюда условие задачи'));
        const sidebarHeader = document.querySelector(".sidebar-header");
        if (sidebarHeader) sidebarHeader.textContent = localization[selectedLang].adminPanel;
        const testPanelLink = document.getElementById("testPanelLink");
        if (testPanelLink) {
            testPanelLink.textContent = localization[selectedLang].testPanel;
        }
        const selectType1 = document.querySelector("#selectType option:nth-child(1)");
        if (selectType1) selectType1.textContent = localization[selectedLang].chat;
        const selectType2 = document.querySelector("#selectType option:nth-child(2)");
        if (selectType2) selectType2.textContent = localization[selectedLang].decideTask;
        const selectType3 = document.querySelector("#selectType option:nth-child(3)");
        if (selectType3) selectType3.textContent = localization[selectedLang].findError;
        const checkTextEl = document.querySelector(".check-text");
        if (checkTextEl) {
            checkTextEl.textContent = localization[selectedLang].enterHint;
        }
        const prepromptEl = document.querySelector(".preprompt");
        if (prepromptEl) {
            prepromptEl.textContent = localization[selectedLang].preprompt;
        }
        const taskLabel = document.querySelector(".task");
        if (taskLabel) taskLabel.textContent = getUiString('task', 'Задача:');
        const codeLabel = document.querySelector(".codetx");
        if (codeLabel) codeLabel.textContent = getUiString('codetx', 'Код программы:');
        updateAccordionLabels();

        const voiceModeBtn = document.getElementById("voiceModeBtn");
        if (voiceModeBtn) voiceModeBtn.textContent = localization[selectedLang].voiceMode;

        const voiceInputBtn = document.getElementById("voiceInputBtn");
        if (voiceInputBtn) voiceInputBtn.textContent = localization[selectedLang].voiceInput;

        const voiceOutputBtn = document.getElementById("voiceOutputBtn");
        if (voiceOutputBtn) voiceOutputBtn.textContent = localization[selectedLang].voiceOutput;

        const speakThinkLabel = document.getElementById("speakThinkLabel");
        if (speakThinkLabel) speakThinkLabel.textContent = localization[selectedLang].speakThinkLabel;

        // Перелокализация селекторов языка/темы/препромпта в новом UI-языке
        // (значения-иды языка-независимы; restorePageState пере-применяет выбор).
        if (problemSelectors) await problemSelectors.repopulateOnUiLanguageChange();
        saveInterfaceLanguage();
        updateVoiceStatus(getVoiceStatusText('readyForVoice'));
    });

    restoreSharedText();
});

// === window.onload: init для find_error ===

window.onload = function () {
    console.log('Initializing WebSocket with client_id:', client_id);
    restoreInterfaceLanguage();
    restoreSelections();
    initWebSocket();
    // MediaRecorder инициализируется при первом нажатии на кнопку записи
    document.getElementById("selectLang").dispatchEvent(new Event("change"));
    initAccordionForMessages();
    updateVoiceStatus(getVoiceStatusText('ready'));
    initSelectionPersistence();
    initModelLimitsWidget();

    // Инициализация чекбокса think-блоков
    const speakThinkCheckbox = document.getElementById('speakThinkContent');
    if (speakThinkCheckbox) {
        speakThinkCheckbox.addEventListener('change', function() {
            speakThinkEnabled = this.checked;
        });
    }
};