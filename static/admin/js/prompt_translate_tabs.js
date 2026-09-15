/* Языковые табы RU/EN/FR в формах админки (PromptAdmin / SharedPromptAdmin /
 * TopicAdmin).
 *
 * Вместо громоздких 3 полей названия + 3 textarea рендерим 1 название +
 * 1 textarea с табами языков. При переключении на таб с пустыми полями
 * содержимое авто-переводится с заполненного языка (AJAX →
 * /ai/admin/ai/prompt/translate-field/ → deep-translator/Google Translate,
 * тот же сервис, что у массового автоперевода). Кнопка «Перевести» в табе
 * перезаполняет поля принудительно.
 *
 * Реальные input'ы формы не трогаем (никаких скрытых клонов) — табы только
 * показывают/прячут строки .form-row, поэтому сохранение работает как раньше.
 * Конфиг полей определяется по наличию id_*_ru в форме (prompt_name_ru →
 * Prompt/SharedPrompt, topic_name_ru → Topic).
 */
(function () {
    'use strict';

    var TRANSLATE_URL = "/ai/admin/ai/prompt/translate-field/";

    // Наборы полей по типу формы. Проверяются по порядку; namePrefix обязателен,
    // textPrefix может отсутствовать (у Topic есть только название).
    var VARIANTS = [
        { namePrefix: "prompt_name", textPrefix: "prompt_text" },
        { namePrefix: "topic_name", textPrefix: null }
    ];

    var LANGS = [
        { key: "ru", label: "Русский" },
        { key: "en", label: "English" },
        { key: "fr", label: "Français" }
    ];

    var byId = function (id) { return document.getElementById(id); };

    var tabs, messageEl, currentLang = "ru", busy = false, variant = null;

    function langByKey(key) {
        for (var i = 0; i < LANGS.length; i++) {
            if (LANGS[i].key === key) return LANGS[i];
        }
        return null;
    }

    function fieldId(lang, kind) {
        var prefix = kind === "name" ? variant.namePrefix : variant.textPrefix;
        if (!prefix) return null;
        return "id_" + prefix + "_" + lang.key;
    }

    function inputOf(lang, kind) {
        var id = fieldId(lang, kind);
        return id ? byId(id) : null;
    }

    function rowOf(input) {
        return input ? input.closest(".form-row") : null;
    }

    function isEditable(input) {
        return input && !input.readOnly && !input.disabled;
    }

    function showMessage(text, isError) {
        if (!messageEl) return;
        messageEl.textContent = text || "";
        messageEl.classList.toggle("ai-lang-msg-error", !!isError);
    }

    function csrfToken() {
        var input = document.querySelector("#prompt_form input[name=csrfmiddlewaretoken]")
            || document.querySelector("input[name=csrfmiddlewaretoken]");
        return input ? input.value : "";
    }

    /* Значение поля-источника для перевода: приоритет RU → EN → FR. */
    function findSource(kind, targetKey) {
        var order = ["ru", "en", "fr"];
        var i, lang, value;
        for (i = 0; i < order.length; i++) {
            if (order[i] === targetKey) continue;
            lang = langByKey(order[i]);
            var input = inputOf(lang, kind);
            if (!input) continue;
            value = input.value || "";
            if (value.trim()) return { key: order[i], text: value };
        }
        return null;
    }

    function translateField(targetKey, sourceText) {
        var data = new FormData();
        data.append("text", sourceText);
        data.append("target", targetKey);
        return fetch(TRANSLATE_URL, {
            method: "POST",
            credentials: "same-origin",
            headers: { "X-CSRFToken": csrfToken() },
            body: data
        }).then(function (response) {
            return response.json().then(function (payload) {
                if (!response.ok || !payload.success) {
                    throw new Error(payload.error || "Сервис перевода недоступен");
                }
                return payload.text;
            });
        });
    }

    /* Перевод полей таба (название + текст). overwrite=true — даже если
       поля заполнены; иначе только пустые. */
    function fillLang(key, overwrite) {
        if (busy) return Promise.resolve();
        var lang = langByKey(key);
        var nameInput = inputOf(lang, "name");
        var textInput = inputOf(lang, "text");
        var jobs = [];

        [["name", nameInput], ["text", textInput]].forEach(function (pair) {
            var kind = pair[0], input = pair[1];
            if (!input) return;
            if (!overwrite && input.value.trim()) return;
            var source = findSource(kind, key);
            if (!source) return;
            jobs.push(
                translateField(kind, key, source.text)
                    .then(function (translated) { input.value = translated; })
            );
        });

        if (!jobs.length) return Promise.resolve();
        busy = true;
        showMessage("Переводим…", false);
        [nameInput, textInput].forEach(function (input) {
            if (input) { input.disabled = true; }
        });
        return Promise.all(jobs)
            .then(function () { showMessage(""); })
            .catch(function (error) {
                showMessage(error && error.message ? error.message : "Не удалось перевести", true);
            })
            .then(function () {
                [nameInput, textInput].forEach(function (input) {
                    if (input) { input.disabled = false; }
                });
                busy = false;
            });
    }

    function selectLang(key) {
        currentLang = key;
        var lang = langByKey(key);
        LANGS.forEach(function (item) {
            var show = item.key === key;
            ["name", "text"].forEach(function (kind) {
                var row = rowOf(inputOf(item, kind));
                if (row) row.hidden = !show;
            });
        });
        tabs.forEach(function (btn) {
            var active = btn.getAttribute("data-lang") === key;
            btn.classList.toggle("ai-lang-tab-active", active);
            btn.setAttribute("aria-selected", active ? "true" : "false");
        });
        // Пустой таб авто-переводится с заполненного (обе стороны таба —
        // если хоть одно поле пусто, дозаполняем только пустые).
        var nameInput = inputOf(lang, "name");
        var textInput = inputOf(lang, "text");
        var needsFill = [nameInput, textInput].some(function (input) {
            return input && !input.value.trim();
        });
        if (needsFill && isEditable(nameInput) && isEditable(textInput)) {
            fillLang(key, false);
        }
        showMessage("");
    }

    function init() {
        // Определяем тип формы по наличию {prefix}_ru в DOM.
        for (var i = 0; i < VARIANTS.length; i++) {
            if (byId("id_" + VARIANTS[i].namePrefix + "_ru")) {
                variant = VARIANTS[i];
                break;
            }
        }
        if (!variant) return;

        var firstInput = byId("id_" + variant.namePrefix + "_ru");
        var firstRow = rowOf(firstInput);
        if (!firstRow) return;
        // Все поля варианта должны быть в форме (у read-only форм они тоже
        // есть, но там поля readonly — табы показываем, кнопку перевода не даём).
        var missing = LANGS.some(function (lang) {
            if (!inputOf(lang, "name")) return true;
            return variant.textPrefix && !inputOf(lang, "text");
        });
        if (missing) return;

        var editable = isEditable(firstInput);

        var bar = document.createElement("div");
        bar.className = "ai-lang-tabs";

        tabs = LANGS.map(function (lang) {
            var btn = document.createElement("button");
            btn.type = "button";
            btn.className = "ai-lang-tab";
            btn.setAttribute("data-lang", lang.key);
            btn.textContent = lang.label;
            btn.addEventListener("click", function () { selectLang(lang.key); });
            bar.appendChild(btn);
            return btn;
        });

        if (editable) {
            var translateBtn = document.createElement("button");
            translateBtn.type = "button";
            translateBtn.className = "ai-lang-translate";
            translateBtn.textContent = "Перевести заново";
            translateBtn.title = "Переперевести поля этого языка с заполненного";
            translateBtn.addEventListener("click", function () { fillLang(currentLang, true); });
            bar.appendChild(translateBtn);
        }

        messageEl = document.createElement("span");
        messageEl.className = "ai-lang-msg";
        bar.appendChild(messageEl);

        firstRow.parentNode.insertBefore(bar, firstRow);

        selectLang("ru");
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();