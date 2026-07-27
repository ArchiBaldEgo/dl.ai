/**
 * chat_template.js — страница «Чат с DLAI» (type=1).
 *
 * Общие хелперы (cookies, CSRF, voice, accordion, markdown, localization, persistence)
 * находятся в ai-common.js, который подключается ПЕРЕД этим файлом.
 * Здесь только специфика чата: sendMessage (type=1), simulateSend, selectLang handler, window.onload.
 */

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
    if (!input.value.trim()) return;

    ws.send(JSON.stringify({
        type: '1',
        message: input.value,
        value: value,
        language: language
    }));
    setRequestLock(true);
    notEnter = true;
    updateVoiceStatus(getVoiceStatusText('messageSent'));
    input.value = '';
    saveSharedText();
}

function sendMessage(event) {
    event.preventDefault();
    if (!ws) { alert("Соединение не установлено. Пожалуйста, подождите..."); return; }
    if (ws.readyState !== WebSocket.OPEN) { alert("Соединение не установлено. Пожалуйста, подождите..."); return; }
    if (requestInFlight) { alert("Дождитесь ответа модели перед новым запросом."); return; }

    var value = document.querySelector("#select").value;
    var language = document.querySelector("#selectLang").value;
    var input = document.getElementById("messageText");
    if (!value) { alert("Сегодня нет доступных моделей. Повторите позже."); return; }
    if (!input.value.trim()) { alert("Пожалуйста, введите сообщение"); return; }

    ws.send(JSON.stringify({
        type: '1',
        message: input.value,
        value: value,
        language: language
    }));
    setRequestLock(true);
    notEnter = true;
    input.value = '';
    saveSharedText();
}

// === selectLang change handler — обновляет UI на странице чата ===
document.getElementById("selectLang").addEventListener("change", function() {
    var selectedLang = this.options[this.selectedIndex].getAttribute("language");

    var submitBtn = document.querySelector("button[type='submit']");
    if (submitBtn) submitBtn.textContent = localization[selectedLang].send;

    var clearBtn = document.querySelector("button[onclick='clearContext()']");
    if (clearBtn) clearBtn.textContent = localization[selectedLang].clear;

    var messageText = document.getElementById("messageText");
    if (messageText) messageText.setAttribute("placeholder", localization[selectedLang].placeholder);

    var sidebarHeader = document.querySelector(".sidebar-header");
    if (sidebarHeader) sidebarHeader.textContent = localization[selectedLang].adminPanel;

    var testPanelLink = document.getElementById("testPanelLink");
    if (testPanelLink) testPanelLink.textContent = localization[selectedLang].testPanel;

    var opt1 = document.querySelector("#selectType option:nth-child(1)");
    if (opt1) opt1.textContent = localization[selectedLang].chat;
    var opt2 = document.querySelector("#selectType option:nth-child(2)");
    if (opt2) opt2.textContent = localization[selectedLang].decideTask;
    var opt3 = document.querySelector("#selectType option:nth-child(3)");
    if (opt3) opt3.textContent = localization[selectedLang].findError;

    var checkTextEl = document.querySelector(".check-text");
    if (checkTextEl) checkTextEl.textContent = localization[selectedLang].enterHint;

    var prepromptEl = document.querySelector(".preprompt");
    if (prepromptEl) prepromptEl.textContent = localization[selectedLang].preprompt;

    var voiceModeBtn = document.getElementById("voiceModeBtn");
    if (voiceModeBtn) voiceModeBtn.textContent = localization[selectedLang].voiceMode;
    var voiceInputBtn = document.getElementById("voiceInputBtn");
    if (voiceInputBtn) voiceInputBtn.textContent = localization[selectedLang].voiceInput;
    var voiceOutputBtn = document.getElementById("voiceOutputBtn");
    if (voiceOutputBtn) voiceOutputBtn.textContent = localization[selectedLang].voiceOutput;
    var speakThinkLabel = document.getElementById("speakThinkLabel");
    if (speakThinkLabel) speakThinkLabel.textContent = localization[selectedLang].speakThinkLabel;

    saveInterfaceLanguage();
    updateAccordionLabels();
    updateVoiceStatus(getVoiceStatusText('readyForVoice'));
});

// === window.onload — инициализация страницы чата ===
window.onload = function() {
    console.log('Initializing WebSocket with client_id:', client_id);
    restoreInterfaceLanguage();
    initWebSocket();
    document.getElementById("selectLang").dispatchEvent(new Event("change"));
    initAccordionForMessages();
    restoreSharedText();
    updateVoiceStatus(getVoiceStatusText('ready'));

    var speakThinkCheckbox = document.getElementById('speakThinkContent');
    if (speakThinkCheckbox) {
        speakThinkCheckbox.addEventListener('change', function() {
            speakThinkEnabled = this.checked;
        });
    }

    var messageText = document.getElementById('messageText');
    if (messageText) messageText.addEventListener('input', saveSharedText);

    // === Виджет лимитов токенов Groq ===
    var groqLimitsTimer = null;

    function updateGroqLimitsWidget() {
        var modelSelect = document.getElementById('select');
        if (!modelSelect) return;
        var selectedKey = modelSelect.value || '';
        var widget = document.getElementById('groqLimitsWidget');
        if (!widget) return;

        if (selectedKey.indexOf('Groq_') !== 0) {
            widget.style.display = 'none';
            if (groqLimitsTimer) { clearInterval(groqLimitsTimer); groqLimitsTimer = null; }
            return;
        }

        widget.style.display = 'block';
        var lang = document.querySelector('#selectLang');
        var selectedLang = lang ? lang.options[lang.selectedIndex].getAttribute('language') : 'Russian';
        var loc = localization[selectedLang] || localization.Russian;
        widget.textContent = loc.groqLoading;

        fetch('/ai/api/groq-limits/')
            .then(function(r) { return r.ok ? r.json() : Promise.reject(); })
            .then(function(data) {
                var limits = data.limits || {};
                var info = limits[selectedKey];
                if (!info) { widget.textContent = loc.groqUnavailable; return; }
                var remT = info.remaining_tokens;
                var limT = info.limit_tokens;
                var remR = info.remaining_requests;
                var limR = info.limit_requests;
                var pct = limT > 0 ? Math.round((remT / limT) * 100) : 0;
                var color = pct > 50 ? '#4CAF50' : (pct > 20 ? '#FF9800' : '#F44336');
                widget.innerHTML = '<span style="color:' + color + '; font-weight:bold;">' +
                    ' Groq: ' + loc.groqRemaining + ': ' + remT + ' / ' + limT + ' ' + loc.groqTokens + ' (' + pct + '%)' +
                    ' · ' + remR + ' / ' + limR + ' ' + loc.groqRequests +
                    '</span>';
            })
            .catch(function() { widget.textContent = loc.groqUnavailable; });
    }

    // === Виджет лимитов OpenRouter ===
    var orLimitsTimer = null;
    // OpenRouter free: ~20 запросов/день на модель, без credit card.
    // API не отдаёт rate-limit заголовки, поэтому показываем статичные лимиты.
    var OR_DAILY_LIMIT = 20;
    var OR_FREE_LABEL = 'OpenRouter (бесплатно)';

    function updateOpenRouterLimitsWidget() {
        var modelSelect = document.getElementById('select');
        if (!modelSelect) return;
        var selectedKey = modelSelect.value || '';
        var widget = document.getElementById('openrouterLimitsWidget');
        if (!widget) return;

        if (selectedKey.indexOf('OR_') !== 0) {
            widget.style.display = 'none';
            if (orLimitsTimer) { clearInterval(orLimitsTimer); orLimitsTimer = null; }
            return;
        }

        widget.style.display = 'block';
        var lang = document.querySelector('#selectLang');
        var selectedLang = lang ? lang.options[lang.selectedIndex].getAttribute('language') : 'Russian';
        var loc = localization[selectedLang] || localization.Russian;
        widget.innerHTML = '<span style="color:#7c3aed; font-weight:bold;">' +
            'OpenRouter (free): ~' + OR_DAILY_LIMIT + ' ' + loc.groqRequests +
            '</span>';
    }

    var modelSelectEl = document.getElementById('select');
    if (modelSelectEl) {
        modelSelectEl.addEventListener('change', function() {
            updateGroqLimitsWidget();
            updateOpenRouterLimitsWidget();
            var selectedKey = this.value || '';
            if (selectedKey.indexOf('Groq_') === 0) {
                if (!groqLimitsTimer) groqLimitsTimer = setInterval(updateGroqLimitsWidget, 60000);
            } else {
                if (groqLimitsTimer) { clearInterval(groqLimitsTimer); groqLimitsTimer = null; }
            }
            if (selectedKey.indexOf('OR_') === 0) {
                if (!orLimitsTimer) orLimitsTimer = setInterval(updateOpenRouterLimitsWidget, 60000);
            } else {
                if (orLimitsTimer) { clearInterval(orLimitsTimer); orLimitsTimer = null; }
            }
        });
        updateGroqLimitsWidget();
        updateOpenRouterLimitsWidget();
    }
};