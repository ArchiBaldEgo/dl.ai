var ws = null;
var client_id = generateClientId();
var notEnter = false;
var requestInFlight = false;

var recognition = null;
var isListening = false;
var speechSynthesis = window.speechSynthesis;
var currentUtterance = null;
var speakThinkEnabled = true;

function generateClientId() {
    return 'client_' + Math.random().toString(36).substr(2, 9) + '_' + Date.now();
}

// Функция для показа/скрытия голосовых контролов
function toggleVoiceControls() {
    const voiceControls = document.getElementById('voiceControls');
    if (voiceControls.style.display === 'flex') {
        voiceControls.style.display = 'none';
        stopSpeech();
        if (isListening && recognition) {
            recognition.stop();
        }
    } else {
        voiceControls.style.display = 'flex';
    }
}

function initSpeechRecognition() {
    try {
        recognition = new (window.SpeechRecognition || window.webkitSpeechRecognition)();

        const langSelect = document.getElementById('selectLang');
        const selectedLang = langSelect.options[langSelect.selectedIndex].getAttribute('language');
        recognition.lang = getSpeechLanguage(selectedLang);

        recognition.continuous = false;
        recognition.interimResults = true;

        recognition.onstart = function () {
            isListening = true;
            updateVoiceUI();
            updateVoiceStatus(getVoiceStatusText('listening'));
        };

        recognition.onresult = function (event) {
            let finalTranscript = '';
            let interimTranscript = '';

            for (let i = event.resultIndex; i < event.results.length; i++) {
                const transcript = event.results[i][0].transcript;
                if (event.results[i].isFinal) {
                    finalTranscript += transcript;
                } else {
                    interimTranscript += transcript;
                }
            }

            if (finalTranscript) {
                document.getElementById('messageText').value = finalTranscript;
                updateVoiceStatus(getVoiceStatusText('recognized') + finalTranscript);
                setTimeout(() => {
                    if (document.getElementById('messageText').value.trim()) {
                        simulateSend();
                    }
                }, 500);
            } else if (interimTranscript) {
                updateVoiceStatus(getVoiceStatusText('recognizing') + interimTranscript);
            }
        };

        recognition.onerror = function (event) {
            updateVoiceStatus(getVoiceStatusText('error') + event.error);
            isListening = false;
            updateVoiceUI();
        };

        recognition.onend = function () {
            isListening = false;
            updateVoiceUI();
            updateVoiceStatus(getVoiceStatusText('readyForVoice'));
        };

    } catch (error) {
        updateVoiceStatus(getVoiceStatusText('notSupported'));
    }
}

function getSpeechLanguage(lang) {
    const languageMap = {
        'Russian': 'ru-RU',
        'English': 'en-US',
        'French': 'fr-FR'
    };
    return languageMap[lang] || 'en-US';
}

function getSpeechSynthesisLanguage(lang) {
    const languageMap = {
        'Russian': 'ru-RU',
        'English': 'en-US',
        'French': 'fr-FR'
    };
    return languageMap[lang] || 'en-US';
}

function toggleVoiceInput() {
    if (!recognition) {
        initSpeechRecognition();
    }

    if (isListening) {
        recognition.stop();
    } else {
        try {
            recognition.start();
        } catch (error) {
            updateVoiceStatus(getVoiceStatusText('startError'));
        }
    }
}

function speakLastResponse() {
    const messages = document.getElementById('messages');
    const assistantMessages = messages.querySelectorAll('.msg-assistant');

    if (assistantMessages.length === 0) {
        updateVoiceStatus(getVoiceStatusText('noResponse'));
        return;
    }

    const lastAssistantMessage = assistantMessages[assistantMessages.length - 1];
    const panel = lastAssistantMessage.querySelector('.panel');
    let text = '';

    if (panel) {
        text = panel.innerText || panel.textContent || '';
    } else {
        text = lastAssistantMessage.innerText || lastAssistantMessage.textContent || '';
    }

    if (!text.trim()) {
        updateVoiceStatus(getVoiceStatusText('textEmpty'));
        return;
    }

    speakText(text);
}

function speakText(text) {
    if (speechSynthesis.speaking) {
        speechSynthesis.cancel();
    }

    const cleanText = cleanSpeechText(text);

    if (!cleanText.trim()) {
        updateVoiceStatus(getVoiceStatusText('noText'));
        return;
    }

    const langSelect = document.getElementById('selectLang');
    const selectedLang = langSelect.options[langSelect.selectedIndex].getAttribute('language');

    currentUtterance = new SpeechSynthesisUtterance(cleanText);
    currentUtterance.lang = getSpeechSynthesisLanguage(selectedLang);
    currentUtterance.rate = 0.9;
    currentUtterance.pitch = 1;

    currentUtterance.onstart = function () {
        updateVoiceStatus(getVoiceStatusText('speaking'));
        document.getElementById('voiceOutputBtn').classList.add('speaking');
    };

    currentUtterance.onend = function () {
        updateVoiceStatus(getVoiceStatusText('speechEnd'));
        document.getElementById('voiceOutputBtn').classList.remove('speaking');
    };

    currentUtterance.onerror = function (event) {
        updateVoiceStatus(getVoiceStatusText('speechError'));
        document.getElementById('voiceOutputBtn').classList.remove('speaking');
    };

    speechSynthesis.speak(currentUtterance);
}

function cleanSpeechText(text) {
    if (!text) return '';

    let cleanText = text;

    // Убираем think-блоки только если пользователь этого не хочет
    if (!speakThinkEnabled) {
        cleanText = cleanText.replace(/<think>[\s\S]*?<\/think>/g, '');
    }

    cleanText = cleanText.replace(/Показать:.*?(Скрыть:|$)/g, '');
    cleanText = cleanText.replace(/Скрыть:.*?(Показать:|$)/g, '');

    cleanText = cleanText.replace(/<[^>]*>/g, '');

    const servicePatterns = [
        /\b(?:Ассистент|Assistant|Vous|Вы|User|Пользователь)\s*:\s*/gi,
        /\b(?:Скрыть|Показать|Hide|Show)\s*:\s*/gi,
        /\bЗапрос успешно обработан\b/gi,
        /\bОбрабатываю запрос пользователя\b/gi,
        /\bProcessing user request\b/gi,
        /\bRequest processed successfully\b/gi,
        /\bКонтекст очищен\b/gi,
        /\bContext cleared\b/gi,
        /\bСоединение установлено\b/gi,
        /\bConnection established\b/gi,
        /\bГотов к работе\b/gi,
        /\bReady to work\b/gi,
        /\bСообщение отправлено\b/gi,
        /\bMessage sent\b/gi,
        /\bПоказать:.*$/gm,
        /\bСкрыть:.*$/gm
    ];

    servicePatterns.forEach(pattern => {
        cleanText = cleanText.replace(pattern, '');
    });

    cleanText = cleanText.replace(/\s+/g, ' ').trim();

    return cleanText;
}

function stopSpeech() {
    if (speechSynthesis.speaking) {
        speechSynthesis.cancel();
        updateVoiceStatus(getVoiceStatusText('speechStopped'));
    }
    if (isListening && recognition) {
        recognition.stop();
    }
    document.getElementById('voiceOutputBtn').classList.remove('speaking');
}

function updateVoiceUI() {
    const voiceBtn = document.getElementById('voiceInputBtn');
    const voiceIndicator = document.getElementById('voiceIndicator');

    if (isListening) {
        voiceBtn.classList.add('recording');
        voiceIndicator.classList.add('active');
    } else {
        voiceBtn.classList.remove('recording');
        voiceIndicator.classList.remove('active');
    }
}

function updateVoiceStatus(message) {
    document.getElementById('voiceStatus').textContent = message;
}

function setRequestLock(isLocked) {
    requestInFlight = isLocked;
    const sendBtn = document.querySelector("button[type='submit']");
    if (sendBtn) {
        sendBtn.disabled = isLocked;
    }
}

function isTerminalAiMessage(payload) {
    const text = String(payload || "").toLowerCase();
    return text.includes("запрос успешно обработан")
        || text.includes("request processed successfully")
        || text.includes("ошибка при обработке запроса")
        || text.includes("что-то пошло не так")
        || text.includes("неверный формат json")
        || text.includes("контекст очищен")
        || text.includes("context cleared");
}

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

    if (!input.value.trim()) {
        return;
    }

    ws.send(JSON.stringify({
        type: '1',
        message: input.value,
        value: value,
        language: language,
    }));

    setRequestLock(true);
    notEnter = true;
    updateVoiceStatus(getVoiceStatusText('messageSent'));
    input.value = '';
}

function fetchCanUseAi(dlsid) {
    return Promise.resolve(null);
}

function parseThinkTag(inputText) {
    const thinkStartTag = '<think>';
    const thinkEndTag = '</think>';
    const startIdx = inputText.indexOf(thinkStartTag);
    const endIdx = inputText.indexOf(thinkEndTag);
    if (startIdx === -1 || endIdx === -1) {
        return {
            thinkContent: '',
            remainingText: inputText
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .trim()
        };
    }
    const thinkContent = inputText.substring(
        startIdx + thinkStartTag.length,
        endIdx
    );
    let remainingText =
        inputText.substring(0, startIdx) +
        inputText.substring(endIdx + thinkEndTag.length);
    remainingText = remainingText
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .trim();

    return {
        thinkContent: thinkContent.trim(),
        remainingText: remainingText
    };
}

function convertMarkdownToHTML(markdown) {
    markdown = markdown.replace(/</g, '&lt;').replace(/>/g, '&gt;');

    let codeBlocks = [];
    markdown = markdown.replace(/```([^\`]*)```/g, (match, code) => {
        const codeId = `%%CODEBLOCK${codeBlocks.length}%%`;
        codeBlocks.push(code);
        return codeId;
    });
    let inlineCodeBlocks = [];
    markdown = markdown.replace(/`([^`]+)`/g, (match, code) => {
        const codeId = `%%INLINECODE${inlineCodeBlocks.length}%%`;
        inlineCodeBlocks.push(code);
        return codeId;
    });
    markdown = markdown.replace(/^(#{1,6})\s*(.+)$/gm, (match, hashes, content) => {
        const level = hashes.length;
        return `<h${level}>${content}</h${level}>`;
    });
    markdown = markdown.replace(/\*\*([^\*]+)\*\*/g, '<strong>$1</strong>');
    markdown = markdown.replace(/\_\_([^\_]+)\_\_/g, '<strong>$1</strong>');
    markdown = markdown.replace(/\*([^\*]+)\*/g, '<em>$1</em>');
    markdown = markdown.replace(/\_([^\_]+)\_/g, '<em>$1</em>');
    markdown = markdown.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2">$1</a>');
    markdown = markdown.replace(/^\s*\*\s*(.+)$/gm, '<ul><li>$1</li></ul>');
    markdown = markdown.replace(/^\s*\d+\.\s*(.+)$/gm, '<ol><li>$1</li></ol>');
    markdown = markdown.replace(/!\[([^\]]+)\]\(([^)]+)\)/g, '<img src="$2" alt="$1" />');
    markdown = markdown.replace(/\n/g, '<br />');
    markdown = markdown.replace(/%%CODEBLOCK(\d+)%%/g, (match, index) => {
        return `<pre><code>${codeBlocks[index]}</code></pre>`;
    });
    markdown = markdown.replace(/%%INLINECODE(\d+)%%/g, (match, index) => {
        return `<code>${inlineCodeBlocks[index]}</code>`;
    });
    return markdown;
}

function initWebSocket() {
    try {
        var wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        var wsUrl = `${wsProtocol}//${window.location.host}/ai/chat/ws/${client_id}${window.location.search}`;

        ws = new WebSocket(wsUrl);

        ws.onopen = function (event) {
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
            var input = document.getElementById("messageText");
            input.value = '';

            if (isTerminalAiMessage(event.data)) {
                setRequestLock(false);
                notEnter = false;
            }

            initAccordionForMessages();
            collapseAllExceptLast();
        };

        ws.onerror = function (error) {
            updateVoiceStatus(getVoiceStatusText('wsError'));
            setRequestLock(false);
            notEnter = false;
        };

        ws.onclose = function (event) {
            updateVoiceStatus(getVoiceStatusText('connectionClosed'));
            setRequestLock(false);
            notEnter = false;
        };

    } catch (error) {
    }
}

function sendMessage(event) {
    event.preventDefault();
    if (!ws) {
        alert("Соединение не установлено. Пожалуйста, подождите...");
        return;
    }

    if (ws.readyState !== WebSocket.OPEN) {
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

    if (!value) {
        alert("Сегодня нет доступных моделей. Повторите позже.");
        return;
    }

    if (!input.value.trim()) {
        alert("Пожалуйста, введите сообщение");
        return;
    }

    ws.send(JSON.stringify({
        type: '1',
        message: input.value,
        value: value,
        language: language,
    }));
    setRequestLock(true);
    notEnter = true;
    input.value = '';
}

function clearContext() {
    if (!ws) {
        return;
    }

    if (ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ action: 'clear_context' }));

        var messages = document.getElementById('messages');
        messages.innerHTML = '';

        var clearMessage = document.createElement('li');
        clearMessage.innerHTML = '<div style="color: green;">Контекст очищен</div>';
        messages.appendChild(clearMessage);
        messages.scrollTo({ top: messages.scrollHeight, behavior: 'smooth' });

    } else {
        alert("Соединение не установлено");
    }
}

document.addEventListener("keydown", function (event) {
    const checkbox = document.querySelector(".inp");
    if (event.key === "Enter" && (!checkbox || checkbox.checked) && !event.shiftKey && !notEnter) {
        sendMessage(event);
    }
});

const toggleButton = document.querySelector('.toggle-button');
const sidebar = document.querySelector('.sidebar');

if (toggleButton && sidebar) {
    toggleButton.addEventListener('click', () => {
        sidebar.classList.toggle('open');
    });
}


let isResizing = false;

function startResize(event) {
    isResizing = true;
    window.addEventListener('mousemove', resize);
    window.addEventListener('mouseup', stopResize);
}

function resize(event) {
    if (isResizing) {
        const container = document.querySelector('form');
        const newWidth = container.getBoundingClientRect().right - event.clientX;
        const minWidth = 500;
        if (newWidth > minWidth) {
            container.style.width = `${newWidth}px`;
        }
    }
}

const localization = {
    Russian: {
        send: "Отправить",
        clear: "Очистить контекст",
        placeholder: "Задайте вопрос (желательно на английском во избежание ошибок), для красивого форматирования оберните код в ```(буква Ё на клавиатуре)\nПример форматирования кода:\n```\nprint('Hello, world!')\n```",
        adminPanel: "Админ-Панель",
        testPanel: "Тест-панель",
        chat: "Чат с DLAI",
        decideTask: "Реши задачу",
        findError: "В чём ошибка?",
        enterHint: "При нажатии на Enter будет отправляться вопрос (для переноса строки Enter+Shift)",
        preprompt: "Препромпт",
        chooseLanguage: "Выберите язык",
        chooseTheme: "Выберите тему",
        voiceMode: "Голосовой режим",
        voiceInput: "Голосовой ввод",
        voiceOutput: "Озвучить ответ",
        voiceStop: "Стоп",
        speakThinkLabel: "Озвучивать think-блоки",
        voiceStatus: {
            listening: "Слушаю... Говорите сейчас",
            recognized: "Распознано: ",
            recognizing: "Распознаю: ",
            error: "Ошибка: ",
            readyForVoice: "Готов к голосовому вводу",
            notSupported: "Голосовой ввод не поддерживается вашим браузером",
            startError: "Ошибка запуска распознавания",
            noResponse: "Нет ответов для озвучивания",
            textEmpty: "Текст для озвучивания пуст",
            noText: "Нет текста для озвучивания",
            speaking: "Озвучиваю...",
            speechEnd: "Озвучивание завершено",
            speechError: "Ошибка озвучивания",
            speechStopped: "Озвучивание остановлено",
            connectionError: "Ошибка: соединение не установлено",
            waitForModel: "Дождитесь ответа модели перед новым запросом",
            messageSent: "Сообщение отправлено",
            connectionEstablished: "Соединение установлено.",
            connectionClosed: "Соединение закрыто",
            wsError: "Ошибка соединения",
            ready: 'Готов к работе. Нажмите "Голосовой режим" для активации голосовых функций.'
        }
    },
    
    English: {
        send: "Send",
        clear: "Clear Context",
        placeholder: "Ask a question (preferably in English to avoid errors), for nice formatting wrap the code in ```\nExample of code formatting:\n```\nprint('Hello, world!')\n```",
        adminPanel: "Admin Panel",
        testPanel: "Test Panel",
        chat: "Chat with DLAI",
        decideTask: "Solve the task",
        findError: "What's the error?",
        enterHint: "Press Enter to send the question (Shift+Enter for a new line)",
        preprompt: "Preprompt",
        chooseLanguage: "Choose language",
        chooseTheme: "Choose theme",
        voiceMode: "Voice mode",
        voiceInput: "Voice input",
        voiceOutput: "Voice answer",
        voiceStop: "Stop",
        speakThinkLabel: "Voice think blocks",
        voiceStatus: {
            listening: "Listening... Speak now",
            recognized: "Recognized: ",
            recognizing: "Recognizing: ",
            error: "Error: ",
            readyForVoice: "Ready for voice input",
            notSupported: "Voice input not supported in your browser",
            startError: "Error starting recognition",
            noResponse: "No responses to speak",
            textEmpty: "Text to speak is empty",
            noText: "No text to speak",
            speaking: "Speaking...",
            speechEnd: "Speaking finished",
            speechError: "Speech error",
            speechStopped: "Speech stopped",
            connectionError: "Error: connection not established",
            waitForModel: "Wait for model response before new request",
            messageSent: "Message sent",
            connectionEstablished: "Connection established.",
            connectionClosed: "Connection closed",
            wsError: "Connection error",
            ready: 'Ready. Click "Voice mode" to activate voice features.'
        }
    },
    French: {
        send: "Envoyer",
        clear: "Effacer le contexte",
        placeholder: "Posez une question (de préférence en anglais pour éviter les erreurs), pour un bon formatage, encadrez le code dans ```\nExemple de formatage du code:\n```\nprint('Hello, world!')\n```",
        adminPanel: "Panneau Admin",
        testPanel: "Panneau Test",
        chat: "Chat avec DLAI",
        decideTask: "Résoudre la tâche",
        findError: "Quelle est l'erreur?",
        enterHint: "Appuyez sur Entrée pour envoyer la question (Shift+Enter pour une nouvelle ligne)",
        preprompt: "Pré-promp",
        chooseLanguage: "Choisir la langue",
        chooseTheme: "Choisir le thème",
        voiceMode: "Mode vocal",
        voiceInput: "Saisie vocale",
        voiceOutput: "Lire la réponse",
        voiceStop: "Arrêter",
        speakThinkLabel: "Lire les blocs think",
        voiceStatus: {
            listening: "Écoute... Parlez maintenant",
            recognized: "Reconnu : ",
            recognizing: "Reconnaissance : ",
            error: "Erreur : ",
            readyForVoice: "Prêt pour la saisie vocale",
            notSupported: "Saisie vocale non supportée par votre navigateur",
            startError: "Erreur de démarrage de la reconnaissance",
            noResponse: "Aucune réponse à lire",
            textEmpty: "Texte à lire vide",
            noText: "Pas de texte à lire",
            speaking: "Lecture...",
            speechEnd: "Lecture terminée",
            speechError: "Erreur de lecture",
            speechStopped: "Lecture arrêtée",
            connectionError: "Erreur : connexion non établie",
            waitForModel: "Attendez la réponse du modèle avant une nouvelle requête",
            messageSent: "Message envoyé",
            connectionEstablished: "Connexion établie.",
            connectionClosed: "Connexion fermée",
            wsError: "Erreur de connexion",
            ready: 'Prêt. Cliquez sur "Mode vocal" pour activer les fonctions vocales.'
        }
    }
};

function getVoiceStatusText(key, param = '') {
    const selectLang = document.getElementById('selectLang');
    const lang = selectLang.options[selectLang.selectedIndex].getAttribute('language');
    const msg = localization[lang]?.voiceStatus?.[key];
    return (msg || localization.Russian.voiceStatus[key] || key) + param;
}

document.getElementById("selectLang").addEventListener("change", function () {
    const selectedLang = this.options[this.selectedIndex].getAttribute("language");
    document.querySelector("button[type='submit']").textContent = localization[selectedLang].send;
    document.querySelector("button[onclick='clearContext()']").textContent = localization[selectedLang].clear;
    document.getElementById("messageText").setAttribute("placeholder", localization[selectedLang].placeholder);
    document.querySelector(".sidebar-header").textContent = localization[selectedLang].adminPanel;
    const testPanelLink = document.getElementById("testPanelLink");
    if (testPanelLink) {
        testPanelLink.textContent = localization[selectedLang].testPanel;
    }
    document.querySelector("#selectType option:nth-child(1)").textContent = localization[selectedLang].chat;
    document.querySelector("#selectType option:nth-child(2)").textContent = localization[selectedLang].decideTask;
    document.querySelector("#selectType option:nth-child(3)").textContent = localization[selectedLang].findError;
    const checkTextEl = document.querySelector(".check-text");
    if (checkTextEl) {
        checkTextEl.textContent = localization[selectedLang].enterHint;
    }
    const prepromptEl = document.querySelector(".preprompt");
    if (prepromptEl) {
        prepromptEl.textContent = localization[selectedLang].preprompt;
    }
    const voiceModeBtn = document.getElementById("voiceModeBtn");
    if (voiceModeBtn) voiceModeBtn.textContent = localization[selectedLang].voiceMode;

    const voiceInputBtn = document.getElementById("voiceInputBtn");
    if (voiceInputBtn) voiceInputBtn.textContent = localization[selectedLang].voiceInput;

    const voiceOutputBtn = document.getElementById("voiceOutputBtn");
    if (voiceOutputBtn) voiceOutputBtn.textContent = localization[selectedLang].voiceOutput;

    const voiceStopBtn = document.getElementById("voiceStopBtn");
    if (voiceStopBtn) voiceStopBtn.textContent = localization[selectedLang].voiceStop;

    const speakThinkLabel = document.getElementById("speakThinkLabel");
    if (speakThinkLabel) speakThinkLabel.textContent = localization[selectedLang].speakThinkLabel;

    updateAccordionLabels();

    updateVoiceStatus(getVoiceStatusText('readyForVoice'));

    if (recognition) {
        recognition.lang = getSpeechLanguage(selectedLang);
    }
});

function stopResize() {
    isResizing = false;
    window.removeEventListener('mousemove', resize);
    window.removeEventListener('mouseup', stopResize);
}


// Остальные функции остаются без изменений
function initAccordionForMessages() {
    const messages = document.getElementById('messages');
    const allMessages = messages.querySelectorAll(':scope > li');
    const roles = [];
    for (let i = 0; i < allMessages.length; i++) {
        if (i % 2 === 0) roles.push('user');
        else roles.push('assistant');
    }

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

    allMessages.forEach(function (li, idx) {
        if (!li.classList.contains('accordion-li')) {
            li.classList.add('accordion-li');
            const role = roles[idx] || 'other';
            li.classList.remove('msg-user', 'msg-assistant');

            const btn = document.createElement('button');
            if (role === 'user') li.classList.add('msg-user');
            if (role === 'assistant') li.classList.add('msg-assistant');
            btn.className = 'accordion';
            if (role === 'user') btn.classList.add('accordion-user');
            if (role === 'assistant') btn.classList.add('accordion-assistant');
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

    if (allMessages.length > 0) {
        const lastLi = allMessages[allMessages.length - 1];
        const lastBtn = lastLi.querySelector('.accordion');
        const lastPanel = lastLi.querySelector('.panel');
        if (lastBtn && lastPanel) {
            lastPanel.classList.add('open');
            lastBtn.classList.add('active');
            const lastRole = roles[allMessages.length - 1] || 'other';
            lastBtn.textContent = `Скрыть: ${getRoleLabel(lastRole, langAttr)}`;
        }
    }

    window._accordionRoles = roles;
}

function updateAccordionLabels() {
    const selectLang = document.getElementById('selectLang');
    const langAttr = selectLang.options[selectLang.selectedIndex].getAttribute('language');
    const roleLabels = {
        Russian: { user: 'Вы', assistant: 'Ассистент', other: 'Други' },
        English: { user: 'You', assistant: 'Assistant', other: 'Others' },
        French: { user: 'Vous', assistant: 'Assistant', other: 'Autres' }
    };
    const roleLabelsShort = roleLabels[langAttr] || roleLabels.Russian;
    const allMessages = document.querySelectorAll('#messages li');
    const roles = window._accordionRoles || [];

    allMessages.forEach((li, idx) => {
        const btn = li.querySelector('.accordion');
        const panel = li.querySelector('.panel');
        if (btn && panel) {
            const role = roles[idx] || 'other';
            const isOpen = panel.classList.contains('open');
            const label = roleLabelsShort[role] || role;
            btn.textContent = isOpen ? `Скрыть: ${label}` : `Показать: ${label}`;
        }
    });
}

function collapseAllExceptLast() {
    const allMessages = document.getElementById('messages').querySelectorAll('li');
    const selectLang = document.getElementById('selectLang');
    const langAttr = selectLang.options[selectLang.selectedIndex].getAttribute('language');
    const roleLabels = {
        Russian: { user: 'Вы', assistant: 'Ассистент', other: 'Други' },
        English: { user: 'You', assistant: 'Assistant', other: 'Others' },
        French: { user: 'Vous', assistant: 'Assistant', other: 'Autres' }
    };

    const roles = window._accordionRoles || [];
    function getRoleLabel(role, lang) {
        return (roleLabels[lang] && roleLabels[lang][role]) ? roleLabels[lang][role] : role;
    }

    allMessages.forEach((li, idx) => {
        const btn = li.querySelector('.accordion');
        const panel = li.querySelector('.panel');
        const role = roles[idx] || 'other';
        if (btn && panel) {
            if (idx === allMessages.length - 1) {
                panel.classList.add('open');
                btn.classList.add('active');
                btn.textContent = `Скрыть: ${getRoleLabel(role, langAttr)}`;
            } else {
                panel.classList.remove('open');
                btn.classList.remove('active');
                btn.textContent = `Показать: ${getRoleLabel(role, langAttr)}`;
            }
        }
    });
}

window.onload = function () {
    console.log('Initializing WebSocket with client_id:', client_id);
    initWebSocket();
    initSpeechRecognition();
    document.getElementById("selectLang").dispatchEvent(new Event("change"));
    initAccordionForMessages();
    updateVoiceStatus(getVoiceStatusText('ready'));

    // Инициализация чекбокса think-блоков
    const speakThinkCheckbox = document.getElementById('speakThinkContent');
    speakThinkCheckbox.addEventListener('change', function () {
        speakThinkEnabled = this.checked;
    });
};