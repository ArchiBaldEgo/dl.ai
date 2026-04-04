// Глобальные переменные
            var ws = null;
            var client_id = generateClientId();
            var notEnter = false;
            
            // Голосовые переменные
            var recognition = null;
            var isListening = false;
            var speechSynthesis = window.speechSynthesis;
            var currentUtterance = null;
            var speakThinkEnabled = true;

            // Генерация client_id
            function generateClientId() {
                return 'client_' + Math.random().toString(36).substr(2, 9) + '_' + Date.now();
            }

            // Функции голосового управления
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
                    
                    recognition.onstart = function() {
                        isListening = true;
                        updateVoiceUI();
                        updateVoiceStatus('Слушаю... Говорите сейчас');
                    };
                    
                    recognition.onresult = function(event) {
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
                            updateVoiceStatus('Распознано: ' + finalTranscript);
                            setTimeout(() => {
                                if (document.getElementById('messageText').value.trim()) {
                                    simulateSend();
                                }
                            }, 500);
                        } else if (interimTranscript) {
                            updateVoiceStatus('Распознаю: ' + interimTranscript);
                        }
                    };
                    
                    recognition.onerror = function(event) {
                        updateVoiceStatus('Ошибка: ' + event.error);
                        isListening = false;
                        updateVoiceUI();
                    };
                    
                    recognition.onend = function() {
                        isListening = false;
                        updateVoiceUI();
                        updateVoiceStatus('Готов к голосовому вводу');
                    };
                    
                } catch (error) {
                    updateVoiceStatus('Голосовой ввод не поддерживается вашим браузером');
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
                        updateVoiceStatus('Ошибка запуска распознавания');
                    }
                }
            }

            function speakLastResponse() {
                const messages = document.getElementById('messages');
                const assistantMessages = messages.querySelectorAll('.msg-assistant');
                
                if (assistantMessages.length === 0) {
                    updateVoiceStatus('Нет ответов для озвучивания');
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
                    updateVoiceStatus('Текст для озвучивания пуст');
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
                    updateVoiceStatus('Нет текста для озвучивания');
                    return;
                }
                
                const langSelect = document.getElementById('selectLang');
                const selectedLang = langSelect.options[langSelect.selectedIndex].getAttribute('language');
                
                currentUtterance = new SpeechSynthesisUtterance(cleanText);
                currentUtterance.lang = getSpeechSynthesisLanguage(selectedLang);
                currentUtterance.rate = 0.9;
                currentUtterance.pitch = 1;
                
                currentUtterance.onstart = function() {
                    updateVoiceStatus('Озвучиваю...');
                    document.getElementById('voiceOutputBtn').classList.add('speaking');
                };
                
                currentUtterance.onend = function() {
                    updateVoiceStatus('Озвучивание завершено');
                    document.getElementById('voiceOutputBtn').classList.remove('speaking');
                };
                
                currentUtterance.onerror = function(event) {
                    updateVoiceStatus('Ошибка озвучивания');
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
                    updateVoiceStatus('Озвучивание остановлено');
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

            function simulateSend() {
                if (!ws || ws.readyState !== WebSocket.OPEN) {
                    updateVoiceStatus('Ошибка: соединение не установлено');
                    return;
                }
                
                var value = document.querySelector("#select").value;
                var language = document.querySelector("#selectLang").value;
                var input = document.getElementById("messageText");
                var progLng = document.querySelector("#selectProgLng").value;
                var preprompt = document.querySelector("#selectPrompt").value;
                
                if (!input.value.trim()) {
                    return;
                }
                if (!progLng) {
                    updateVoiceStatus('Выберите язык программирования перед отправкой.');
                    return;
                }
                
                ws.send(JSON.stringify({
                    type: '2',
                    message: input.value,
                    value: value,
                    language: language,
                    progLng: progLng,
                    preprompt: preprompt
                }));
                
                updateVoiceStatus('Сообщение отправлено');
                input.value = '';
            }

            // Инициализация WebSocket
            function initWebSocket() {
                try {
                    var wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
                    var wsUrl = `${wsProtocol}//${window.location.host}/ai/chat/ws/${client_id}`;
                    
                    console.log('Connecting to WebSocket:', wsUrl);
                    ws = new WebSocket(wsUrl);

                    ws.onopen = function(event) {
                        console.log('WebSocket connection established');
                        updateVoiceStatus('Соединение установлено.');
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
                        var input = document.getElementById("messageText");
                        input.value = '';
                        notEnter = false;
                        
                        initAccordionForMessages();
                        collapseAllExceptLast();
                    };

                    ws.onerror = function(error) {
                        console.error('WebSocket error:', error);
                        updateVoiceStatus('Ошибка соединения');
                    };

                    ws.onclose = function(event) {
                        console.log('WebSocket connection closed');
                        updateVoiceStatus('Соединение закрыто');
                    };

                } catch (error) {
                    console.error('Error initializing WebSocket:', error);
                }
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
                
                var value = document.querySelector("#select").value;
                var language = document.querySelector("#selectLang").value;
                var input = document.getElementById("messageText");
                var progLng = document.querySelector("#selectProgLng").value;
                var preprompt = document.querySelector("#selectPrompt").value;
                
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
                    preprompt: preprompt
                }));
                input.value = '';
            }

            // ОРИГИНАЛЬНАЯ функция clearContext - НЕ ТРОГАТЬ
            function clearContext() {
                if (!ws) {
                    console.log("WebSocket is not initialized");
                    return;
                }
                
                if (ws.readyState === WebSocket.OPEN) {
                    ws.send(JSON.stringify({ action: 'clear_context' }));
                    
                    // Также очищаем локально сообщения
                    var messages = document.getElementById('messages');
                    messages.innerHTML = '';
                    
                    // Показываем сообщение об очистке
                    var clearMessage = document.createElement('li');
                    clearMessage.innerHTML = '<div style="color: green;">Контекст очищен</div>';
                    messages.appendChild(clearMessage);
                    messages.scrollTo({ top: messages.scrollHeight, behavior: 'smooth' });
                    
                } else {
                    console.log("WebSocket is not open. State:", ws.readyState);
                    alert("Соединение не установлено");
                }
            }

            // Обработка клавиши Enter
            document.addEventListener("keydown", function (event) {
                const checkbox = document.querySelector(".inp");
                if (event.key === "Enter" && (!checkbox || checkbox.checked) && !event.shiftKey && !notEnter) {
                    notEnter = true;
                    sendMessage(event);
                }
            });

            const toggleButton = document.querySelector('.toggle-button');
            const sidebar = document.querySelector('.sidebar');

            toggleButton.addEventListener('click', () => {
                sidebar.classList.toggle('open');
            });

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

            function stopResize() {
                isResizing = false;
                window.removeEventListener('mousemove', resize);
                window.removeEventListener('mouseup', stopResize);
            }

            const localization = {
                Russian: {
                    send: "Отправить",
                    clear: "Очистить контекст",
                    placeholder: "Задайте вопрос (желательно на английском во избежание ошибок), для красивого форматирования оберните код в ```(буква Ё на клавиатуре)\nПример форматирования кода:\n```\nprint('Hello, world!')\n```",
                    adminPanel: "Админ-Панель",
                    chat: "Чат с DLAI",
                    decideTask: "Реши задачу",
                    findError: "В чём ошибка?",
                    enterHint: "При нажатии на Enter будет отправляться вопрос (для переноса строки Enter+Shift)",
                    preprompt: "Препромпт",
                    chooseLanguage: "Выберите язык",
                    chooseTheme: "Выберите тему"
                },
                English: {
                    send: "Send",
                    clear: "Clear Context",
                    placeholder: "Ask a question (preferably in English to avoid errors), for nice formatting wrap the code in ```\nExample of code formatting:\n```\nprint('Hello, world!')\n```",
                    adminPanel: "Admin Panel",
                    chat: "Chat with DLAI",
                    decideTask: "Solve the task",
                    findError: "What's the error?",
                    enterHint: "Press Enter to send the question (Shift+Enter for a new line)",
                    preprompt: "Preprompt",
                    chooseLanguage: "Choose language",
                    chooseTheme: "Choose theme"
                },
                French: {
                    send: "Envoyer",
                    clear: "Effacer le contexte",
                    placeholder: "Posez une question (de préférence en anglais pour éviter les erreurs), pour un bon formatage, encadrez le code dans ```\nExemple de formatage du code:\n```\nprint('Hello, world!')\n```",
                    adminPanel: "Panneau Admin",
                    chat: "Chat avec DLAI",
                    decideTask: "Résoudre la tâche",
                    findError: "Quelle est l'erreur?",
                    enterHint: "Appuyez sur Entrée pour envoyer la question (Shift+Enter pour une nouvelle ligne)",
                    preprompt: "Pré-promp",
                    chooseLanguage: "Choisir la langue",
                    chooseTheme: "Choisir le thème"
                }
            };

            document.getElementById("selectLang").addEventListener("change", function () {
                const selectedLang = this.options[this.selectedIndex].getAttribute("language");
                document.querySelector("button[type='submit']").textContent = localization[selectedLang].send;
                document.querySelector("button[onclick='clearContext()']").textContent = localization[selectedLang].clear;
                document.getElementById("messageText").setAttribute("placeholder", localization[selectedLang].placeholder);
                document.querySelector(".sidebar-header").textContent = localization[selectedLang].adminPanel;
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
                updateAccordionLabels();
            });

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
                
                function getRoleLabel(role, lang) {
                    return (roleLabels[lang] && roleLabels[lang][role]) ? roleLabels[lang][role] : role;
                }
                
                const allMessages = document.getElementById('messages').querySelectorAll('li');
                const roles = window._accordionRoles || [];
                allMessages.forEach(function (li, idx) {
                    const btn = li.querySelector('.accordion');
                    const panel = li.querySelector('.panel');
                    if (btn && panel) {
                        const role = roles[idx] || 'other';
                        btn.textContent = panel.classList.contains('open')
                            ? `Скрыть: ${getRoleLabel(role, langAttr)}`
                            : `Показать: ${getRoleLabel(role, langAttr)}`;
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

            // Загрузка языков, тем и промптов
            document.addEventListener("DOMContentLoaded", async () => {
                const languageSelect = document.getElementById("selectProgLng");
                const topicSelect = document.getElementById("selectTheme");
                const promptSelect = document.getElementById("selectPrompt");

                async function fetchData(url) {
                    try {
                        const urlWithAuth = `${url}${window.location.search || ''}`;
                        const response = await fetch(urlWithAuth);
                        if (!response.ok) {
                            throw new Error(`HTTP error! status: ${response.status}`);
                        }
                        const data = await response.json();
                        console.log('Fetched data:', data);
                        return data;
                    } catch (error) {
                        console.error('Error fetching data:', error);
                        return [];
                    }
                }

                // Функция для выбора первого варианта, если он единственный
                function selectFirstIfSingle(selectElement) {
                    if (selectElement && selectElement.options.length === 1) {
                        selectElement.selectedIndex = 0;
                    }
                }

                async function loadLanguages() {
                    const languages = await fetchData("/ai/api/languages");
                    languageSelect.innerHTML = '<option value="">Выберите язык</option>';
                    if (languages && languages.length > 0) {
                        languages.forEach(lang => {
                            const option = new Option(lang.language_name, lang.id);
                            languageSelect.appendChild(option);
                        });
                    }
                    selectFirstIfSingle(languageSelect);
                }

                async function loadTopics(languageId) {
                    const topics = await fetchData("/ai/api/topics");
                    topicSelect.innerHTML = '<option value="">Выберите тему</option>';
                    if (topics && topics.length > 0) {
                        const filteredTopics = topics.filter(topic => topic.programming_language == languageId);
                        filteredTopics.forEach(topic => {
                            const option = new Option(topic.topic_name, topic.id);
                            topicSelect.appendChild(option);
                        });
                    }
                    selectFirstIfSingle(topicSelect);
                }

                async function loadPrompts(topicId) {
                    const prompts = await fetchData("/ai/api/prompts");
                    promptSelect.innerHTML = '<option value="">Выберите промпт</option>';
                    if (prompts && prompts.length > 0) {
                        const filteredPrompts = prompts.filter(prompt => prompt.topic_id == topicId);
                        filteredPrompts.forEach(prompt => {
                            const option = new Option(prompt.prompt_name, prompt.id);
                            promptSelect.appendChild(option);
                        });
                    }
                    selectFirstIfSingle(promptSelect);
                }

                languageSelect.addEventListener("change", async () => {
                    const languageId = parseInt(languageSelect.value);
                    topicSelect.innerHTML = '<option value="">Выберите тему</option>';
                    promptSelect.innerHTML = '<option value="">Выберите промпт</option>';
                    if (!isNaN(languageId)) {
                        await loadTopics(languageId);
                    }
                });

                topicSelect.addEventListener("change", async () => {
                    const topicId = parseInt(topicSelect.value);
                    promptSelect.innerHTML = '<option value="">Выберите промпт</option>';
                    if (!isNaN(topicId)) {
                        await loadPrompts(topicId);
                    }
                });

                await loadLanguages();
            });

            window.onload = function () {
                console.log('Initializing WebSocket with client_id:', client_id);
                initWebSocket();
                initSpeechRecognition();
                document.getElementById("selectLang").dispatchEvent(new Event("change"));
                initAccordionForMessages();
                updateVoiceStatus('Готов к работе. Нажмите "Голосовой режим" для активации голосовых функций.');
                
                // Инициализация чекбокса think-блоков
                const speakThinkCheckbox = document.getElementById('speakThinkContent');
                speakThinkCheckbox.addEventListener('change', function() {
                    speakThinkEnabled = this.checked;
                });
            };