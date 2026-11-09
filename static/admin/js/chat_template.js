/**
 * chat_template.js — страница «Чат с DLAI» (type=1).
 *
 * Общие хелперы (cookies, CSRF, voice, accordion, markdown, localization, persistence)
 * находятся в ai-common.js, который подключается ПЕРЕД этим файлом.
 * Здесь только специфика чата: sendMessage (type=1), simulateSend, selectLang handler, window.onload.
 */

// === PDF-вложение (только чат, только модели со зрением) ====================
//
// Файл НЕ отправляется модели: браузер шлёт его на /ai/api/pdf/, сервер
// строго валидирует (pypdf, без активного содержимого) и возвращает только
// текст. Этот текст уходит в WS-payload полем pdf; сервер повторно чистит и
// обрезает его (ai/services/pdf_upload.py). Клиентский гейт data-vision —
// удобство, окончательное решение за сервером (consumers.py).
var pendingPdf = null;
var pdfUploading = false;

function pdfUiText(key, fallback, value) {
    var text = getUiString(key, fallback);
    return (value === undefined) ? text : String(text).replace('{0}', value);
}

function getFormCsrfToken() {
    var field = document.querySelector('#chatForm input[name="csrfmiddlewaretoken"]');
    if (field && field.value) return field.value;
    return (typeof getCsrfToken === 'function') ? getCsrfToken() : '';
}

function currentModelSupportsVision() {
    var select = document.getElementById('select');
    if (!select || select.selectedIndex < 0) return false;
    var option = select.options[select.selectedIndex];
    return !!(option && option.getAttribute('data-vision') === '1');
}

function setPdfStatus(text, isError) {
    var el = document.getElementById('pdfAttachStatus');
    if (!el) return;
    el.textContent = text || '';
    if (isError) el.classList.add('is-error');
    else el.classList.remove('is-error');
}

function clearPendingPdf(clearStatus) {
    pendingPdf = null;
    var clearBtn = document.getElementById('pdfClearBtn');
    if (clearBtn) clearBtn.hidden = true;
    var input = document.getElementById('pdfFileInput');
    if (input) input.value = '';
    if (clearStatus !== false) setPdfStatus('', false);
}

function updatePdfUi() {
    var wrap = document.getElementById('pdfAttach');
    if (!wrap) return;
    var supports = currentModelSupportsVision();
    wrap.hidden = !supports;
    var btn = document.getElementById('pdfAttachBtn');
    if (btn) btn.disabled = !supports || pdfUploading;
    if (!supports && pendingPdf) {
        clearPendingPdf(false);
        setPdfStatus(getUiString('pdfNoVision', ''), true);
    }
}

function pdfErrorMessage(code) {
    switch (code) {
        case 'pdf_requires_vision': return getUiString('pdfNoVision', '');
        case 'pdf_only': return getUiString('pdfOnly', '');
        case 'pdf_too_large': {
            var wrap = document.getElementById('pdfAttach');
            var maxMb = wrap ? wrap.getAttribute('data-max-mb') : '';
            return pdfUiText('pdfTooLarge', '', maxMb || '10');
        }
        case 'pdf_no_text': return getUiString('pdfNoText', '');
        case 'pdf_encrypted': return getUiString('pdfEncrypted', '');
        case 'pdf_active_content': return getUiString('pdfActiveContent', '');
        case 'session': return getUiString('sessionExpired', '');
        default: return getUiString('pdfBad', '');
    }
}

function uploadPdfFile(file) {
    if (!file || pdfUploading) return;
    if (!/\.pdf$/i.test(file.name)) {
        setPdfStatus(getUiString('pdfOnly', ''), true);
        return;
    }
    var wrap = document.getElementById('pdfAttach');
    var maxMb = parseInt(wrap ? wrap.getAttribute('data-max-mb') : '0', 10);
    if (maxMb > 0 && file.size > maxMb * 1024 * 1024) {
        setPdfStatus(pdfUiText('pdfTooLarge', '', maxMb), true);
        return;
    }
    var model = document.getElementById('select').value;
    if (!model) return;

    pdfUploading = true;
    updatePdfUi();
    setPdfStatus(getUiString('pdfUploading', ''), false);

    var form = new FormData();
    form.append('file', file);
    form.append('model', model);
    var headers = {};
    var token = getFormCsrfToken();
    if (token) headers['X-CSRFToken'] = token;

    fetch('/ai/api/pdf/', {
        method: 'POST',
        headers: headers,
        body: form,
        credentials: 'same-origin'
    }).then(function (response) {
        return response.text().then(function (text) {
            var data = null;
            try { data = JSON.parse(text); } catch (e) { data = null; }
            if (!data) {
                // HTML вместо JSON — протухшая сессия (редирект на логин).
                throw { code: (response.status === 401 || response.status === 403) ? 'session' : 'pdfBad' };
            }
            // _deny_access отдаёт {"error": ...} без code — это отказ доступа
            // (протухшая сессия), а не повреждённый PDF.
            if (!data.ok && !data.code) {
                data.code = (response.status === 401 || response.status === 403) ? 'session' : 'pdfBad';
            }
            return data;
        });
    }).then(function (data) {
        if (!data.ok) throw { code: data.code || 'pdfBad' };
        pendingPdf = {
            name: data.name,
            pages: data.pages,
            text: data.text,
            truncated: !!data.truncated
        };
        var clearBtn = document.getElementById('pdfClearBtn');
        if (clearBtn) clearBtn.hidden = false;
        setPdfStatus(pdfUiText('pdfReady', '', data.name), false);
    }).catch(function (error) {
        pendingPdf = null;
        var clearBtn = document.getElementById('pdfClearBtn');
        if (clearBtn) clearBtn.hidden = true;
        setPdfStatus(pdfErrorMessage(error && error.code), true);
    }).then(function () {
        pdfUploading = false;
        updatePdfUi();
    });
}

function initPdfAttach() {
    var wrap = document.getElementById('pdfAttach');
    if (!wrap) return;
    var input = document.getElementById('pdfFileInput');
    var attachBtn = document.getElementById('pdfAttachBtn');
    var clearBtn = document.getElementById('pdfClearBtn');

    if (attachBtn && input) {
        attachBtn.addEventListener('click', function () {
            if (attachBtn.disabled) return;
            input.click();
        });
    }
    if (input) {
        input.addEventListener('change', function () {
            var file = input.files && input.files[0];
            if (file) uploadPdfFile(file);
            input.value = '';
        });
    }
    if (clearBtn) {
        clearBtn.addEventListener('click', function () { clearPendingPdf(true); });
    }
    var select = document.getElementById('select');
    if (select) select.addEventListener('change', updatePdfUi);
    updatePdfUi();
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
    var hasPdf = !!pendingPdf;
    if (pdfUploading) { updateVoiceStatus(getUiString('pdfUploading', "Загрузка PDF…")); return; }
    if (!input.value.trim() && !hasPdf) return;

    var payload = {
        type: '1',
        message: input.value,
        value: value,
        language: language,
        think: aiThinkEnabled()
    };
    if (hasPdf) payload.pdf = pendingPdf;
    ws.send(JSON.stringify(payload));
    setRequestLock(true);
    notEnter = true;
    updateVoiceStatus(getVoiceStatusText('messageSent'));
    input.value = '';
    if (hasPdf) clearPendingPdf(true);
    saveSharedText();
}

function sendMessage(event) {
    event.preventDefault();
    if (!ws) { alert(getUiString('notConnected', "Соединение не установлено. Пожалуйста, подождите...")); return; }
    if (ws.readyState !== WebSocket.OPEN) { alert(getUiString('notConnected', "Соединение не установлено. Пожалуйста, подождите...")); return; }
    if (requestInFlight) { alert(getUiString('waitResponse', "Дождитесь ответа модели перед новым запросом.")); return; }

    var value = document.querySelector("#select").value;
    var language = document.querySelector("#selectLang").value;
    var input = document.getElementById("messageText");
    var hasPdf = !!pendingPdf;
    if (pdfUploading) { alert(getUiString('pdfUploading', "Загрузка PDF…")); return; }
    if (!value) { alert(getUiString('noModelsToday', "Сегодня нет доступных моделей. Повторите позже.")); return; }
    if (!input.value.trim() && !hasPdf) { alert(getUiString('enterMessage', "Пожалуйста, введите сообщение")); return; }
    if (hasPdf && !currentModelSupportsVision()) {
        alert(getUiString('pdfNoVision', "Эта модель не умеет работать с PDF. Выберите модель со зрением."));
        clearPendingPdf(false);
        updatePdfUi();
        return;
    }

    var payload = {
        type: '1',
        message: input.value,
        value: value,
        language: language,
        think: aiThinkEnabled()
    };
    if (hasPdf) payload.pdf = pendingPdf;
    ws.send(JSON.stringify(payload));
    setRequestLock(true);
    notEnter = true;
    input.value = '';
    if (hasPdf) clearPendingPdf(true);
    saveSharedText();
}

// === selectLang change handler — обновляет UI на странице чата ===
document.getElementById("selectLang").addEventListener("change", function() {
    var selectedLang = this.options[this.selectedIndex].getAttribute("language");

    var submitBtn = document.querySelector("button[type='submit']");
    if (submitBtn) submitBtn.textContent = localization[selectedLang].send;

    var clearBtn = document.querySelector("#clearContextBtn");
    if (clearBtn) clearBtn.textContent = localization[selectedLang].clear;

    var messageText = document.getElementById("messageText");
    if (messageText) messageText.setAttribute("placeholder", localization[selectedLang].placeholder);

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

    // PDF-кнопки: подписи и title при смене языка; статус приложенного файла
    // перерисовываем из pendingPdf (имя файла не зависит от языка).
    var pdfAttachBtn = document.getElementById("pdfAttachBtn");
    if (pdfAttachBtn) {
        pdfAttachBtn.textContent = "📎 " + localization[selectedLang].pdfAttach;
        pdfAttachBtn.title = localization[selectedLang].pdfAttachTitle;
        pdfAttachBtn.setAttribute("aria-label", localization[selectedLang].pdfAttachTitle);
    }
    var pdfClearBtn = document.getElementById("pdfClearBtn");
    if (pdfClearBtn) {
        pdfClearBtn.title = localization[selectedLang].pdfRemove;
        pdfClearBtn.setAttribute("aria-label", localization[selectedLang].pdfRemove);
    }
    if (pendingPdf) setPdfStatus(pdfUiText('pdfReady', '', pendingPdf.name), false);

    saveInterfaceLanguage();
    updateAccordionLabels();
    updateLastUpdateLabel();
    updateVoiceStatus(getVoiceStatusText('readyForVoice'));
});

// === window.onload — инициализация страницы чата ===
window.onload = function() {
    console.log('Initializing WebSocket with client_id:', client_id);
    restoreInterfaceLanguage();
    restoreSelections();
    initWebSocket();
    restorePersistedMessages();
    document.getElementById("selectLang").dispatchEvent(new Event("change"));
    initAccordionForMessages();
    restoreSharedText();
    updateVoiceStatus(getVoiceStatusText('ready'));
    initSelectionPersistence();
    initModelLimitsWidget();
    // PDF: после restoreSelections — видимость кнопки зависит от модели.
    initPdfAttach();

    var speakThinkCheckbox = document.getElementById('speakThinkContent');
    if (speakThinkCheckbox) {
        speakThinkCheckbox.addEventListener('change', function() {
            speakThinkEnabled = this.checked;
        });
    }

    var messageText = document.getElementById('messageText');
    if (messageText) messageText.addEventListener('input', saveSharedText);

    // Автозагрузка условия задачи из DL-ссылки (?nid=…): кнопка «Чат с DLAI»
    // на странице задачи ведёт сюда. Раньше nid игнорировался; теперь, если
    // со стороны dl.gsu.by он передан — подставляем условие в поле сообщения
    // (после restoreSharedText и только при пустом/восстановленном поле,
    // см. ai-common.js::aiLoadTaskFromUrl).
    if (window.AI_TASK_NODE_ID) {
        aiLoadTaskFromUrl(document.getElementById('messageText'), { force: true });
    }
};