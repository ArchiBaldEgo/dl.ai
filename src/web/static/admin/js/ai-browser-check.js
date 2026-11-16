/* ==========================================================================
   ai-browser-check.js — предупреждение об устаревшем браузере.

   ПОЧЕМУ ОТДЕЛЬНЫЙ ФАЙЛ И ЧИСТЫЙ ES5:
   приложение (ai-common.js, chat_template.js, decide-task/find-error) уже
   использует async/await, стрелочные функции, const/let и шаблонные строки.
   Браузер, который их не понимает, получает SyntaxError при загрузке
   ai-common.js и не выполняет НИ ОДНОГО скрипта страницы — то есть проверка,
   лежащая в ai-common.js, там просто не запустится. Поэтому:
     * файл обязан быть синтаксически понятен даже IE11 (var, function,
       никаких стрелок/шаблонов/let/const/async);
     * никаких зависимостей от ai-common.js (getUiString и т.п.) — свои
       трёхъязычные тексты и свой разбор языка UI.
   Подключается с defer в <head> ai/templates/ai/base_chat.html.

   Что проверяется:
     * возможности движка (Promise/fetch/WebSocket/localStorage/closest/
       Array.from/Object.assign/async-функции) — объективный критерий;
     * версия движка по User-Agent — чтобы сказать «Chromium 72 устарел,
       нужна версия 80+» (Chromium Portal и прочие старые сборки Chromium
       часто отстают на годы, хотя выглядят как обычный Chrome);
     * IE и EdgeHTML — не поддерживаются вовсе.
   Баннер закрывается, отметка — в localStorage; повторно не показывается.
   ========================================================================== */
(function () {
    'use strict';

    var DISMISS_KEY = 'ai-browser-warning-dismissed';
    var BANNER_ID = 'ai-browser-warning';

    // Минимальные версии: ниже — предупреждаем. Порог с запасом от реально
    // используемых возможностей (async/await — Chrome 55, Firefox 52,
    // Safari 10.1; Element.closest — Chrome 41; scrollTo(options) — Chrome 61,
    // Safari 14), чтобы не пугать владельцев ещё рабочих сборок.
    var MINS = { Chrome: 80, Chromium: 80, Edge: 80, Opera: 67, Yandex: 20,
                 Firefox: 78, Safari: 14 };

    var TEXTS = {
        Russian: {
            dismiss: 'Закрыть',
            outdated: 'Ваш браузер%NAME% устарел — приложение может работать некорректно. ' +
                      'Обновите его%TO% или установите современный браузер (Chrome, Firefox, Edge).',
            unsupported: 'Ваш браузер не поддерживается — приложение может работать некорректно. ' +
                         'Установите современный браузер (Chrome, Firefox, Edge).',
            version: ' (%VERSION%)',
            to: ' до версии %MIN% или новее'
        },
        English: {
            dismiss: 'Dismiss',
            outdated: 'Your browser%NAME% is outdated — the app may not work correctly. ' +
                      'Please update it%TO% or install a modern browser (Chrome, Firefox, Edge).',
            unsupported: 'Your browser is not supported — the app may not work correctly. ' +
                         'Please install a modern browser (Chrome, Firefox, Edge).',
            version: ' (%VERSION%)',
            to: ' to version %MIN% or newer'
        },
        French: {
            dismiss: 'Fermer',
            outdated: 'Votre navigateur%NAME% est obsolète — l\'application peut ne pas fonctionner correctement. ' +
                      'Mettez-le à jour%TO% ou installez un navigateur moderne (Chrome, Firefox, Edge).',
            unsupported: 'Votre navigateur n\'est pas pris en charge — l\'application peut ne pas fonctionner correctement. ' +
                         'Installez un navigateur moderne (Chrome, Firefox, Edge).',
            version: ' (%VERSION%)',
            to: ' vers la version %MIN% ou plus récente'
        }
    };

    function format(template, values) {
        var out = template;
        for (var key in values) {
            if (Object.prototype.hasOwnProperty.call(values, key)) {
                out = out.replace(new RegExp('%' + key + '%', 'g'), values[key]);
            }
        }
        return out;
    }

    /** Язык UI: селект #selectLang (его рендерит сервер), иначе кука ai_state,
     *  иначе русский (дефолт клиентской части). */
    function uiLanguage() {
        var select = document.getElementById('selectLang');
        if (select && select.options && select.selectedIndex >= 0) {
            var attr = select.options[select.selectedIndex].getAttribute('language');
            if (attr && TEXTS[attr]) {
                return attr;
            }
        }
        try {
            var match = /(?:^|;\s*)ai_state=([^;]*)/.exec(document.cookie || '');
            if (match) {
                var state = JSON.parse(decodeURIComponent(match[1]));
                if (state && state.lang && TEXTS[state.lang]) {
                    return state.lang;
                }
            }
        } catch (e) { /* битая кука — русский */ }
        return 'Russian';
    }

    /** Движок и версия по UA; null — неизвестный браузер (решает проверка
     *  возможностей). Порядок веток важен: Edge/Opera/Yandex/Chromium-сборки
     *  подставляют в UA и «Chrome/», и «Safari/». */
    function detectEngine() {
        var ua = navigator.userAgent || '';
        var match;

        match = /Edg\/(\d+)/.exec(ua);
        if (match) {
            return { family: 'Edge', version: parseInt(match[1], 10) };
        }
        match = /Edge\/(\d+)/.exec(ua);
        if (match) {
            return { family: 'EdgeHTML', version: parseInt(match[1], 10), unsupported: true };
        }
        match = /OPR\/(\d+)/.exec(ua) || /Opera\/(\d+)/.exec(ua);
        if (match) {
            return { family: 'Opera', version: parseInt(match[1], 10) };
        }
        match = /YaBrowser\/(\d+)/.exec(ua);
        if (match) {
            return { family: 'Yandex', version: parseInt(match[1], 10) };
        }
        match = /Chromium\/(\d+)/.exec(ua);
        if (match) {
            return { family: 'Chromium', version: parseInt(match[1], 10) };
        }
        match = /Chrome\/(\d+)/.exec(ua) || /CriOS\/(\d+)/.exec(ua);
        if (match) {
            return { family: 'Chrome', version: parseInt(match[1], 10) };
        }
        match = /Firefox\/(\d+)/.exec(ua) || /FxiOS\/(\d+)/.exec(ua);
        if (match) {
            return { family: 'Firefox', version: parseInt(match[1], 10) };
        }
        match = /Version\/(\d+)(?:\.\d+)?.*Safari\//.exec(ua);
        if (match) {
            return { family: 'Safari', version: parseInt(match[1], 10) };
        }
        if (/MSIE |Trident\//.test(ua)) {
            return { family: 'IE', version: 0, unsupported: true };
        }
        return null;
    }

    /** Возможности, реально нужные странице. async-функция проверяется
     *  через new Function: старый движок бросит SyntaxError только на этой
     *  строке, файл продолжит выполняться. */
    function featuresOk() {
        try {
            if (!window.Promise || !window.fetch || !window.WebSocket ||
                !window.localStorage || !window.JSON) {
                return false;
            }
            if (!window.Element || !Element.prototype || !Element.prototype.closest) {
                return false;
            }
            if (!Array.from || !Object.assign) {
                return false;
            }
            new Function('return async function () {};');
            return true;
        } catch (e) {
            return false;
        }
    }

    function isDismissed() {
        try {
            return window.localStorage.getItem(DISMISS_KEY) === '1';
        } catch (e) {
            return false;   // localStorage недоступен (приватный режим/политика)
        }
    }

    function dismiss() {
        try {
            window.localStorage.setItem(DISMISS_KEY, '1');
        } catch (e) { /* не запомнили — покажем в следующий раз */ }
        var banner = document.getElementById(BANNER_ID);
        if (banner && banner.parentNode) {
            banner.parentNode.removeChild(banner);
        }
        document.documentElement.className =
            document.documentElement.className
                .replace(/(^|\s)ai-browser-warning-visible(\s|$)/, '$1')
                .replace(/\s+/g, ' ');
    }

    /** Высота баннера зависит от переноса текста на узких экранах: #content
     *  сжимается на неё через CSS-переменную (страницы чата — height:100vh). */
    function syncHeight(banner) {
        var root = document.documentElement;
        if (root.style && root.style.setProperty) {
            root.style.setProperty('--ai-browser-warning-h', banner.offsetHeight + 'px');
        }
    }

    function showWarning() {
        var lang = uiLanguage();
        var texts = TEXTS[lang] || TEXTS.Russian;
        var engine = detectEngine();
        var min = engine ? MINS[engine.family] : 0;
        var outdated = engine
            ? (engine.unsupported || (min && engine.version < min))
            : !featuresOk();
        // Известный браузер, но возможностей не хватает (старая сборка с
        // подменённым UA) — тоже предупреждаем.
        if (!outdated && !featuresOk()) {
            outdated = true;
        }
        if (!outdated || isDismissed()) {
            return;
        }

        var text;
        if (engine && !engine.unsupported && min && engine.version < min) {
            text = format(texts.outdated, {
                NAME: format(texts.version, { VERSION: engine.family + ' ' + engine.version }),
                TO: format(texts.to, { MIN: min })
            });
        } else if (engine && engine.version && !engine.unsupported) {
            text = format(texts.outdated, {
                NAME: format(texts.version, { VERSION: engine.family + ' ' + engine.version }),
                TO: ''
            });
        } else {
            text = texts.unsupported;
        }

        var banner = document.createElement('div');
        banner.id = BANNER_ID;
        banner.className = 'ai-browser-warning';
        banner.setAttribute('role', 'alert');

        var label = document.createElement('span');
        label.className = 'ai-browser-warning__text';
        label.appendChild(document.createTextNode(text));
        banner.appendChild(label);

        var button = document.createElement('button');
        button.type = 'button';
        button.className = 'ai-browser-warning__close';
        button.setAttribute('aria-label', texts.dismiss);
        button.setAttribute('title', texts.dismiss);
        button.appendChild(document.createTextNode('×'));
        button.onclick = dismiss;
        banner.appendChild(button);

        document.body.appendChild(banner);
        document.documentElement.className += ' ai-browser-warning-visible';
        syncHeight(banner);
        // addEventListener, а не window.onresize: страничные скрипты могут
        // вешать свой обработчик, перезапись чужого их сломает.
        window.addEventListener('resize', function () { syncHeight(banner); });
        // Шрифты догружаются после DOMContentLoaded и на узком экране меняют
        // перенос строк — перезамеряем после полной загрузки.
        window.addEventListener('load', function () { syncHeight(banner); });
    }

    function ready(callback) {
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', callback);
        } else {
            callback();
        }
    }

    ready(function () {
        try {
            showWarning();
        } catch (e) { /* предупреждение не должно ломать страницу */ }
    });
})();
