/* Языковые табы RU/EN/FR в форме препромпта (PromptAdmin).
 *
 * Вместо громоздких 3 полей названия + 3 textarea рендерим 1 название +
 * 1 textarea с табами языков. При переключении на таб с пустыми полями
 * содержимое авто-переводится с заполненного языка (AJAX →
 * /ai/admin/ai/prompt/translate-field/ → deep-translator/Google Translate,
 * тот же сервис, что у массового автоперевода). Кнопка «Перевести» в табе
 * перезаполняет поля принудительно. Базовые поля (prompt_name / prompt_text,
 * fallback для старых записей) лежат в свёрнутом блоке; если RU-таб пуст, а
 * базовое поле заполнено, значение копируется в RU-таб (видимо для юзера).
 *
 * Реальные input'ы формы не трогаем (никаких скрытых клонов) — табы только
 * показывают/прячут строки .form-row, поэтому сохранение работает как раньше.
 */
(function () {
    'use strict';

    var TRANSLATE_URL = "/ai/admin/ai/prompt/translate-field/";

    var LANGS = [
        { key: "ru", label: "Русский",  nameField: "prompt_name_ru", textField: "prompt_text_ru" },
        { key: "en", label: "English",  nameField: "prompt_name_en", textField: "prompt_text_en" },
        { key: "fr", label: "Français", nameField: "prompt_name_fr", textField: "prompt_text_fr" }
    ];

    // Базовые (fallback) поля: RU-таб при пустых *_ru подхватывает из них.
    var BASE_FIELDS = { name: "prompt_name", text: "prompt_text" };

    var byId = function (id) { return document.getElementById(id); };

    var tabs, messageEl, currentLang = "ru", busy = false;

    function langByKey(key) {
        for (var i = 0; i < LANGS.length; i++) {
            if (LANGS[i].key === key) return LANGS[i];
        }
        return null;
    }

    function inputOf(fieldName) {
        var node = byId("id_" + fieldName);
        return node || null;
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

    /* Значение поля-источника для перевода: приоритет RU → EN → FR → base. */
    function findSource(kind, targetKey) {
        var order = ["ru", "en", "fr"];
        var i, lang, value;
        for (i = 0; i < order.length; i++) {
            if (order[i] === targetKey) continue;
            lang = langByKey(order[i]);
            value = (byId("id_" + (kind === "name" ? lang.nameField : lang.textField)) || {}).value || "";
            if (value.trim()) return { key: order[i], text: value };
        }
        var baseInput = byId("id_" + (kind === "name" ? BASE_FIELDS.name : BASE_FIELDS.text));
        if (baseInput && baseInput.value.trim()) return { key: "base", text: baseInput.value };
        return null;
    }

    function translateField(kind, targetKey, sourceText) {
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

    /* Перевод обоих полей таба (название + текст). overwrite=true — даже если
       поля заполнены; иначе только пустые. */
    function fillLang(key, overwrite) {
        if (busy) return Promise.resolve();
        var lang = langByKey(key);
        var nameInput = byId("id_" + lang.nameField);
        var textInput = byId("id_" + lang.textField);
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

    /* RU-таб пуст, а базовые поля заполнены → видимая копия base → *_ru.
       Это не меняет данные до сохранения формы и совпадает с фактическим
       fallback-поведением get_effective_text. */
    function prefillRuFromBase() {
        var lang = langByKey("ru");
        [["name", lang.nameField, BASE_FIELDS.name], ["text", lang.textField, BASE_FIELDS.text]]
            .forEach(function (triple) {
                var localized = byId("id_" + triple[1]);
                var base = byId("id_" + triple[2]);
                if (localized && base && !localized.value.trim() && base.value.trim()) {
                    localized.value = base.value;
                }
            });
    }

    function selectLang(key) {
        currentLang = key;
        var lang = langByKey(key);
        LANGS.forEach(function (item) {
            var show = item.key === key;
            [item.nameField, item.textField].forEach(function (fieldName) {
                var row = rowOf(inputOf(fieldName));
                if (row) row.hidden = !show;
            });
        });
        tabs.forEach(function (btn) {
            var active = btn.getAttribute("data-lang") === key;
            btn.classList.toggle("ai-lang-tab-active", active);
            btn.setAttribute("aria-selected", active ? "true" : "false");
        });
        if (key === "ru") prefillRuFromBase();
        // Пустой таб авто-переводится с заполненного (обе стороны таба —
        // если хоть одно поле пусто, дозаполняем только пустые).
        var nameInput = byId("id_" + lang.nameField);
        var textInput = byId("id_" + lang.textField);
        var needsFill = [nameInput, textInput].some(function (input) {
            return input && !input.value.trim();
        });
        if (needsFill && isEditable(nameInput) && isEditable(textInput)) {
            fillLang(key, false);
        }
        showMessage("");
    }

    function init() {
        var firstInput = byId("id_prompt_name_ru");
        var firstRow = rowOf(firstInput);
        if (!firstRow) return; // не форма препромпта
        // Все шесть полей должны быть в форме (у read-only форм они тоже есть,
        // но там поля readonly — табы показываем, кнопку перевода не даём).
        var missing = LANGS.some(function (lang) {
            return !byId("id_" + lang.nameField) || !byId("id_" + lang.textField);
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