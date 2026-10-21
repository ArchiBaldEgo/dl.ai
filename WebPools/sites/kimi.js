'use strict';

/**
 * Описание сайта Web Kimi (www.kimi.ai).
 *
 * Отличия от DeepSeek (всё, что было в WebKimi/worker/modules/*):
 * - вход НЕ автоматизируется (телефон/SMS, WeChat QR, email) — сессия
 *   сидируется вручную один раз (WebKimi/seed.js) в постоянный Chrome-профиль;
 * - поле ввода — contenteditable div (НЕ textarea): текст вставляется через
 *   событие paste (ClipboardEvent + DataTransfer) с fallback на keyboard.type;
 * - список сообщений — Virtuoso (виртуализированный);
 * - ответ достаётся класс-независимым robust-fallback (см. worker/modules/promtps.js);
 * - гвард «stale answer»: ответ, совпадающий с пузырём ДО отправки, отбраковывается.
 *
 * Общий алгоритм — в worker/modules/promtps.js (одна копия на оба пула).
 */

const fs = require('fs');
const path = require('path');

const {
    waitAndClickX,
    elementExists,
} = require('../worker/core/page-utils');
const {
    waitForXPathCompat,
    htmlToMarkdown,
    extractCodeFromPreInner,
    decodeHtmlEntities,
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
    "_live_inspection_2026_08": "ВНИМАНИЕ: эта инспекция УСТАРЕЛА — worker/logs/application.log показывает, что div.markdown теперь совпадает с пустым скелетом ('matched an empty skeleton (1 chars text)'), т.е. Kimi снова сменил вёрстку и текст ответа больше не внутри div.markdown. Селекторы answer.kimi (//div[..markdown..]) поэтому ловят пустоту. Ответ теперь достаётся НЕ по классу, а класс-независимым robust-fallback в worker/modules/promtps.js (getRobustLastAnswerHtml): берётся весь последний «пузырь» ассистента по структурным признакам (class*='assistant' / последний дочерний блок чат-контейнера) и конвертируется целиком. При провале всех селекторов в лог дампится DOM-диагностика (dumpDomDiagnostics) — по ней можно откалибровать answer.kimi заново. Поле ввода: div.chat-input-editor[role=textbox] (contenteditable). Enter в contenteditable отправляет надёжно.",
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
                "kimi": ""
            },
            "answer": {
                "kimi": "//div[contains(@class,'chat-content-item-assistant')]//div[contains(@class,'markdown')]"
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
        // Поле ввода чата означает, что мы залогинены.
        await waitForXPathCompat(page, chatInputXPath, { timeout: 15000 });
        return true;
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
 * Ввод текста в contenteditable div Kimi. element.type() в contenteditable
 * ненадёжен (Kimi слушает input/paste, а не keystroke-накопление), поэтому
 * вставляем через событие paste (ClipboardEvent + DataTransfer) — доказанный
 * подход для [data-testid='msh-chatinput-editor']. Fallback — page.keyboard.type
 * при сфокусированном элементе.
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

        // 1) Paste-инжект — основной путь.
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

        if (!injected) {
            // 2) Fallback — посылаем keystrokes в сфокусированный contenteditable.
            log('typeIntoContentEditable: paste не заполнил поле, fallback на keyboard.type');
            await page.keyboard.type(txt, { delay: 0 });
            await sleep(300);
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
 * Селекторы кнопки «Stop» (генерация ещё идёт) для Kimi.
 * Kimi-специфичный класс кнопки Stop неизвестен (TODO — живая инспекция),
 * поэтому держим широкий набор generic-вариантов (aria-label, role=button, текст).
 * Если ни один не совпадает — isStillGenerating всегда false, и единственный
 * сигнал завершения — подтверждение перечитыванием (confirmMs).
 */
const STOP_GENERATING_XPATHS = [
    "//*[@data-testid='virtuoso-item-list']//*[contains(@class,'stop') and not(contains(@class,'hidden'))]",
    "//div[contains(@class,'stop') and not(contains(@class,'hidden'))]",
    "//div[@aria-label='stop' or @aria-label='Stop' or @aria-label='Stop generating' or @aria-label='stop generating']",
    "//button[contains(@class,'stop') or @aria-label='stop' or @aria-label='Stop' or @aria-label='Stop generating' or @aria-label='stop generating']",
    "//*[@role='button' and (contains(@aria-label,'stop') or contains(@aria-label,'Stop') or contains(@class,'stop'))]",
    "//button[.//text()[contains(.,'Stop') or contains(.,'Остановить') or contains(.,'stop') or contains(.,'停止')]]",
];

/**
 * Экстракторы code-block'ов Kimi (в порядке применения).
 * Kimi использует свой Vue markdown-рендер, поэтому работаем по generic
 * <pre><code class="language-x"> и по <pre> без вложенного <code>.
 *
 * Сигнатура extract совпадает с сигнатурой колбэка String.replace:
 * (полное совпадение, ...capture-группы).
 */
const PRE_EXTRACTORS = [
    {
        pattern: /<pre\b[^>]*>\s*<code\b([^>]*)>([\s\S]*?)<\/code>\s*<\/pre>/gi,
        extract: (_match, codeAttrs, codeInner) => {
            const langMatch = String(codeAttrs).match(/class\s*=\s*"[^"]*language-([^"\s]+)[^"]*"/i);
            const lang = langMatch ? langMatch[1].trim().toLowerCase() : '';
            return { lang, code: decodeHtmlEntities(codeInner).replace(/\r\n/g, '\n').trim() };
        },
    },
    {
        pattern: /<pre\b[^>]*>([\s\S]*?)<\/pre>/gi,
        extract: (_match, preInner) => ({ lang: '', code: extractCodeFromPreInner(preInner) }),
    },
];

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
                "//*[@data-testid='msh-chatinput-editor']";
            return elementExists(page, inputXPath);
        },
        typeInput: async (page, site, service, text) => {
            const inputCss = site.data?.xpaths?.chat?.inputLabelCss?.[service] ||
                "[data-testid='msh-chatinput-editor']";
            return typeIntoContentEditable(page, inputCss, text);
        },
        send: async (page, site, service) => {
            // Отправка: в Kimi намеренно НЕ ищем кнопку отправки по XPath —
            // data.xpaths.chat.sendMessageButton пуст (""), т.к. реальный селектор
            // кнопки не выверен, а waitAndClickX жжёт 30с на промахе. Enter в
            // contenteditable-редакторе Kimi = отправка (Shift+Enter — перенос строки).
            const sendBtnXPath = site.data?.xpaths?.chat?.sendMessageButton?.[service];
            let sendClicked = false;
            if (sendBtnXPath) {
                sendClicked = await waitAndClickX(page, sendBtnXPath);
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
            site.data.xpaths.chat.answer[service],
            // Fallback 1: явно последний assistant-item (getLastOuterHtmlByXPath берёт
            // последний snapshot-узел в document order — текущий ответ).
            "//div[contains(@class,'chat-content-item-assistant')][last()]//div[contains(@class,'markdown')]",
            // ВАРИАНТЫ БЕЗ reasoning (прецедент Fallback 2): контейнер размышлений
            // тоже может носить 'markdown' — без исключений ловит простыню размышлений.
            "//div[contains(@class,'chat-content-item-assistant')][last()]//div[contains(@class,'markdown') and not(contains(@class,'think')) and not(contains(@class,'toolcall-content-text'))]",
            // Fallback 2: answer-markdown без toolcall/thinking-обёртки.
            // ОГРАНИЧЕНЫ контейнером чата (chat-content-list): глобальные
            // markdown-блоки ловили служебные блоки страницы (гидрация/лайдинг)
            // вместо ответа модели. Глобальный вариант оставлен ниже как
            // последний крайний случай (если класс контейнера сменится).
            "//div[contains(@class,'chat-content-list')]//div[contains(@class,'markdown-container') and not(contains(@class,'toolcall-content-text'))]//div[contains(@class,'markdown')]",
            "//div[contains(@class,'chat-content-list')]//div[contains(@class,'markdown') and not(contains(@class,'toolcall-content-text')) and not(contains(@class,'think'))]",
            "//div[contains(@class,'chat-content-list')]//div[contains(@class,'markdown')]",
            // Fallback 3: весь последний assistant-item (конвертер разберёт markdown-блоки внутри)
            "//div[contains(@class,'chat-content-item-assistant')][last()]",
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
        robust: {
            // У Kimi текст «размышлений» живёт в контейнере toolcall-content-text.
            extraThinkClass: 'toolcall-content-text',
            extraThinkLabels: ['已搜索'],
            blockSelector: "[class*='markdown']",
        },
        htmlToMarkdown: (html) => htmlToMarkdown(html, {
            stripOuterDiv,
            preExtractors: PRE_EXTRACTORS,
        }),
        // СНАПШОТ ДО отправки: текст последнего assistant-пузыря до нажатия
        // Enter. Если после «отправки» extracted-ответ совпадёт с ним — сообщение
        // не отправилось, а extraction прочитал СТАРЫЙ пузырь (например,
        // kimi.com восстановил прошлый диалог в персистентном Chrome-профиле).
        snapshotBeforeSend: async (page) => {
            const pre = await getRobustLastAnswerData(page, {
                extraThinkClass: 'toolcall-content-text',
                extraThinkLabels: ['已搜索'],
                blockSelector: "[class*='markdown']",
            });
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
