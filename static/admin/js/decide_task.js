/**
 * Фронтенд страницы «Реши задачу» — WebSocket-чат с AI для решения задач dl.gsu.by.
 *
 * Общие функции (cookies, CSRF, voice, accordion, markdown, localization, resize,
 * sidebar, Enter handler, clearContext, initWebSocket default) берутся из ai-common.js,
 * который подключается ПЕРЕД этим файлом.
 *
 * Здесь только специфичная для «Реши задачу» логика:
 * - Данные задачи: языки, темы, промпты (problemData).
 * - override initWebSocket() — type=2 и accordion logic в onmessage.
 * - override sendMessage() / simulateSend() — progLng, topic, preprompt, nodeId.
 * - selectLang change handler — обновление UI элементов, reload problem data.
 * - DOMContentLoaded — загрузка данных задачи, заполнение селектов, restorePageState.
 * - window.onload — init для decide_task.
 */

// === Специфичные для «Реши задачу» переменные ===
let problemData = null;
const PROBLEM_DATA_KEY = 'ai_problem_data_cache';
const PAGE_STATE_KEY = 'ai_page_state_problem';
let problemLanguageSelect = null;
let problemTopicSelect = null;
let problemPromptSelect = null;

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

// === override sendMessage — с progLng, topic, preprompt, nodeId ===
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
    var progLng = document.querySelector("#selectProgLng").value;
    var topic = document.querySelector("#selectTheme").value;
    var preprompt = document.querySelector("#selectPrompt").value;

    if (!value) {
        alert("Сегодня нет доступных моделей. Повторите позже.");
        return;
    }

    if (!input.value.trim()) {
        alert("Пожалуйста, введите сообщение");
        return;
    }
    if (!progLng) {
        alert("Выберите язык программирования перед отправкой");
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

// === override simulateSend — с progLng, topic, preprompt, nodeId ===
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

            // Reload topics/prompts in the selected UI language
            sessionStorage.removeItem(PROBLEM_DATA_KEY);
            problemData = null;
            await fetchProblemData();
            var languageId = parseInt(problemLanguageSelect.value);
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
    }
});

// === DOMContentLoaded — загрузка данных задачи, заполнение селектов ===
document.addEventListener("DOMContentLoaded", async () => {
    problemLanguageSelect = document.getElementById("selectProgLng");
    problemTopicSelect = document.getElementById("selectTheme");
    problemPromptSelect = document.getElementById("selectPrompt");

    async function fetchProblemData() {
        if (problemData) return problemData;
        try {
            var cached = sessionStorage.getItem(PROBLEM_DATA_KEY);
            if (cached) {
                problemData = JSON.parse(cached);
                return problemData;
            }
        } catch (e) {}

        try {
            var uiLang = document.getElementById('selectLang').value || 'Русский';
            var url = new URL('/ai/api/problem-data/', window.location.origin);
            url.searchParams.set('ui_language', uiLang);
            var response = await fetch(url.toString());
            if (!response.ok) throw new Error('HTTP error! status: ' + response.status);
            var data = await response.json();
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
            languages.forEach(function(lang) {
                var option = new Option(lang.language_name, lang.id);
                problemLanguageSelect.appendChild(option);
            });
        }
        selectFirstIfSingle(problemLanguageSelect);
    }

    function populateTopics(languageId) {
        problemTopicSelect.innerHTML = '<option value="">' + getUiString('chooseTheme', 'Выберите тему') + '</option>';
        var topics = (problemData && problemData.topics) || [];
        var filteredTopics = topics.filter(function(topic) { return topic.programming_language == languageId; });
        filteredTopics.forEach(function(topic) {
            var option = new Option(topic.name || topic.topic_name, topic.id);
            problemTopicSelect.appendChild(option);
        });
        selectFirstIfSingle(problemTopicSelect);
        setSelectEnabled(problemTopicSelect, languageId !== null && languageId !== undefined && problemTopicSelect.options.length > 1);
    }

    function filterPrompts(prompts, languageId, topicId) {
        var languageValue = languageId ? String(languageId) : "";
        var topicValue = topicId ? String(topicId) : "";
        return prompts.filter(function(prompt) {
            var hasTopic = prompt.topic_id !== null && prompt.topic_id !== undefined && prompt.topic_id !== "";
            if (!hasTopic) return true;
            if (topicValue) return String(prompt.topic_id) === topicValue;
            if (!languageValue) return false;
            return String(prompt.topic__programming_language) === languageValue;
        });
    }

    function populatePrompts(languageId, topicId) {
        problemPromptSelect.innerHTML = '<option value="">' + getUiString('choosePrompt', 'Выберите промпт') + '</option>';
        if (!problemData) return;
        var allPrompts = (problemData.prompts || []).slice();

        if (languageId) {
            var langIdStr = String(languageId);
            var shared = (problemData.shared_prompts || []).filter(function(sp) {
                var ids = sp.language_ids || [];
                return ids.length === 0 || ids.includes(languageId) || ids.includes(langIdStr);
            });
            shared.forEach(function(sp) {
                allPrompts.push({
                    id: 'shared_' + sp.id,
                    prompt_name: '[Общий] ' + (sp.name || sp.prompt_name),
                    name: '[Общий] ' + (sp.name || sp.prompt_name),
                    topic_id: null,
                    topic__programming_language: langIdStr
                });
            });
        }

        var filteredPrompts = filterPrompts(allPrompts, languageId, topicId);
        filteredPrompts.forEach(function(prompt) {
            var option = new Option(prompt.name || prompt.prompt_name, prompt.id);
            problemPromptSelect.appendChild(option);
        });
        selectFirstIfSingle(problemPromptSelect);
        var hasTopic = topicId !== null && topicId !== undefined && topicId !== "";
        setSelectEnabled(problemPromptSelect, hasTopic && problemPromptSelect.options.length > 1);
    }

    function savePageState() {
        try {
            var state = JSON.parse(localStorage.getItem(PAGE_STATE_KEY) || '{}');
            state.progLng = problemLanguageSelect.value;
            state.topic = problemTopicSelect.value;
            state.prompt = problemPromptSelect.value;
            localStorage.setItem(PAGE_STATE_KEY, JSON.stringify(state));
        } catch(e) {}
    }

    function restorePageState() {
        try {
            var state = JSON.parse(localStorage.getItem(PAGE_STATE_KEY) || '{}');
            if (!state.progLng) return;
            var langOpt = Array.from(problemLanguageSelect.options).find(function(o) { return o.value === state.progLng; });
            if (!langOpt) return;

            var languageId = parseInt(state.progLng);
            problemLanguageSelect.value = state.progLng;
            populateTopics(languageId);
            populatePrompts(languageId, null);

            if (state.topic) {
                var topicOpt = Array.from(problemTopicSelect.options).find(function(o) { return o.value === state.topic; });
                if (topicOpt) {
                    problemTopicSelect.value = state.topic;
                    var topicId = parseInt(state.topic);
                    populatePrompts(languageId, isNaN(topicId) ? null : topicId);
                    if (state.prompt) {
                        var promptOpt = Array.from(problemPromptSelect.options).find(function(o) { return o.value === state.prompt; });
                        if (promptOpt) problemPromptSelect.value = state.prompt;
                    }
                }
            }
        } catch(e) {}
    }

    problemLanguageSelect.addEventListener("change", function() {
        var languageId = parseInt(problemLanguageSelect.value);
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

    problemTopicSelect.addEventListener("change", function() {
        var topicId = parseInt(problemTopicSelect.value);
        problemPromptSelect.innerHTML = '<option value="">Выберите промпт</option>';
        var languageId = parseInt(problemLanguageSelect.value);
        setSelectEnabled(problemPromptSelect, false);
        populatePrompts(isNaN(languageId) ? null : languageId, isNaN(topicId) ? null : topicId);
        savePageState();
    });

    problemPromptSelect.addEventListener("change", savePageState);

    var messageText = document.getElementById('messageText');
    if (messageText) messageText.addEventListener('input', saveSharedText);

    function decodeCompilerName(encoded) {
        if (!encoded) return '';
        try {
            return atob(encoded).trim().toLowerCase();
        } catch (e) {
            return '';
        }
    }

    function findLanguageIdByName(languages, compilerName) {
        if (!compilerName || !languages) return null;
        return languages.find(function(lang) {
            return lang.language_name && lang.language_name.toLowerCase().includes(compilerName);
        })?.id || null;
    }

    async function loadTaskFromUrl() {
        var messageTextEl = document.getElementById('messageText');
        var nodeId = window.AI_TASK_NODE_ID || (messageTextEl && messageTextEl.dataset.nodeId) || '';
        var compilerNameEncoded = window.AI_COMPILER_NAME || (messageTextEl && messageTextEl.dataset.compilerName) || '';
        console.log('loadTaskFromUrl: nodeId=', nodeId, 'compiler_b64=', compilerNameEncoded);
        if (!nodeId) return false;

        try {
            var url = new URL('/ai/api/task-info/', window.location.origin);
            url.searchParams.set('nodeId', nodeId);
            url.searchParams.set('removeHtmlTags', 'true');
            console.log('Fetching task info:', url.toString());
            var response = await fetch(url.toString());
            console.log('Task info response status:', response.status);
            if (response.status === 404) {
                updateVoiceStatus(getUiString('taskNotFound', 'Задача не найдена'));
                return false;
            }
            if (!response.ok) {
                var errorText = await response.text();
                throw new Error('HTTP ' + response.status + ': ' + errorText);
            }
            var data = await response.json();
            console.log('Task info data:', data);
            var statement = data.statement || data.currentStatement || '';
            if (messageTextEl) {
                messageTextEl.value = statement;
                saveSharedText();
            }

            var compilerName = decodeCompilerName(compilerNameEncoded);
            var matchedLanguageId = findLanguageIdByName(problemData?.languages, compilerName);
            if (matchedLanguageId) {
                problemLanguageSelect.value = matchedLanguageId;
                populateTopics(matchedLanguageId);
                populatePrompts(matchedLanguageId, null);
                savePageState();
            }
            return true;
        } catch (error) {
            console.error('Error loading task statement:', error);
            updateVoiceStatus(getUiString('taskLoadError', 'Не удалось загрузить условие задачи'));
            return false;
        }
    }

    await fetchProblemData();
    populateLanguages(problemData.languages);
    setSelectEnabled(problemTopicSelect, false);
    setSelectEnabled(problemPromptSelect, false);
    populatePrompts(null, null);
    restorePageState();
    var taskLoaded = await loadTaskFromUrl();
    if (!taskLoaded) {
        restoreSharedText();
    }
});

// === window.onload — init для decide_task ===
window.onload = function () {
    console.log('Initializing WebSocket with client_id:', client_id);
    restoreInterfaceLanguage();
    initWebSocket();
    document.getElementById("selectLang").dispatchEvent(new Event("change"));
    initAccordionForMessages();
    updateVoiceStatus(getVoiceStatusText('ready'));

    var speakThinkCheckbox = document.getElementById('speakThinkContent');
    if (speakThinkCheckbox) {
        speakThinkCheckbox.addEventListener('change', function() {
            speakThinkEnabled = this.checked;
        });
    }
};