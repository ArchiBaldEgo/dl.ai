'use strict';

/**
 * Описание сайта Web Kimi (www.kimi.ai).
 *
 * Отличия от DeepSeek (всё, что было в WebKimi/worker/modules/*):
 * - вход НЕ автоматизируется (телефон/SMS, WeChat QR, email) — сессия
 *   сидируется вручную один раз (WebKimi/seed.js) в постоянный Chrome-профиль;
 * - поле ввода — contenteditable div (НЕ textarea); с октября 2026 это
 *   Lexical-редактор (div.chat-input-editor[role=textbox]): paste-инжект
 *   он игнорирует, текст вводится настоящими клавишами (keyboard.type);
 * - отправка — кнопка div.send-button-container (fallback — Enter);
 * - ответ лежит в div.segment-content-box (текст + блоки кода), размышления и
 *   вызовы инструментов — в блоках класса toolcall-* (вырезаются из снимка);
 * - ответ достаётся класс-независимым robust-fallback (см. worker/modules/promtps.js);
 * - гвард «stale answer»: ответ, совпадающий с пузырём ДО отправки, отбраковывается.
 *
 * Общий алгоритм — в worker/modules/promtps.js (одна копия на оба пула).
 */

const fs = require('fs');
const path = require('path');

const {
    clickIfExists,
    elementExists,
} = require('../worker/core/page-utils');
const {
    waitForXPathCompat,
    htmlToMarkdown,
    extractCodeFromPreInner,
    getRobustLastAnswerData,
} = require('../worker/modules/promtps');
const { log, error } = require('../worker/utils/logger');
const { sleep } = require('../worker/utils/helpers');
const { toBool } = require('../worker/utils/env');

const REPO_ROOT = path.resolve(__dirname, '..', '..');

/**
 * URL'ы и XPath'ы сайта (бывший WebKimi/worker/data.json).
 */
const data = {
    "services": {
        "kimi": "https://www.kimi.ai/"
    },
    "loginUrls": {
        "kimi": "https://www.kimi.ai/"
    },
    "_live_inspection_2026_10": "Живая инспекция Kimi (октябрь 2026) — Kimi переделал чат. Поле ввода: div.chat-input-editor[role=textbox] — ТЕПЕРЬ это Lexical-редактор (data-lexical-editor=true, внутри span[data-lexical-text]); paste-инжект больше НЕ работает (событие paste игнорируется) — ввод только настоящими клавишами (keyboard.type). Enter = ОТПРАВКА, Shift+Enter = перенос строки, поэтому многострочный промпт вводится через Shift+Enter. Kimi ВОССТАНАВЛИВАЕТ неотправленный черновик из прошлого запроса — перед вводом редактор чистим (Ctrl+A, Backspace). Кнопка отправки: div.send-button-container (36x36, при генерации та же кнопка получает класс 'stop' — индикатор «генерация идёт»). Ответ: div.chat-content-item.chat-content-item-assistant > (Segment) > div.segment-content-box, внутри — блоки div.markdown (текст) и div.segment-code (КОД: <pre class='language-x'><code class='language-x'> с токенами ПОДСВЕТКИ highlight.js внутри — теги надо срезать, иначе в «код» попадает разметка). Размышления/вызовы инструментов — блоки класса toolcall-* (toolcall-container thinking-container); прежний класс toolcall-content-text больше не встречается. Старый селектор по div.markdown брал только ПОСЛЕДНИЙ markdown-блок (часто — один лишь блок кода, а текст ответа терялся), поэтому ответ берём целиком из div.segment-content-box. У контейнера ответов класс chat-content-list (раньше был virtuoso). ВАЖНО: поле ввода показывается и ГОСТЮ (сессии нет) — у гостя в шапке .not-login-icon и .login-button, а по отправке всплывает модалка входа; поэтому checkAlreadyAuthorized проверяет маркеры гостя, а не только наличие редактора. Локальный профиль WebKimi/worker/.chrome-profiles/bot-1 на момент проверки был РАЗЛОГИНЕН (сессия истекла) — живая проверка ответа невозможна, селекторы ответа взяты из бандла kimi-web-seo (официальные классы Vue-приложения).",
    "xpaths": {
        "auth": {
            "chatUrl": {
                "kimi": "https://www.kimi.ai/"
            }
        },
        "chat": {
            "inputLabel": {
                "kimi": "//div[contains(@class,'chat-input-editor') and @role='textbox']"
            },
            "inputLabelCss": {
                "kimi": ".chat-input-editor[role='textbox']"
            },
            "sendMessageButton": {
                "kimi": "//div[contains(@class,'send-button-container') and not(contains(@class,'disabled')) and not(contains(@class,'stop'))]"
            },
            "answer": {
                "kimi": "//div[contains(@class,'chat-content-item-assistant')][last()]//div[contains(@class,'segment-content-box')][last()]"
            },
            "fullAnswer": {
                "kimi": "//div[contains(@class,'chat-content-list')]"
            }
        }
    }
};

const LOG_DIR = path.join(REPO_ROOT, 'WebKimi', 'worker', 'logs');

function ensureLogDir() {
    if (!fs.existsSync(LOG_DIR)) fs.mkdirSync(LOG_DIR, { recursive: true });
}

function isAuthDebugEnabled() {
    return toBool(process.env.KIMI_AUTH_DEBUG, false);
}

function sanitizeLabel(label) {
    return String(label || 'debug').replace(/[^a-z0-9_-]+/gi, '_');
}

async function writeAuthDebugArtifacts(page, label) {
    if (!isAuthDebugEnabled() || !page) return;

    ensureLogDir();
    const ts = new Date().toISOString().replace(/[:.]/g, '-');
    const safeLabel = sanitizeLabel(label);
    const base = `auth-${safeLabel}-${ts}`;
    const htmlPath = path.join(LOG_DIR, `${base}.html`);
    const shotPath = path.join(LOG_DIR, `${base}.png`);

    try {
        const html = await page.content();
        fs.writeFileSync(htmlPath, html);
    } catch (e) {
        error(`[auth] debug html write failed: ${e?.message || e}`);
    }

    try {
        await page.screenshot({ path: shotPath, fullPage: true });
    } catch (e) {
        error(`[auth] debug screenshot failed: ${e?.message || e}`);
    }

    error(`[auth] debug artifacts saved: ${htmlPath} ${shotPath}`);
}

function getTimeoutMs() {
    const raw = Number(process.env.KIMI_AUTH_TIMEOUT_MS || 45000);
    return Number.isFinite(raw) && raw > 0 ? raw : 45000;
}

/**
 * Быстрый путь: если в постоянном профиле уже есть живая сессия, то после
 * открытия kimi.ai сразу появится поле ввода чата — логин не нужен.
 *
 * ВАЖНО (живая проверка, октябрь 2026): поле ввода появляется и у ГОСТЯ —
 * Kimi показывает редактор всегда, а при отправке всплывает модалка входа.
 * Поэтому одного поля ввода мало: дополнительно проверяем маркеры гостя
 * (иконка «не залогинен», модалка входа). Иначе пул считал бы гостя
 * авторизованным и на каждом запросе падал бы с «селекторы не нашли ответ»
 * вместо честного «пересидируйте профиль».
 */
async function checkAlreadyAuthorized(ctx, payload = {}) {
    const page = ctx?.page;
    if (!page) return false;

    const currentService = payload.model;
    const chatUrl =
        data?.xpaths?.auth?.chatUrl?.[currentService] ||
        data?.services?.[currentService] ||
        'https://www.kimi.com/';
    const chatInputXPath = data?.xpaths?.chat?.inputLabel?.[currentService] ||
        "//div[contains(@class,'chat-input-editor') and @role='textbox']";

    try {
        await page.goto(chatUrl, { waitUntil: 'domcontentloaded', timeout: getTimeoutMs() });
        // Поле ввода чата означает, что страница чата открылась.
        await waitForXPathCompat(page, chatInputXPath, { timeout: 15000 });
        // …но гость тоже видит это поле — проверяем маркеры гостя.
        await sleep(1500);
        if (await hasGuestMarkers(page)) {
            log('Kimi: сессия не найдена — страница открыта как гость (нужен пересев профиля)');
            return false;
        }
        return true;
    } catch (_) {
        return false;
    }
}

/**
 * Признаки того, что Kimi открыт как гость (сессии нет).
 * Живая проверка: у гостя в шапке есть .not-login-icon (иконка «не залогинен»)
 * и .login-button («Log in»), плюс может висеть модалка входа.
 */
async function hasGuestMarkers(page) {
    try {
        return await page.evaluate(() => {
            const visible = (n) => {
                if (!n) return false;
                const r = n.getBoundingClientRect();
                return !!(r.width || r.height);
            };
            const guestSelectors = [
                '.not-login-icon',
                '.not-login-avatar-svg',
                '.login-modal-next-mask',
                '[class*="login-modal"]',
            ];
            for (const sel of guestSelectors) {
                for (const n of document.querySelectorAll(sel)) {
                    if (visible(n)) return true;
                }
            }
            return false;
        });
    } catch (_) {
        return false;
    }
}

/**
 * Авторизация Kimi НЕ автоматизируется: сайт принимает только телефон+SMS,
 * WeChat QR или email — форма логина/пароля для бота не подходит (SMS/QR
 * требуют ручного действия). Поэтому сессия сидируется ОДИН РАЗ вручную:
 *   1. запустить бот с KIMI_HEADLESS=false (нужен дисплей/X11/VNC);
 *   2. в окне Chromium войти в Kimi любым способом (телефон/WeChat/email);
 *   3. закрыть браузер — постоянный профиль (KIMI_BOT_PROFILE_DIR/bot-<id>)
 *      сохранит сессионную куку;
 *   4. дальнейшие запуски (даже headless) переиспользуют сессию через
 *      checkAlreadyAuthorized() — повторный логин не нужен.
 *
 * Это тот же обход, что задокументирован для DeepSeek Google-блокировки
 * (см. docs/web_deepseek_bot.md): ручной сид профиля вместо автоматизации.
 */
async function login(ctx, payload = {}) {
    // Автоматизация формы входа Kimi невозможна (телефон/SMS, WeChat QR, email
    // требуют ручного шага). Этот метод лишь диагностирует отсутствие сессии и
    // инструктирует оператора пересидировать постоянный профиль. bot.js
    // превратит {ok:false} в NotAuthorizedError (HTTP 401 not_autorized).
    const page = ctx?.page;

    try {
        await writeAuthDebugArtifacts(page, 'kimi_no_session');
    } catch (_) {}

    return {
        ok: false,
        reason:
            'сессия Kimi отсутствует/истекла — пересидируйте профиль: ' +
            'запустите бот с KIMI_HEADLESS=false (нужен дисплей/X11/VNC), ' +
            'войдите в Kimi вручную один раз, закройте браузер; ' +
            'постоянный профиль (KIMI_BOT_PROFILE_DIR) сохранит сессионную куку',
    };
}

/**
 * Ввод текста в редактор Kimi.
 *
 * ОКТЯБРЬ 2026: Kimi перевёл поле ввода на Lexical-редактор
 * (div.chat-input-editor[role=textbox], внутри span[data-lexical-text]).
 * Синтетическое событие 'paste' Lexical ИГНОРИРУЕТ — прежний paste-инжект
 * (ClipboardEvent + DataTransfer) текст больше НЕ вставляет (проверено живьём:
 * после paste в редакторе оставался прежний текст). Работает только ввод
 * настоящими клавишами (keyboard.type) — как это делает человек.
 *
 * Ещё два живых наблюдения:
 * - Kimi ВОССТАНАВЛИВАЕТ неотправленный черновик из прошлого запроса
 *   (проверено: в редакторе лежал текст прошлой пробы) — перед вводом
 *   редактор обязателен чистим, иначе промпт уйдёт с чужим хвостом;
 * - Enter в Kimi = ОТПРАВКА, Shift+Enter — перенос строки, поэтому
 *   многострочный промпт вводится через Shift+Enter.
 *
 * Возвращаем true ТОЛЬКО если текст реально попал в редактор.
 */
async function typeIntoContentEditable(page, cssSelector, text) {
    const txt = String(text ?? '');
    let element = null;
    try {
        element = await page.waitForSelector(cssSelector, { visible: true, timeout: 15000 });
        await sleep(300);
        await element.click();
        await element.focus();
        await sleep(200);

        // 0) Чистим редактор (черновик прошлого запроса).
        await clearEditor(page, cssSelector);

        // 1) Настоящие клавиши — единственный рабочий путь в Lexical-редакторе.
        await typeWithLineBreaks(page, txt);
        await sleep(300);
        let filled = await readEditorText(page, cssSelector);

        // 2) Fallback — paste-инжект (старая вёрстка Kimi / другие contenteditable).
        if (!filled) {
            log('typeIntoContentEditable: keyboard.type не заполнил поле, fallback на paste-инжект');
            const injected = await page.evaluate((sel, t) => {
                const el = document.querySelector(sel);
                if (!el) return false;
                el.focus();
                try {
                    const dt = new DataTransfer();
                    dt.setData('text/plain', t);
                    const ev = new ClipboardEvent('paste', {
                        clipboardData: dt,
                        bubbles: true,
                        cancelable: true,
                    });
                    el.dispatchEvent(ev);
                    // Некоторые редакторы слушают beforeinput/input.
                    el.dispatchEvent(new InputEvent('beforeinput', {
                        data: t, inputType: 'insertText', bubbles: true, cancelable: true,
                    }));
                    el.dispatchEvent(new InputEvent('input', {
                        data: t, inputType: 'insertText', bubbles: true,
                    }));
                    return (el.textContent || '').trim().length > 0;
                } catch (_) {
                    return false;
                }
            }, cssSelector, txt);
            if (injected) {
                await sleep(300);
                filled = await readEditorText(page, cssSelector);
            }
        }

        if (!filled) {
            error('typeIntoContentEditable: текст не попал в редактор Kimi');
            return false;
        }
        // Промпт должен реально лежать в редакторе — сверяем хвост текста.
        const tail = txt.trim().split('\n').filter(Boolean).pop() || '';
        if (tail && !filled.includes(tail.slice(-40))) {
            error('typeIntoContentEditable: в редакторе Kimi нет нашего текста (черновик/потеря ввода)');
            return false;
        }
        return true;
    } catch (err) {
        error(`typeIntoContentEditable failed: ${err?.message || err}`);
        return false;
    } finally {
        if (element) {
            try { await element.dispose(); } catch (_) {}
        }
    }
}

/**
 * Очистка редактора: Ctrl+A + Backspace (в сфокусированном Lexical-редакторе
 * выделяется только содержимое редактора, не страница).
 * Нужно, потому что Kimi восстанавливает неотправленный черновик.
 */
async function clearEditor(page, cssSelector) {
    for (let attempt = 0; attempt < 3; attempt++) {
        const before = await readEditorText(page, cssSelector);
        if (!before) return true;
        await page.keyboard.down('Control');
        try {
            await page.keyboard.press('KeyA');
        } finally {
            await page.keyboard.up('Control');
        }
        await page.keyboard.press('Backspace');
        await sleep(250);
        const after = await readEditorText(page, cssSelector);
        if (!after) return true;
    }
    const left = await readEditorText(page, cssSelector);
    if (left) {
        error('clearEditor: не удалось очистить редактор Kimi — осталось: ' + left.slice(0, 80));
    }
    return !left;
}

/** Текст, который реально лежит в редакторе Kimi (проверка ввода). */
async function readEditorText(page, cssSelector) {
    try {
        const text = await page.evaluate((sel) => {
            const el = document.querySelector(sel);
            return el ? (el.innerText || el.textContent || '').trim() : '';
        }, cssSelector);
        return String(text || '').trim();
    } catch (_) {
        return '';
    }
}

/**
 * Набор текста с корректными переносами строк.
 * В Kimi Enter отправляет сообщение, Shift+Enter — перенос строки, поэтому
 * '\n' вводим через Shift+Enter, а всё остальное — обычным keyboard.type.
 */
async function typeWithLineBreaks(page, text) {
    const normalized = String(text ?? '').replace(/\r\n/g, '\n').replace(/\r/g, '\n');
    const lines = normalized.split('\n');
    for (let i = 0; i < lines.length; i++) {
        if (i > 0) {
            await page.keyboard.down('Shift');
            try {
                await page.keyboard.press('Enter');
            } finally {
                await page.keyboard.up('Shift');
            }
        }
        if (lines[i]) {
            await page.keyboard.type(lines[i], { delay: 0 });
        }
    }
}

/**
 * Селекторы кнопки «Stop» (генерация ещё идёт) для Kimi.
 *
 * ОКТЯБРЬ 2026 (живая инспекция): у Kimi одна и та же кнопка отправки
 * div.send-button-container во время генерации получает модификатор класса
 * 'stop' (проверено по бандлу kimi-web-seo) — это и есть индикатор «генерация
 * идёт». Остальные селекторы оставлены как generic-фолбэк.
 */
const STOP_GENERATING_XPATHS = [
    "//div[contains(@class,'send-button-container') and contains(@class,'stop')]",
    "//*[@data-testid='virtuoso-item-list']//*[contains(@class,'stop') and not(contains(@class,'hidden'))]",
    "//div[contains(@class,'stop') and not(contains(@class,'hidden'))]",
    "//div[@aria-label='stop' or @aria-label='Stop' or @aria-label='Stop generating' or @aria-label='stop generating']",
    "//button[contains(@class,'stop') or @aria-label='stop' or @aria-label='Stop' or @aria-label='Stop generating' or @aria-label='stop generating']",
    "//*[@role='button' and (contains(@aria-label,'stop') or contains(@aria-label,'Stop') or contains(@class,'stop'))]",
    "//button[.//text()[contains(.,'Stop') or contains(.,'Остановить') or contains(.,'stop') or contains(.,'停止')]]",
];

/**
 * Экстракторы code-block'ов Kimi (в порядке применения).
 *
 * ОКТЯБРЬ 2026: Kimi рендерит код через свой CodeASTToVNode — внутри
 * <pre><code class="language-x"> лежат span'ы ПОДСВЕТКИ синтаксиса
 * (<span class="hljs-keyword">…</span> и т.п.). Раньше мы отдавали innerHTML
 * как есть — в «код» попадала разметка. Теперь теги срезаются
 * (extractCodeFromPreInner), остаётся чистый текст кода.
 *
 * Сигнатура extract совпадает с сигнатурой колбэка String.replace:
 * (полное совождение, ...capture-группы).
 */
const PRE_EXTRACTORS = [
    {
        pattern: /<pre\b[^>]*>\s*<code\b([^>]*)>([\s\S]*?)<\/code>\s*<\/pre>/gi,
        extract: (_match, codeAttrs, codeInner) => {
            const attrs = String(codeAttrs || '');
            const langMatch = attrs.match(/class\s*=\s*(?:"[^"]*language-([^"'\s]+)[^"]*"|'[^']*language-([^"'\s]+)[^']*')/i);
            const lang = langMatch ? (langMatch[1] || langMatch[2] || '').trim().toLowerCase() : '';
            // Теги подсветки внутри <code> срезаем — иначе в код попадёт разметка.
            return { lang, code: extractCodeFromPreInner(codeInner) };
        },
    },
    {
        pattern: /<pre\b[^>]*>([\s\S]*?)<\/pre>/gi,
        extract: (_match, preInner) => ({ lang: '', code: extractCodeFromPreInner(preInner) }),
    },
];

/**
 * Конфиг class-независимого robust-fallback для Kimi.
 *
 * ОКТЯБРЬ 2026 (разбор бандла kimi-web-seo):
 * - «размышления» и вызовы инструментов — блоки класса toolcall-* (в т.ч.
 *   toolcall-container thinking-container);
 * - текст ответа — div.markdown (внутри div.markdown-container), код —
 *   div.segment-code (карточка с pre>code);
 * - оба лежат в div.segment-content-box — это и есть тело ответа.
 *
 * Один объект на обе точки использования (robust-fallback и снапшот ДО
 * отправки) — иначе они разъезжаются при правке селекторов.
 */
const KIMI_ROBUST = {
    extraThinkClass: 'toolcall-|thinking-container',
    extraThinkLabels: ['已搜索'],
    // Блок ответа — весь segment-content-box (текст + код), а не один
    // markdown-блок (иначе терялась часть ответа).
    blockSelector: "[class*='segment-content-box'], [class*='markdown']",
    // Ответ разбит на блоки (текст + карточка кода) — отдаём пузырь
    // целиком, иначе «самый длинный блок» выкинул бы вторую часть.
    preferWholeBubble: true,
    // Поддеревья, которые вырезаются из снимка ответа (в XPath-пути и
    // в robust-fallback): вызовы инструментов, «размышления» и служебный
    // UI (кнопки, заголовок карточки кода с названием языка).
    stripSelectors: [
        '[class*="toolcall-"]',
        '[class*="thinking-container"]',
        '[class*="think"]',
        '[class*="reasoning"]',
        '[class*="segment-code-header"]',
        '[class*="segment-assistant-actions"]',
        '[class*="markdown-actions"]',
    ],
};

/**
 * Снимаем один внешний div/section-обёрточный тег (Kimi рендерит markdown внутри
 * обёртки; точный класс уточняется живой инспекцией — см. _live_inspection в data).
 */
function stripOuterDiv(html) {
    let s = String(html || '').trim();
    return s
        .replace(/^<(div|section)\b[^>]*>/i, '')
        .replace(/<\/(div|section)>\s*$/i, '')
        .trim();
}

module.exports = {
    id: 'kimi',
    label: 'Web Kimi',
    // Kimi использует namespaced env (KIMI_PORT, KIMI_MAX_BOT_COUNT, ...).
    envPrefix: 'KIMI_',
    // ЯВНАЯ карта имён env (не склейка из префикса — исключаем опечатки).
    envNames: {
        port: 'KIMI_PORT',
        maxBotCount: 'KIMI_MAX_BOT_COUNT',
        retryAfterSec: 'KIMI_RETRY_AFTER_SEC',
        requestTimeoutMs: 'KIMI_REQUEST_TIMEOUT_MS',
        queueWaitMs: 'KIMI_POOL_QUEUE_WAIT_MS',
        serviceModel: 'KIMI_SERVICE_MODEL',
        headless: 'KIMI_HEADLESS',
        chromePath: 'KIMI_CHROME_PATH',
        viewportW: 'KIMI_VIEWPORT_W',
        viewportH: 'KIMI_VIEWPORT_H',
        botUsername: 'KIMI_BOT_USERNAME',
        botPassword: 'KIMI_BOT_PASSWORD',
    },
    defaults: {
        port: 3001,
        maxBotCount: 3,
        retryAfterSec: 3,
        requestTimeoutMs: 300_000,
        queueWaitMs: 60_000,
        serviceModel: 'kimi',
    },
    // Каталоги runtime остаются прежними (профили DeepSeek и Kimi не смешиваются).
    profileRoot: path.join(REPO_ROOT, 'WebKimi', 'worker', '.chrome-profiles'),
    logFile: path.join(REPO_ROOT, 'WebKimi', 'worker', 'logs', 'application.log'),
    logDir: LOG_DIR,
    data,
    auth: { login, checkAlreadyAuthorized },
    promtps: {
        // У Kimi переключателя «размышлений» нет.
        thinkingToggle: false,
        stopXPaths: STOP_GENERATING_XPATHS,
        labels: {
            typeFailed: "Failed to type into Kimi contenteditable editor",
            selectorsFailed: "All answer XPath selectors failed — Kimi UI may have changed (DOM diagnostics dumped to worker log)",
            emptyResponse: "Kimi returned an empty response after retries",
            emptyRetryLog: 'Empty response detected',
        },
        inputExists: async (page, site, service) => {
            const inputXPath = site.data?.xpaths?.chat?.inputLabel?.[service] ||
                "//div[contains(@class,'chat-input-editor') and @role='textbox']";
            return elementExists(page, inputXPath);
        },
        typeInput: async (page, site, service, text) => {
            const inputCss = site.data?.xpaths?.chat?.inputLabelCss?.[service] ||
                ".chat-input-editor[role='textbox']";
            return typeIntoContentEditable(page, inputCss, text);
        },
        send: async (page, site, service) => {
            // ОКТЯБРЬ 2026: у Kimi появилась НАСТОЯЩАЯ кнопка отправки —
            // div.send-button-container (живая инспекция). Кликаем её; если не
            // нашлась/клик не сработал — Enter в редакторе = отправка
            // (Shift+Enter — перенос строки). Обе дороги рабочие.
            const sendBtnXPath = site.data?.xpaths?.chat?.sendMessageButton?.[service];
            let sendClicked = false;
            if (sendBtnXPath) {
                // Кликаем БЕЗ долгого ожидания (waitAndClickX жёг бы 30с на
                // промахе): сначала мгновенная проверка, затем короткое
                // ожидание появления кнопки (Vue перерисовывает её после ввода).
                sendClicked = await clickIfExists(page, sendBtnXPath);
                if (!sendClicked) {
                    try {
                        const el = await waitForXPathCompat(page, sendBtnXPath, { timeout: 3000 });
                        try { await el.click(); sendClicked = true; } finally { await el.dispose(); }
                    } catch (_) { /* кнопки нет — уйдём по Enter */ }
                }
            }
            if (!sendClicked) {
                if (sendBtnXPath) log('Send button not found via XPath, falling back to Enter key');
                try {
                    await page.keyboard.press('Enter');
                } catch (e2) {
                    log('Enter fallback failed: ' + (e2?.message || e2));
                }
            }
        },
        answerXPaths: (site, service) => [
            // ОСНОВНОЙ: последний segment-content-box последнего assistant-item —
            // это ВЕСЬ ответ (текст + блоки кода), а не один markdown-блок.
            site.data.xpaths.chat.answer[service],
            // Fallback 1: весь последний assistant-item (конвертер разберёт
            // markdown-блоки и code-блоки внутри).
            "//div[contains(@class,'chat-content-item-assistant')][last()]",
            // Fallback 2: последний segment-content-box в контейнере чата
            // (на случай, если класс assistant-item сменится).
            "//div[contains(@class,'chat-content-list')]//div[contains(@class,'segment-content-box')][last()]",
            // Fallback 3: legacy-разметка (старый Kimi): markdown-блок без
            // размышлений/инструментов.
            "//div[contains(@class,'chat-content-item-assistant')][last()]//div[contains(@class,'markdown') and not(contains(@class,'think')) and not(contains(@class,'toolcall-content-text'))]",
            "//div[contains(@class,'chat-content-list')]//div[contains(@class,'markdown') and not(contains(@class,'toolcall-content-text')) and not(contains(@class,'think'))]",
            // Fallback 4: любой markdown-блок (broader — крайний случай; в ответе
            // остаётся фильтр мусора — гидрация страницы не пройдёт).
            "//div[contains(@class,'markdown')]",
        ],
        appearTimeoutMs: () => 20000,
        stable: {
            timeoutMs: 240000,
            pollMs: 1500,
            stableTicks: 4,
            minContentLength: 50,
            checkStopButton: true,
            confirmMs: 3000,
            // ФЕЙЛ-ФАСТ на «пустом скелете» (Kimi часто ловит пустую обёртку
            // после смены вёрстки — раньше каждый мёртвый XPath выжигал полное
            // окно стабильности, по логу воркера три мёртвых XPath ≈ минуту
            // пустого ожидания на КАЖДЫЙ запрос).
            maxEmptyStableTicks: 2,
        },
        robustStable: {
            timeoutMs: 120000,
            pollMs: 1500,
            stableTicks: 4,
            minContentLength: 50,
            checkStopButton: true,
            confirmMs: 3000,
            maxEmptyStableTicks: 2,
        },
        retryStable: {
            timeoutMs: 120000,
            pollMs: 1500,
            stableTicks: 4,
            minContentLength: 50,
            checkStopButton: true,
            confirmMs: 3000,
            maxEmptyStableTicks: 2,
        },
        robust: KIMI_ROBUST,
        htmlToMarkdown: (html) => htmlToMarkdown(html, {
            stripOuterDiv,
            preExtractors: PRE_EXTRACTORS,
        }),
        // СНАПШОТ ДО отправки: текст последнего assistant-пузыря до нажатия
        // Enter. Если после «отправки» extracted-ответ совпадёт с ним — сообщение
        // не отправилось, а extraction прочитал СТАРЫЙ пузырь (например,
        // kimi.com восстановил прошлый диалог в персистентном Chrome-профиле).
        snapshotBeforeSend: async (page) => {
            const pre = await getRobustLastAnswerData(page, KIMI_ROBUST);
            return (pre?.text || '').trim();
        },
        // ГВАРД СВЕЖЕСТИ ОТВЕТА. Если сообщение не отправилось (paste-инжект
        // мёртв, Enter не сработал, kimi.com восстановил прошлый диалог в
        // персистентном профиле), нового пузыря нет — и extraction молча
        // возвращал СТАРЫЙ последний assistant-пузырь как «ответ модели»
        // (реальный случай: ARM solve получил анализ hydration payload из
        // чужого ручного диалога). Отбраковываем такие ответы ошибкой, а не
        // мусором: Django пометит пару как error с понятной причиной.
        finalGuards: (inner, messageText, preAnswerText) => {
            const innerTrimmed = inner.trim();
            const msgTrimmed = messageText.trim();
            if (preAnswerText && innerTrimmed === preAnswerText) {
                log('STALE ANSWER guard: extracted answer is identical to the assistant bubble that existed BEFORE sending — message was probably not sent');
                return {
                    ok: false,
                    reason: "can't send message",
                    data: {
                        "moreInformation": "Stale answer: extracted text is identical to the assistant bubble that existed BEFORE sending — the message was probably not sent (Kimi UI changed / Enter did not submit)"
                    }
                };
            }
            // Частичный фрагмент промпта (равенством не ловился).
            if (msgTrimmed && require('../worker/modules/promtps').isEchoOfUserMessage(innerTrimmed, msgTrimmed)) {
                log("STALE ANSWER guard: extraction picked the user's own message (fragment) instead of the answer");
                return {
                    ok: false,
                    reason: "can't send message",
                    data: {
                        "moreInformation": "Extraction picked the user's own message bubble instead of the answer — answer selectors are stale"
                    }
                };
            }
            return null;
        },
    },
};
