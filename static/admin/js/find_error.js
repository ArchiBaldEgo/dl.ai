/**
 * Фронтенд страницы «В чём ошибка?» — WebSocket-чат с AI для поиска ошибок в коде.
 *
 * Общие функции (cookies, CSRF, voice, accordion, markdown, localization, etc.)
 * берутся из ai-common.js. Этот файл содержит только переопределения:
 * - initWebSocket() — type=3 и accordion logic в onmessage
 * - sendMessage() — taskText, codeText, progLng, topic, preprompt, type=3
 * - simulateSend() — taskText, codeText, progLng, type=3
 * - selectLang change handler — специфичный для find_error
 * - DOMContentLoaded — fetchProblemData, populateLanguages, populateTopics, populatePrompts, etc.
 * - window.onload — init для find_error
 */

// === Специфичные для «В чём ошибка?» переменные ===
let problemData = null;
const PROBLEM_DATA_KEY = 'ai_problem_data_cache';
const PAGE_STATE_KEY = 'ai_page_state_problem';
let problemLanguageSelect = null;
let problemTopicSelect = null;
let problemPromptSelect = null;

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

// === DOMContentLoaded: загрузка языков, тем, промптов, восстановление состояния ===

document.addEventListener("DOMContentLoaded", async () => {
    problemLanguageSelect = document.getElementById("selectProgLng");
    problemTopicSelect = document.getElementById("selectTheme");
    problemPromptSelect = document.getElementById("selectPrompt");

    async function fetchProblemData() {
        if (problemData) return problemData;
        try {
            const cached = sessionStorage.getItem(PROBLEM_DATA_KEY);
            if (cached) {
                problemData = JSON.parse(cached);
                return problemData;
            }
        } catch (e) {}

        try {
            const uiLang = document.getElementById('selectLang').value || 'Русский';
            const url = new URL('/ai/api/problem-data/', window.location.origin);
            url.searchParams.set('ui_language', uiLang);
            const response = await fetch(url.toString());
            if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
            const data = await response.json();
            problemData = data;
            try {
                sessionStorage.setItem(PROBLEM_DATA_KEY, JSON.stringify(data));
            } catch (e) {}
            return data;
        } catch (error) {
            console.error('Error fetching problem data:', error);
            return { languages: [], topics: [], prompts: [], shared_prompts: [] };
        }
    }

    function setSelectEnabled(selectElement, enabled) {
        if (!selectElement) return;
        selectElement.disabled = !enabled;
    }

    function selectFirstIfSingle(selectElement) {
        if (selectElement && selectElement.options.length === 1) {
            selectElement.selectedIndex = 0;
        }
    }

    function populateLanguages(languages) {
        problemLanguageSelect.innerHTML = '<option value="">' + getUiString('select_prog_lang', 'Выберите язык программирования') + '</option>';
        if (languages && languages.length > 0) {
            languages.forEach(lang => {
                const option = new Option(lang.language_name, lang.id);
                problemLanguageSelect.appendChild(option);
            });
        }
        selectFirstIfSingle(problemLanguageSelect);
    }

    function populateTopics(languageId) {
        problemTopicSelect.innerHTML = '<option value="">' + getUiString('chooseTheme', 'Выберите тему') + '</option>';
        const topics = (problemData && problemData.topics) || [];
        const filteredTopics = topics.filter(topic => topic.programming_language == languageId);
        filteredTopics.forEach(topic => {
            const option = new Option(topic.name || topic.topic_name, topic.id);
            problemTopicSelect.appendChild(option);
        });
        selectFirstIfSingle(problemTopicSelect);
        setSelectEnabled(problemTopicSelect, languageId !== null && languageId !== undefined && problemTopicSelect.options.length > 1);
    }

    function filterPrompts(prompts, languageId, topicId) {
        const languageValue = languageId ? String(languageId) : "";
        const topicValue = topicId ? String(topicId) : "";

        return prompts.filter(prompt => {
            const hasTopic = prompt.topic_id !== null && prompt.topic_id !== undefined && prompt.topic_id !== "";
            if (!hasTopic) return true;
            if (topicValue) return String(prompt.topic_id) === topicValue;
            if (!languageValue) return false;
            return String(prompt.topic__programming_language) === languageValue;
        });
    }

    function populatePrompts(languageId, topicId) {
        problemPromptSelect.innerHTML = '<option value="">' + getUiString('choosePrompt', 'Выберите промпт') + '</option>';
        if (!problemData) return;
        let allPrompts = (problemData.prompts || []).slice();

        if (languageId) {
            const langIdStr = String(languageId);
            const shared = (problemData.shared_prompts || []).filter(sp => {
                const ids = sp.language_ids || [];
                return ids.length === 0 || ids.includes(languageId) || ids.includes(langIdStr);
            });
            shared.forEach(sp => {
                allPrompts.push({
                    id: `shared_${sp.id}`,
                    prompt_name: `[Общий] ${sp.name || sp.prompt_name}`,
                    name: `[Общий] ${sp.name || sp.prompt_name}`,
                    topic_id: null,
                    topic__programming_language: langIdStr
                });
            });
        }

        const filteredPrompts = filterPrompts(allPrompts, languageId, topicId);
        filteredPrompts.forEach(prompt => {
            const option = new Option(prompt.name || prompt.prompt_name, prompt.id);
            problemPromptSelect.appendChild(option);
        });
        selectFirstIfSingle(problemPromptSelect);
        const hasTopic = topicId !== null && topicId !== undefined && topicId !== "";
        setSelectEnabled(problemPromptSelect, hasTopic && problemPromptSelect.options.length > 1);
    }

    function savePageState() {
        try {
            const state = JSON.parse(localStorage.getItem(PAGE_STATE_KEY) || '{}');
            state.progLng = problemLanguageSelect.value;
            state.topic = problemTopicSelect.value;
            state.prompt = problemPromptSelect.value;
            localStorage.setItem(PAGE_STATE_KEY, JSON.stringify(state));
        } catch(e) {}
    }

    function restorePageState() {
        try {
            const state = JSON.parse(localStorage.getItem(PAGE_STATE_KEY) || '{}');
            if (!state.progLng) return;
            const langOpt = Array.from(problemLanguageSelect.options).find(o => o.value === state.progLng);
            if (!langOpt) return;

            const languageId = parseInt(state.progLng);
            problemLanguageSelect.value = state.progLng;
            populateTopics(languageId);
            populatePrompts(languageId, null);

            if (state.topic) {
                const topicOpt = Array.from(problemTopicSelect.options).find(o => o.value === state.topic);
                if (topicOpt) {
                    problemTopicSelect.value = state.topic;
                    const topicId = parseInt(state.topic);
                    populatePrompts(languageId, isNaN(topicId) ? null : topicId);
                    if (state.prompt) {
                        const promptOpt = Array.from(problemPromptSelect.options).find(o => o.value === state.prompt);
                        if (promptOpt) problemPromptSelect.value = state.prompt;
                    }
                }
            }
        } catch(e) {}
    }

    problemLanguageSelect.addEventListener("change", () => {
        const languageId = parseInt(problemLanguageSelect.value);
        problemTopicSelect.innerHTML = '<option value="">Выберите тему</option>';
        problemPromptSelect.innerHTML = '<option value="">Выберите промпт</option>';
        setSelectEnabled(problemTopicSelect, false);
        setSelectEnabled(problemPromptSelect, false);
        if (!isNaN(languageId)) {
            populateTopics(languageId);
            populatePrompts(languageId, null);
        } else {
            populatePrompts(null, null);
        }
        savePageState();
    });

    problemTopicSelect.addEventListener("change", () => {
        const topicId = parseInt(problemTopicSelect.value);
        problemPromptSelect.innerHTML = '<option value="">Выберите промпт</option>';
        const languageId = parseInt(problemLanguageSelect.value);
        setSelectEnabled(problemPromptSelect, false);
        populatePrompts(isNaN(languageId) ? null : languageId, isNaN(topicId) ? null : topicId);
        savePageState();
    });

    problemPromptSelect.addEventListener("change", savePageState);

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

        // Reload topics/prompts in the selected UI language
        sessionStorage.removeItem(PROBLEM_DATA_KEY);
        problemData = null;
        await fetchProblemData();
        const languageId = parseInt(problemLanguageSelect.value);
        populateLanguages(problemData.languages);
        if (!isNaN(languageId)) {
            populateTopics(languageId);
            populatePrompts(languageId, null);
        } else {
            populateTopics(null);
            populatePrompts(null, null);
        }

        saveInterfaceLanguage();
        updateVoiceStatus(getVoiceStatusText('readyForVoice'));
    });

    await fetchProblemData();
    populateLanguages(problemData.languages);
    setSelectEnabled(problemTopicSelect, false);
    setSelectEnabled(problemPromptSelect, false);
    populatePrompts(null, null);
    restorePageState();
    restoreSharedText();
});

// === window.onload: init для find_error ===

window.onload = function () {
    console.log('Initializing WebSocket with client_id:', client_id);
    restoreInterfaceLanguage();
    initWebSocket();
    // MediaRecorder инициализируется при первом нажатии на кнопку записи
    document.getElementById("selectLang").dispatchEvent(new Event("change"));
    initAccordionForMessages();
    updateVoiceStatus(getVoiceStatusText('ready'));

    // Инициализация чекбокса think-блоков
    const speakThinkCheckbox = document.getElementById('speakThinkContent');
    if (speakThinkCheckbox) {
        speakThinkCheckbox.addEventListener('change', function() {
            speakThinkEnabled = this.checked;
        });
    }
};