/**
 * ai-common.js — общие хелперы для всех AI-страниц (chat, decide_task, find_error).
 *
 * Подключается ПЕРЕД страничным JS через <script defer>.
 * Содержит: cookies, CSRF, WebSocket init, voice (MediaRecorder + SpeechSynthesis),
 * accordion, markdown, think-tag parsing, localization, persistence.
 *
 * Страничные файлы (chat_template.js, decide_task.js, find_error.js) переопределяют
 * sendMessage, simulateSend, initWebSocket своей реализацией.
 */

// === Глобальные переменные (общие для всех страниц) ===
var ws = null;
var client_id = resolveClientId();
var notEnter = false;
var requestInFlight = false;

// Голосовые переменные для MediaRecorder
var mediaRecorder = null;
var audioChunks = [];
var isListening = false;
var audioStream = null;
var speechSynthesis = window.speechSynthesis;
var currentUtterance = null;
var speakThinkEnabled = true;

// Таймер для автоматической остановки записи
var recordingTimeout = null;
var MAX_RECORDING_TIME = 30000;

// Persistence keys
var INTERFACE_LANG_KEY = 'ai_interface_language';
var SHARED_TEXT_KEY = 'ai_text_shared';
var SELECTIONS_KEY = 'ai_user_selections';

// Селекты, которые сохраняем и восстанавливаем
var PERSISTED_SELECT_IDS = [
    'select',        // модель
    'selectLang',    // язык интерфейса
    'selectType',    // тип страницы (чат/решить/ошибка)
    'selectProgLng', // язык программирования
    'selectTheme',   // тема
    'selectPrompt',  // препромпт
];

// Сохранение всех селектов в localStorage
function saveSelections() {
    try {
        var saved = JSON.parse(localStorage.getItem(SELECTIONS_KEY) || '{}');
        for (var i = 0; i < PERSISTED_SELECT_IDS.length; i++) {
            var el = document.getElementById(PERSISTED_SELECT_IDS[i]);
            if (el && el.selectedIndex >= 0) {
                saved[PERSISTED_SELECT_IDS[i]] = el.selectedIndex;
            }
        }
        // Голосовой режим
        var voiceBtn = document.getElementById('voiceModeBtn');
        if (voiceBtn) {
            var voiceControls = document.getElementById('voiceControls');
            saved._voiceMode = voiceControls && voiceControls.style.display !== 'none' ? 1 : 0;
        }
        // Node ID задачи (если есть data-атрибут)
        var msgText = document.getElementById('messageText');
        if (msgText && msgText.dataset.nodeId) {
            saved._nodeId = msgText.dataset.nodeId;
        }
        localStorage.setItem(SELECTIONS_KEY, JSON.stringify(saved));
    } catch (e) {}
}

// Восстановление всех селектов из localStorage
function restoreSelections() {
    try {
        var saved = JSON.parse(localStorage.getItem(SELECTIONS_KEY) || '{}');
        for (var i = 0; i < PERSISTED_SELECT_IDS.length; i++) {
            var el = document.getElementById(PERSISTED_SELECT_IDS[i]);
            if (el && saved[PERSISTED_SELECT_IDS[i]] !== undefined) {
                var idx = saved[PERSISTED_SELECT_IDS[i]];
                if (idx >= 0 && idx < el.options.length) {
                    el.selectedIndex = idx;
                }
            }
        }
        // Голосовой режим
        if (saved._voiceMode === 1) {
            var voiceControls = document.getElementById('voiceControls');
            if (voiceControls && voiceControls.style.display === 'none') {
                toggleVoiceControls();
            }
        }
    } catch (e) {}
}

// Сохранение при изменении любого селекта
function initSelectionPersistence() {
    for (var i = 0; i < PERSISTED_SELECT_IDS.length; i++) {
        var el = document.getElementById(PERSISTED_SELECT_IDS[i]);
        if (el) {
            el.addEventListener('change', saveSelections);
        }
    }
    // Сохраняем голосовой режим при переключении
    var voiceBtn = document.getElementById('voiceModeBtn');
    if (voiceBtn) {
        voiceBtn.addEventListener('click', function() {
            setTimeout(saveSelections, 100);
        });
    }
}

// === Cookie / CSRF / Session helpers ===

function getCookieValue(name) {
    const match = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
    return match ? decodeURIComponent(match[1]) : '';
}

function getMetaContent(name) {
    const meta = document.querySelector('meta[name="' + name + '"]');
    return meta ? meta.getAttribute('content') : null;
}

function getCsrfToken() {
    var cookies = document.cookie.split(';');
    for (var i = 0; i < cookies.length; i++) {
        var parts = cookies[i].trim().split('=');
        if (parts[0] === 'csrftoken') return parts[1];
    }
    return '';
}

function resolveClientId() {
    var sessionIdFromMeta = getMetaContent('external-session-id');
    var sessionId = sessionIdFromMeta || getCookieValue('DLSID');
    return sessionId ? 'dlsid_' + encodeURIComponent(sessionId) : 'dlsid_missing';
}

// === HTML / Markdown helpers ===

function escapeHtml(unsafe) {
    return unsafe
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}

function parseThinkTag(inputText) {
    var thinkStartTag = '<think>';
    var thinkEndTag = '</think>';
    var startIdx = inputText.indexOf(thinkStartTag);
    var endIdx = inputText.indexOf(thinkEndTag);
    if (startIdx === -1 || endIdx === -1) {
        return {
            thinkContent: '',
            remainingText: escapeHtml(inputText).trim()
        };
    }
    var thinkContent = inputText.substring(
        startIdx + thinkStartTag.length,
        endIdx
    );
    var remainingText =
        inputText.substring(0, startIdx) +
        inputText.substring(endIdx + thinkEndTag.length);

    return {
        thinkContent: escapeHtml(thinkContent).trim(),
        remainingText: escapeHtml(remainingText).trim()
    };
}

function convertMarkdownToHTML(markdown) {
    markdown = markdown.replace(/</g, '&lt;').replace(/>/g, '&gt;');

    var codeBlocks = [];
    markdown = markdown.replace(/```([^\`]*)```/g, function(match, code) {
        var codeId = '%%CODEBLOCK' + codeBlocks.length + '%%';
        codeBlocks.push(code);
        return codeId;
    });
    var inlineCodeBlocks = [];
    markdown = markdown.replace(/`([^`]+)`/g, function(match, code) {
        var codeId = '%%INLINECODE' + inlineCodeBlocks.length + '%%';
        inlineCodeBlocks.push(code);
        return codeId;
    });
    markdown = markdown.replace(/^(#{1,6})\s*(.+)$/gm, function(match, hashes, content) {
        var level = hashes.length;
        return '<h' + level + '>' + content + '</h' + level + '>';
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
    markdown = markdown.replace(/%%CODEBLOCK(\d+)%%/g, function(match, index) {
        return '<pre><code>' + codeBlocks[index] + '</code></pre>';
    });
    markdown = markdown.replace(/%%INLINECODE(\d+)%%/g, function(match, index) {
        return '<code>' + inlineCodeBlocks[index] + '</code>';
    });
    return markdown;
}

// === Terminal message detection ===

function isTerminalAiMessage(payload) {
    var text = String(payload || "").toLowerCase();
    return text.includes("запрос успешно обработан")
        || text.includes("request processed successfully")
        || text.includes("ошибка при обработке запроса")
        || text.includes("что-то пошло не так")
        || text.includes("неверный формат json")
        || text.includes("контекст очищен")
        || text.includes("context cleared");
}

// === Request lock ===

function setRequestLock(isLocked) {
    requestInFlight = isLocked;
    var sendBtn = document.querySelector("button[type='submit']");
    if (sendBtn) {
        sendBtn.disabled = isLocked;
    }
}

// === Voice: MediaRecorder ===

function toggleVoiceControls() {
    var voiceControls = document.getElementById('voiceControls');
    if (voiceControls.style.display === 'flex') {
        voiceControls.style.display = 'none';
        if (speechSynthesis.speaking) {
            speechSynthesis.cancel();
            updateVoiceStatus(getVoiceStatusText('speechStopped'));
            var btn = document.getElementById('voiceOutputBtn');
            if (btn) btn.classList.remove('speaking');
        }
        if (isListening && mediaRecorder && mediaRecorder.state === 'recording') {
            stopMediaRecording();
        }
    } else {
        voiceControls.style.display = 'flex';
    }
}

async function initMediaRecorder() {
    try {
        audioStream = await navigator.mediaDevices.getUserMedia({ audio: true });
        mediaRecorder = new MediaRecorder(audioStream);
        audioChunks = [];

        mediaRecorder.ondataavailable = function(event) {
            if (event.data.size > 0) audioChunks.push(event.data);
        };

        mediaRecorder.onstop = function() {
            if (audioChunks.length > 0) {
                var audioBlob = new Blob(audioChunks, { type: 'audio/webm' });
                sendAudioToServer(audioBlob);
            }
            if (audioStream) {
                audioStream.getTracks().forEach(function(track) { track.stop(); });
                audioStream = null;
            }
            if (recordingTimeout) {
                clearTimeout(recordingTimeout);
                recordingTimeout = null;
            }
            isListening = false;
            updateVoiceUI();
        };

        mediaRecorder.onerror = function(event) {
            console.error('MediaRecorder error:', event.error);
            updateVoiceStatus(getVoiceStatusText('error') + getVoiceStatusText('recording_error'));
            isListening = false;
            updateVoiceUI();
            if (audioStream) {
                audioStream.getTracks().forEach(function(track) { track.stop(); });
                audioStream = null;
            }
        };

        return true;
    } catch (error) {
        console.error('Microphone access error:', error);
        if (error.name === 'NotAllowedError') {
            updateVoiceStatus(getVoiceStatusText('microphone_denied'));
        } else if (error.name === 'NotFoundError') {
            updateVoiceStatus(getVoiceStatusText('microphone_not_found'));
        } else {
            updateVoiceStatus(getVoiceStatusText('notSupported'));
        }
        return false;
    }
}

async function sendAudioToServer(audioBlob) {
    updateVoiceStatus(getVoiceStatusText('recognizing'));

    var formData = new FormData();
    formData.append('audio', audioBlob, 'recording.webm');

    var langSelect = document.getElementById('selectLang');
    var selectedLang = langSelect.options[langSelect.selectedIndex].getAttribute('language');
    formData.append('language', selectedLang);

    try {
        var response = await fetch('/ai/transcribe/', {
            method: 'POST',
            body: formData,
            headers: { 'X-CSRFToken': getCsrfToken() }
        });

        if (!response.ok) {
            updateVoiceStatus(getVoiceStatusText(response.status === 429 ? 'rate_limited' : 'server_error'));
            return;
        }

        var data = await response.json();
        if (data.success && data.text) {
            var inputEl = document.getElementById('messageText') || document.getElementById('taskText');
            if (inputEl) {
                inputEl.value = data.text;
                updateVoiceStatus(getVoiceStatusText('recognized') + data.text);
                if (inputEl.value.trim()) simulateSend();
            }
        } else {
            updateVoiceStatus(getVoiceStatusText('recognition_failed'));
        }
    } catch (error) {
        console.error('Transcription error:', error);
        updateVoiceStatus(getVoiceStatusText('server_error'));
    }
}

async function startMediaRecording() {
    if (mediaRecorder && mediaRecorder.state === 'recording') return;

    if (speechSynthesis.speaking) {
        speechSynthesis.cancel();
        updateVoiceStatus(getVoiceStatusText('speechStopped'));
        var btn = document.getElementById('voiceOutputBtn');
        if (btn) btn.classList.remove('speaking');
    }

    var success = await initMediaRecorder();
    if (!success) return;

    mediaRecorder.start();
    isListening = true;
    updateVoiceUI();
    updateVoiceStatus(getVoiceStatusText('listening'));

    recordingTimeout = setTimeout(function() {
        if (mediaRecorder && mediaRecorder.state === 'recording') {
            stopMediaRecording();
            updateVoiceStatus(getVoiceStatusText('max_time_exceeded'));
        }
    }, MAX_RECORDING_TIME);
}

function stopMediaRecording() {
    if (mediaRecorder && mediaRecorder.state === 'recording') {
        mediaRecorder.stop();
    }
}

function toggleVoiceInput() {
    if (isListening) {
        stopMediaRecording();
    } else {
        startMediaRecording();
    }
}

// === Voice: SpeechSynthesis ===

function getSpeechSynthesisLanguage(lang) {
    var languageMap = { 'Russian': 'ru-RU', 'English': 'en-US', 'French': 'fr-FR' };
    return languageMap[lang] || 'en-US';
}

function speakLastResponse() {
    var messages = document.getElementById('messages');
    var assistantMessages = messages.querySelectorAll('.msg-assistant');

    if (assistantMessages.length === 0) {
        updateVoiceStatus(getVoiceStatusText('noResponse'));
        return;
    }
    if (isListening) stopMediaRecording();

    var lastMsg = assistantMessages[assistantMessages.length - 1];
    var panel = lastMsg.querySelector('.panel');
    var text = '';
    if (panel) {
        text = panel.innerText || panel.textContent || '';
    } else {
        text = lastMsg.innerText || lastMsg.textContent || '';
    }
    if (!text.trim()) {
        updateVoiceStatus(getVoiceStatusText('textEmpty'));
        return;
    }
    if (speechSynthesis.speaking) {
        speechSynthesis.cancel();
        updateVoiceStatus(getVoiceStatusText('speechStopped'));
        var btn = document.getElementById('voiceOutputBtn');
        if (btn) btn.classList.remove('speaking');
        return;
    }
    speakText(text);
}

function speakText(text) {
    if (speechSynthesis.speaking) speechSynthesis.cancel();

    var cleanText = cleanSpeechText(text);
    if (!cleanText.trim()) {
        updateVoiceStatus(getVoiceStatusText('noText'));
        return;
    }

    var langSelect = document.getElementById('selectLang');
    var selectedLang = langSelect.options[langSelect.selectedIndex].getAttribute('language');

    currentUtterance = new SpeechSynthesisUtterance(cleanText);
    currentUtterance.lang = getSpeechSynthesisLanguage(selectedLang);
    currentUtterance.rate = 0.9;
    currentUtterance.pitch = 1;

    currentUtterance.onstart = function() {
        updateVoiceStatus(getVoiceStatusText('speaking'));
        var btn = document.getElementById('voiceOutputBtn');
        if (btn) btn.classList.add('speaking');
    };
    currentUtterance.onend = function() {
        updateVoiceStatus(getVoiceStatusText('speechEnd'));
        var btn = document.getElementById('voiceOutputBtn');
        if (btn) btn.classList.remove('speaking');
    };
    currentUtterance.onerror = function() {
        updateVoiceStatus(getVoiceStatusText('speechError'));
        var btn = document.getElementById('voiceOutputBtn');
        if (btn) btn.classList.remove('speaking');
    };

    speechSynthesis.speak(currentUtterance);
}

function cleanSpeechText(text) {
    if (!text) return '';
    var cleanText = text;

    if (speakThinkEnabled) {
        cleanText = cleanText.replace(/Показать:.*?(Скрыть:|$)/g, '');
        cleanText = cleanText.replace(/Скрыть:.*?(Показать:|$)/g, '');
        cleanText = cleanText.replace(/<[^>]*>/g, '');
        return cleanText.trim();
    }

    cleanText = cleanText.replace(/<think>[\s\S]*?<\/think>/g, '');

    var technicalPatterns = [
        /^\d{2}:\d{2}:\d{2}\s+Запрос успешно обработан\s*$/gm,
        /^\d{2}:\d{2}:\d{2}\s+Request processed successfully\s*$/gm,
        /Модель:\s*.+/gi,
        /Время обработки запроса:\s*.+сек/gi,
        /Потрачено токенов:\s*\d+/gi,
        /^Скрыть:\s*(Ассистент|Assistant|Vous)\s*$/gim,
        /^Показать:\s*(Ассистент|Assistant|Vous)\s*$/gim,
        /^\d{2}:\d{2}:\d{2}\s*$/gm
    ];
    technicalPatterns.forEach(function(pattern) { cleanText = cleanText.replace(pattern, ''); });

    cleanText = cleanText.replace(/Показать:.*?(Скрыть:|$)/g, '');
    cleanText = cleanText.replace(/Скрыть:.*?(Показать:|$)/g, '');
    cleanText = cleanText.replace(/<[^>]*>/g, '');

    var servicePatterns = [
        /\b(?:Ассистент|Assistant|Vous|Вы|User|Пользователь)\s*:\s*/gi,
        /\b(?:Скрыть|Показать|Hide|Show)\s*:\s*/gi,
        /\bЗапрос успешно обработан\b/gi,
        /\bRequest processed successfully\b/gi,
        /\bОбрабатываю запрос пользователя\b/gi,
        /\bProcessing user request\b/gi,
        /\bКонтекст очищен\b/gi,
        /\bContext cleared\b/gi,
        /\bСоединение установлено\b/gi,
        /\bConnection established\b/gi,
        /\bГотов к работе\b/gi,
        /\bReady to work\b/gi,
        /\bСообщение отправлено\b/gi,
        /\bMessage sent\b/gi
    ];
    servicePatterns.forEach(function(pattern) { cleanText = cleanText.replace(pattern, ''); });

    cleanText = cleanText.replace(/\n\s*\n/g, '\n');
    cleanText = cleanText.replace(/\s+/g, ' ').trim();
    return cleanText;
}

function stopSpeech() {
    if (speechSynthesis.speaking) {
        speechSynthesis.cancel();
        updateVoiceStatus(getVoiceStatusText('speechStopped'));
    }
    if (isListening && mediaRecorder && mediaRecorder.state === 'recording') {
        stopMediaRecording();
    }
    var btn = document.getElementById('voiceOutputBtn');
    if (btn) btn.classList.remove('speaking');
}

// === Voice UI helpers ===

function updateVoiceUI() {
    var voiceBtn = document.getElementById('voiceInputBtn');
    var voiceIndicator = document.getElementById('voiceIndicator');
    if (voiceBtn) {
        if (isListening) voiceBtn.classList.add('recording');
        else voiceBtn.classList.remove('recording');
    }
    if (voiceIndicator) {
        if (isListening) voiceIndicator.classList.add('active');
        else voiceIndicator.classList.remove('active');
    }
}

function updateVoiceStatus(message) {
    var voiceStatus = document.getElementById('voiceStatus');
    if (voiceStatus) voiceStatus.textContent = message;
}

// === Localization (shared across all pages) ===

var localization = {
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
        choosePrompt: "Выберите промпт",
        voiceMode: "Голосовой режим",
        voiceInput: "Голосовой ввод",
        voiceOutput: "Озвучить ответ",
        speakThinkLabel: "Озвучивать дополнительную информацию",
        voiceStatus: {
            listening: "Запись голоса",
            recognized: "Распознано: ",
            recognizing: "Распознаю...",
            error: "Ошибка: ",
            readyForVoice: "Готов к голосовому вводу",
            notSupported: "Голосовой ввод не поддерживается вашим браузером или нет микрофона",
            startError: "Ошибка запуска записи",
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
            ready: 'Готов к работе. Нажмите "Голосовой режим" для активации голосовых функций.',
            recording_error: "Ошибка записи",
            recognition_failed: "Не удалось распознать речь",
            server_error: "Ошибка связи с сервером",
            rate_limited: "Слишком много запросов. Попробуйте позже.",
            microphone_denied: "Разрешите доступ к микрофону",
            microphone_not_found: "Микрофон не найден",
            max_time_exceeded: "Превышено время записи",
            select_prog_lang: "Выберите язык программирования",
            taskLoadError: "Не удалось загрузить условие задачи",
            taskNotFound: "Задача не найдена",
            task: "Задача:",
            codetx: "Код программы:",
            taskplace: "Вставьте сюда условие задачи",
            tokensUsed: "Потрачено сегодня",
            tokensRemaining: "Осталось",
            tokensNoLimit: "Использовано сегодня"
        },
        groqLoading: "Загрузка лимитов...",
        groqUnavailable: "Лимиты недоступны",
        groqTokens: "токенов",
        groqRequests: "запросов/min",
        groqRemaining: "Осталось"
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
        choosePrompt: "Choose prompt",
        voiceMode: "Voice mode",
        voiceInput: "Voice input",
        voiceOutput: "Speak answer",
        speakThinkLabel: "Voice extra information",
        voiceStatus: {
            listening: "Voice recording",
            recognized: "Recognized: ",
            recognizing: "Recognizing...",
            error: "Error: ",
            readyForVoice: "Ready for voice input",
            notSupported: "Voice input not supported in your browser or no microphone",
            startError: "Error starting recording",
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
            ready: 'Ready. Click "Voice mode" to activate voice features.',
            recording_error: "Recording error",
            recognition_failed: "Recognition failed",
            server_error: "Server connection error",
            rate_limited: "Too many requests. Please try again later.",
            microphone_denied: "Microphone access denied",
            microphone_not_found: "Microphone not found",
            max_time_exceeded: "Max recording time exceeded",
            select_prog_lang: "Select programming language",
            taskLoadError: "Failed to load task statement",
            taskNotFound: "Task not found",
            task: "Task:",
            codetx: "Program code:",
            taskplace: "Paste the task condition here",
            tokensUsed: "Used today",
            tokensRemaining: "Remaining",
            tokensNoLimit: "Used today"
        },
        groqLoading: "Loading limits...",
        groqUnavailable: "Limits unavailable",
        groqTokens: "tokens",
        groqRequests: "requests/min",
        groqRemaining: "Remaining"
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
        choosePrompt: "Choisir le pré-promp",
        voiceMode: "Mode vocal",
        voiceInput: "Saisie vocale",
        voiceOutput: "Lire la réponse",
        speakThinkLabel: "Informations supplémentaires vocales",
        voiceStatus: {
            listening: "Enregistrement vocal",
            recognized: "Reconnu : ",
            recognizing: "Reconnaissance...",
            error: "Erreur : ",
            readyForVoice: "Prêt pour la saisie vocale",
            notSupported: "Saisie vocale non supportée par votre navigateur ou pas de microphone",
            startError: "Erreur de démarrage de l'enregistrement",
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
            ready: 'Prêt. Cliquez sur "Mode vocal" pour activer les fonctions vocales.',
            recording_error: "Erreur d'enregistrement",
            recognition_failed: "Échec de reconnaissance",
            server_error: "Erreur de connexion au serveur",
            rate_limited: "Trop de requêtes. Réessayez plus tard.",
            microphone_denied: "Accès au micro refusé",
            microphone_not_found: "Microphone introuvable",
            max_time_exceeded: "Durée d'enregistrement dépassée",
            select_prog_lang: "Sélectionnez le langage de programmation",
            taskLoadError: "Impossible de charger l'énoncé de la tâche",
            taskNotFound: "Tâche non trouvée",
            task: "Tâche :",
            codetx: "Code du programme :",
            taskplace: "Collez ici l'énoncé de la tâche",
            tokensUsed: "Utilisé aujourd'hui",
            tokensRemaining: "Restant",
            tokensNoLimit: "Utilisé aujourd'hui"
        },
        groqLoading: "Chargement des limites...",
        groqUnavailable: "Limites indisponibles",
        groqTokens: "jetons",
        groqRequests: "requêtes/min",
        groqRemaining: "Restant"
    }
};

function getVoiceStatusText(key, param) {
    param = param || '';
    var selectLang = document.getElementById('selectLang');
    var lang = selectLang.options[selectLang.selectedIndex].getAttribute('language');
    var msg = (localization[lang] && localization[lang].voiceStatus && localization[lang].voiceStatus[key])
              || (localization.Russian.voiceStatus[key]) || key;
    return msg + param;
}

function getUiString(key, defaultValue) {
    defaultValue = defaultValue || '';
    var selectLang = document.getElementById('selectLang');
    var lang = selectLang.options[selectLang.selectedIndex].getAttribute('language');
    var dict = localization[lang] || {};
    return dict[key] || (dict.voiceStatus && dict.voiceStatus[key]) || defaultValue;
}

// === Interface language persistence ===

function saveInterfaceLanguage() {
    try {
        var selectLang = document.getElementById('selectLang');
        if (selectLang) {
            var lang = selectLang.options[selectLang.selectedIndex].getAttribute('language');
            localStorage.setItem(INTERFACE_LANG_KEY, lang);
        }
        saveSelections();
    } catch (e) {}
}

function restoreInterfaceLanguage() {
    try {
        var savedLang = localStorage.getItem(INTERFACE_LANG_KEY);
        if (!savedLang) return;
        var selectLang = document.getElementById('selectLang');
        if (!selectLang) return;
        var option = Array.from(selectLang.options).find(function(o) { return o.getAttribute('language') === savedLang; });
        if (option) selectLang.selectedIndex = option.index;
    } catch (e) {}
}

// === Shared text persistence ===

function saveSharedText() {
    try {
        var messageText = document.getElementById('messageText') || document.getElementById('taskText');
        var codeText = document.getElementById('codeText');
        var saved = JSON.parse(localStorage.getItem(SHARED_TEXT_KEY) || '{}');
        if (messageText) saved.message = messageText.value;
        if (codeText) saved.code = codeText.value;
        localStorage.setItem(SHARED_TEXT_KEY, JSON.stringify(saved));
    } catch (e) {}
}

function restoreSharedText() {
    try {
        var saved = JSON.parse(localStorage.getItem(SHARED_TEXT_KEY) || '{}');
        var messageText = document.getElementById('messageText') || document.getElementById('taskText');
        var codeText = document.getElementById('codeText');
        if (messageText && saved.message !== undefined) messageText.value = saved.message;
        if (codeText && saved.code !== undefined) codeText.value = saved.code;
    } catch (e) {}
}

// === Accordion ===

function initAccordionForMessages() {
    var messages = document.getElementById('messages');
    var allMessages = messages.querySelectorAll(':scope > li');
    var roles = [];
    for (var i = 0; i < allMessages.length; i++) {
        roles.push(i % 2 === 0 ? 'user' : 'assistant');
    }

    var selectLang = document.getElementById('selectLang');
    var langAttr = selectLang.options[selectLang.selectedIndex].getAttribute('language');
    var roleLabels = {
        Russian: { user: 'Вы', assistant: 'Ассистент', other: 'Други' },
        English: { user: 'You', assistant: 'Assistant', other: 'Others' },
        French: { user: 'Vous', assistant: 'Assistant', other: 'Autres' }
    };

    function getRoleLabel(role, lang) {
        return (roleLabels[lang] && roleLabels[lang][role]) ? roleLabels[lang][role] : role;
    }

    allMessages.forEach(function(li, idx) {
        if (!li.classList.contains('accordion-li')) {
            li.classList.add('accordion-li');
            var role = roles[idx] || 'other';
            li.classList.remove('msg-user', 'msg-assistant');
            if (role === 'user') li.classList.add('msg-user');
            if (role === 'assistant') li.classList.add('msg-assistant');

            var btn = document.createElement('button');
            btn.className = 'accordion';
            if (role === 'user') btn.classList.add('accordion-user');
            if (role === 'assistant') btn.classList.add('accordion-assistant');
            btn.textContent = 'Показать: ' + getRoleLabel(role, langAttr);

            var panel = document.createElement('div');
            panel.className = 'panel';
            while (li.firstChild) panel.appendChild(li.firstChild);
            li.appendChild(btn);
            li.appendChild(panel);

            btn.addEventListener('click', function() {
                panel.classList.toggle('open');
                btn.classList.toggle('active');
                btn.textContent = panel.classList.contains('open')
                    ? 'Скрыть: ' + getRoleLabel(role, langAttr)
                    : 'Показать: ' + getRoleLabel(role, langAttr);
            });
        }
    });

    if (allMessages.length > 0) {
        var lastLi = allMessages[allMessages.length - 1];
        var lastBtn = lastLi.querySelector('.accordion');
        var lastPanel = lastLi.querySelector('.panel');
        if (lastBtn && lastPanel) {
            lastPanel.classList.add('open');
            lastBtn.classList.add('active');
            var lastRole = roles[allMessages.length - 1] || 'other';
            lastBtn.textContent = 'Скрыть: ' + getRoleLabel(lastRole, langAttr);
        }
    }
    window._accordionRoles = roles;
}

function updateAccordionLabels() {
    var selectLang = document.getElementById('selectLang');
    var langAttr = selectLang.options[selectLang.selectedIndex].getAttribute('language');
    var roleLabels = {
        Russian: { user: 'Вы', assistant: 'Ассистент', other: 'Други' },
        English: { user: 'You', assistant: 'Assistant', other: 'Others' },
        French: { user: 'Vous', assistant: 'Assistant', other: 'Autres' }
    };

    function getRoleLabel(role, lang) {
        return (roleLabels[lang] && roleLabels[lang][role]) ? roleLabels[lang][role] : role;
    }

    var allMessages = document.getElementById('messages').querySelectorAll('li');
    var roles = window._accordionRoles || [];
    allMessages.forEach(function(li, idx) {
        var btn = li.querySelector('.accordion');
        var panel = li.querySelector('.panel');
        if (btn && panel) {
            var role = roles[idx] || 'other';
            btn.textContent = panel.classList.contains('open')
                ? 'Скрыть: ' + getRoleLabel(role, langAttr)
                : 'Показать: ' + getRoleLabel(role, langAttr);
        }
    });
}

function collapseAllExceptLast() {
    var allMessages = document.getElementById('messages').querySelectorAll('li');
    var selectLang = document.getElementById('selectLang');
    var langAttr = selectLang.options[selectLang.selectedIndex].getAttribute('language');
    var roleLabels = {
        Russian: { user: 'Вы', assistant: 'Ассистент', other: 'Други' },
        English: { user: 'You', assistant: 'Assistant', other: 'Others' },
        French: { user: 'Vous', assistant: 'Assistant', other: 'Autres' }
    };
    var roles = window._accordionRoles || [];

    function getRoleLabel(role, lang) {
        return (roleLabels[lang] && roleLabels[lang][role]) ? roleLabels[lang][role] : role;
    }

    allMessages.forEach(function(li, idx) {
        var btn = li.querySelector('.accordion');
        var panel = li.querySelector('.panel');
        var role = roles[idx] || 'other';
        if (btn && panel) {
            if (idx === allMessages.length - 1) {
                panel.classList.add('open');
                btn.classList.add('active');
                btn.textContent = 'Скрыть: ' + getRoleLabel(role, langAttr);
            } else {
                panel.classList.remove('open');
                btn.classList.remove('active');
                btn.textContent = 'Показать: ' + getRoleLabel(role, langAttr);
            }
        }
    });
}

// === Resize helpers ===

var isResizing = false;

function startResize(event) {
    isResizing = true;
    window.addEventListener('mousemove', resize);
    window.addEventListener('mouseup', stopResize);
}

function resize(event) {
    if (isResizing) {
        var container = document.querySelector('form');
        var newWidth = container.getBoundingClientRect().right - event.clientX;
        var minWidth = 500;
        if (newWidth > minWidth) container.style.width = newWidth + 'px';
    }
}

function stopResize() {
    isResizing = false;
    window.removeEventListener('mousemove', resize);
    window.removeEventListener('mouseup', stopResize);
}

// === Common WebSocket init (pages can override) ===
// Default implementation — chat page. decide_task/find_error override initWebSocket.

function initWebSocket() {
    try {
        var wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        var wsUrl = wsProtocol + '//' + window.location.host + '/ai/chat/ws/' + client_id + window.location.search;
        ws = new WebSocket(wsUrl);

        ws.onopen = function(event) {
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
            updateVoiceStatus(getVoiceStatusText('wsError'));
            setRequestLock(false);
            notEnter = false;
        };

        ws.onclose = function(event) {
            updateVoiceStatus(getVoiceStatusText('connectionClosed'));
            setRequestLock(false);
            notEnter = false;
        };
    } catch (error) {
        console.error('WebSocket init error:', error);
    }
}

// === Common: Enter key handler ===

document.addEventListener("keydown", function(event) {
    var checkbox = document.querySelector(".inp");
    if (event.key === "Enter" && (!checkbox || checkbox.checked) && !event.shiftKey && !notEnter) {
        sendMessage(event);
    }
});

// === Common: sidebar toggle ===

var toggleButton = document.querySelector('.toggle-button');
var sidebar = document.querySelector('.sidebar');
if (toggleButton && sidebar) {
    toggleButton.addEventListener('click', function() {
        sidebar.classList.toggle('open');
    });
}

// === Common: clearContext (default — pages can override) ===

function clearContext() {
    if (!ws) return;
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

// === Desktop/Mobile mode toggle ===
function toggleDesktopMode() {
    var html = document.documentElement;
    var btn = document.querySelector('.desktop-toggle-btn');
    if (html.classList.contains('desktop-mode')) {
        html.classList.remove('desktop-mode');
        if (btn) btn.textContent = '🖥️';
        try { localStorage.removeItem('ai_desktop_mode'); } catch(e) {}
    } else {
        html.classList.add('desktop-mode');
        if (btn) btn.textContent = '📱';
        try { localStorage.setItem('ai_desktop_mode', '1'); } catch(e) {}
    }
}

// Restore desktop mode on page load
(function() {
    try {
        if (localStorage.getItem('ai_desktop_mode') === '1') {
            document.documentElement.classList.add('desktop-mode');
            var btn = document.querySelector('.desktop-toggle-btn');
            if (btn) btn.textContent = '📱';
        }
    } catch(e) {}
})();

// === Common: language change handler (pages can extend via selectLang listener) ===
// Pages register their own additional selectLang change listeners for page-specific UI.