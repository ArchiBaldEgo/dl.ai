'use strict';

/**
 * Описание сайта Web DeepSeek (chat.deepseek.com).
 *
 * Здесь собрано ВСЁ, что различает DeepSeek и Kimi:
 * - конфиг пула (порт, лимиты, env-префикс);
 * - селекторы вёрстки (data.xpaths) и URL сайта (data.services);
 * - сценарий входа (Google OAuth);
 * - site-специфичные хуки отправки сообщения (ввод/отправка/селекторы ответа).
 *
 * Общий алгоритм (стабилизация ответа, анти-обрыв, robust-fallback, эхо-гейт,
 * DOM-диагностика) — в worker/modules/promtps.js. Правки общих механизмов
 * применяются ОДИН раз, а не в двух копиях пулов.
 */

const fs = require('fs');
const path = require('path');

const {
    waitAndType,
    waitAndTypeX,
    waitAndClick,
    waitAndClickX,
    elementExists,
    clickIfExists,
} = require('../worker/core/page-utils');
const {
    waitForXPathCompat,
    htmlToMarkdown,
    extractCodeFromPreInner,
    decodeHtmlEntities,
} = require('../worker/modules/promtps');
const { log, error } = require('../worker/utils/logger');
const { sleep } = require('../worker/utils/helpers');
const { toBool } = require('../worker/utils/env');

const REPO_ROOT = path.resolve(__dirname, '..', '..');

/**
 * URL'ы и XPath'ы сайта (бывший WebDeepseek/worker/data.json).
 */
const data = {
    "services": {
        "deepseek": "https://chat.deepseek.com/"
    },
    "loginUrls": {
        "deepseek": "https://chat.deepseek.com/sign_in"
    },
    "registerUrls": {
        "deepseek": "https://chat.deepseek.com/sign_up"
    },

    "xpaths": {
        "auth": {
            "chatUrl": {
                "deepseek": "https://chat.deepseek.com/"
            },
            "loginUrl": {
                "deepseek": "https://chat.deepseek.com/sign_in"
            },
            "googleButton": {
                "deepseek": [
                    "//div[contains(@class,'ds-button') and .//span[contains(text(),'Google')]]",
                    "//button[.//span[contains(text(),'Google')]]",
                    "//div[contains(@class,'ds-button') and .//*[contains(text(),'Google')]]",
                    "//button[.//*[contains(text(),'Google')]]",
                    "//a[contains(@href,'accounts.google.com') or contains(@data-url,'accounts.google.com')]",
                    "//*[contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'войти с помощью google')]",
                    "//*[contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'sign in with google')]",
                    "//*[contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'continue with google')]"
                ]
            },
            "googleEmailInput": {
                "deepseek": "input[type='email']"
            },
            "googleEmailNext": {
                "deepseek": "#identifierNext"
            },
            "googlePasswordInput": {
                "deepseek": "input[type='password']"
            },
            "googlePasswordNext": {
                "deepseek": "#passwordNext"
            },
            "googleConsent": {
                "deepseek": [
                    "#submit_approve_access",
                    "button[name='approve_access']",
                    "//button[contains(.,'Разрешить') or contains(.,'Allow') or contains(.,'Продолжить') or contains(.,'Continue') or contains(.,'Подтвердить')]"
                ]
            },
            "googleError": {
                "deepseek": "//div[contains(@class,'o6cuMc') or contains(@jsname,'B34E7')] | //div[contains(text(),'Wrong password') or contains(text(),'Неверный пароль') or contains(text(),'Couldn') or contains(text(),'не удалось') or contains(text(),'not secure') or contains(text(),'не безопасно')]"
            }
        },
        "register": {
            "emailLabel": {
                "deepseek": "//input[@type='text' and contains(@placeholder, 'Email')]"
            },
            "passwordLabel": {
                "deepseek": "//input[@type='password']"
            },
            "confirmPasswordLabel": {
                "deepseek": "//input[@type='password' and contains(@placeholder, 'Confirm password')]"
            },
            "sendCodeButton": {
                "deepseek": "//span[text()='Send code']/ancestor::button"
            },
            "codeLabel": {
                "deepseek": "//input[@type='tel']"
            },
            "signUpButton": {
                "deepseek": "//span[text()='Sign up']/ancestor::button"
            }
        },
        "chat": {
            "thinkingButtonEnabled": {
                "deepseek": "//span[text()='DeepThink']/ancestor::div[@aria-pressed='false']"
            },
            "thinkingButtonDisabled": {
                "deepseek": "//span[text()='DeepThink']/ancestor::div[@aria-pressed='true']"
            },
            "inputLabel": {
                "deepseek": "//textarea"
            },
            "sendMessageButton": {
                "deepseek": "//div[contains(@class,'ds-button') and contains(@class,'primary') and not(contains(@class,'disabled'))] | //button[contains(@class,'send') or contains(@aria-label,'send') or contains(@data-testid,'send')] | //div[@role='button' and not(contains(@class,'disabled')) and .//textarea]"
            },
            "answer": {
                "deepseek": "//div[contains(@class, 'ds-message')]/div[contains(@class, 'ds-markdown')]"
            },
            "fullAnswer": {
                "deepseek": "//div[contains(@class, 'ds-message')]"
            }
        }
    }
};

const LOG_DIR = path.join(REPO_ROOT, 'WebDeepseek', 'worker', 'logs');

function ensureLogDir() {
    if (!fs.existsSync(LOG_DIR)) fs.mkdirSync(LOG_DIR, { recursive: true });
}

function isAuthDebugEnabled() {
    return toBool(process.env.AUTH_DEBUG, false);
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
    const raw = Number(process.env.AUTH_TIMEOUT_MS || 45000);
    return Number.isFinite(raw) && raw > 0 ? raw : 45000;
}

async function waitForGooglePage(browser, originalPage, timeoutMs) {
    // Google OAuth открывается либо в popup, либо в той же вкладке. Опрашиваем оба.
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
        // Тот же таб?
        try {
            const u = originalPage.url();
            if (u && u.includes('accounts.google.com')) return originalPage;
        } catch (_) {}

        // Popup?
        try {
            const targets = browser.targets();
            for (const t of targets) {
                let u = '';
                try { u = t.url(); } catch (_) {}
                if (u && u.includes('accounts.google.com') && t.type() === 'page') {
                    const p = await t.page();
                    if (p) return p;
                }
            }
        } catch (_) {}

        await sleep(300);
    }
    return null;
}

/**
 * Быстрый путь: если в постоянном профиле уже есть живая сессия, то после
 * открытия chat.deepseek.com сразу появится поле чата — логин не нужен.
 */
async function checkAlreadyAuthorized(ctx, payload = {}) {
    const page = ctx?.page;
    if (!page) return false;

    const currentService = payload.model;
    const chatUrl =
        data?.xpaths?.auth?.chatUrl?.[currentService] ||
        data?.services?.[currentService] ||
        'https://chat.deepseek.com/';
    const chatInputXPath = data?.xpaths?.chat?.inputLabel?.[currentService] || '//textarea';

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
 * Авторизация DeepSeek выполняется ТОЛЬКО через Google (кнопка «Войти с помощью
 * Google», обычных полей логина/пароля на сайте нет). Поэтому поток входа:
 *   1. открыть страницу входа DeepSeek;
 *   2. нажать «Войти с помощью Google»;
 *   3. дождаться страницы accounts.google.com (popup или переход в той же вкладке);
 *   4. ввести email, нажать «Далее»;
 *   5. ввести пароль, нажать «Далее»;
 *   6. при необходимости подтвердить доступ (экран согласия);
 *   7. дождаться возврата на chat.deepseek.com и появления поля чата.
 *
 * Постоянный Chrome-профиль (userDataDir в bot.js) хранит сессию Google, поэтому
 * повторный логин обычно не требуется — см. checkAlreadyAuthorized().
 */
async function login(ctx, payload = {}) {
    const browser = ctx?.browser;
    const page = ctx?.page;

    if (!page || !browser) {
        return {
            ok: false,
            reason: 'ctx.page/browser отсутствует',
            data: { isAuthorized: false },
        };
    }

    const currentService = payload.model;
    const timeWait = getTimeoutMs();
    const loginUrl =
        data?.xpaths?.auth?.loginUrl?.[currentService] || data?.loginUrls?.[currentService];
    const googleButtonRaw = data?.xpaths?.auth?.googleButton?.[currentService];
    const googleButtonXPaths = Array.isArray(googleButtonRaw)
        ? googleButtonRaw
        : googleButtonRaw
        ? [googleButtonRaw]
        : [];
    const emailSel = data?.xpaths?.auth?.googleEmailInput?.[currentService];
    const emailNextSel = data?.xpaths?.auth?.googleEmailNext?.[currentService];
    const passSel = data?.xpaths?.auth?.googlePasswordInput?.[currentService];
    const passNextSel = data?.xpaths?.auth?.googlePasswordNext?.[currentService];
    const consentRaw = data?.xpaths?.auth?.googleConsent?.[currentService];
    const consentSelectors = Array.isArray(consentRaw)
        ? consentRaw
        : consentRaw
        ? [consentRaw]
        : [];
    const googleErrorXPath = data?.xpaths?.auth?.googleError?.[currentService];
    const chatInputXPath = data?.xpaths?.chat?.inputLabel?.[currentService] || '//textarea';

    if (!loginUrl || !googleButtonXPaths.length || !emailSel || !passSel) {
        return { ok: false, reason: 'нет селекторов Google OAuth для сервиса' };
    }

    try {
        // 1. Страница входа DeepSeek.
        await page.goto(loginUrl, { waitUntil: 'domcontentloaded', timeout: timeWait });

        // 2. Кнопка «Войти с помощью Google».
        let clickOk = false;
        for (const xp of googleButtonXPaths) {
            clickOk = await waitAndClickX(page, xp);
            if (clickOk) break;
        }
        if (!clickOk) {
            await writeAuthDebugArtifacts(page, 'google_button_missing');
            return { ok: false, reason: 'кнопка «Войти с помощью Google» не найдена' };
        }

        // 3. Ждём страницу Google.
        const googlePage = await waitForGooglePage(browser, page, timeWait);
        if (!googlePage) {
            await writeAuthDebugArtifacts(page, 'google_page_missing');
            return { ok: false, reason: 'страница авторизации Google не открылась' };
        }

        // 4. Email.
        const emailOk = await waitAndType(googlePage, emailSel, payload.username);
        if (!emailOk) {
            await writeAuthDebugArtifacts(googlePage, 'google_email_missing');
            return { ok: false, reason: 'поле email на странице Google не найдено (возможно, экран выбора аккаунта)' };
        }
        await waitAndClick(googlePage, emailNextSel);

        // 5. Пароль.
        const passOk = await waitAndType(googlePage, passSel, payload.password);
        if (!passOk) {
            await writeAuthDebugArtifacts(googlePage, 'google_password_missing');
            return { ok: false, reason: 'поле пароля Google не появилось (возможно, неверный email или блокировка)' };
        }
        await waitAndClick(googlePage, passNextSel);

        // 6. Ждём исход: возврат на DeepSeek (успех) / экран согласия / ошибка Google.
        const deadline = Date.now() + timeWait;
        let settled = false;
        let clickedConsentOnce = false;
        while (Date.now() < deadline && !settled) {
            await sleep(800);

            // Успех — на исходной вкладке DeepSeek появилось поле чата.
            try {
                if (await elementExists(page, chatInputXPath)) {
                    settled = true;
                    break;
                }
            } catch (_) {}

            // Ошибка Google (неверный пароль / «не безопасно» / блокировка).
            if (googleErrorXPath) {
                try {
                    if (await elementExists(googlePage, googleErrorXPath)) {
                        await writeAuthDebugArtifacts(googlePage, 'google_error');
                        return {
                            ok: false,
                            reason: 'Google отклонил вход: неверный пароль или сработала защита от автоматизации',
                        };
                    }
                } catch (_) {} // popup мог закрыться
            }

            // Экран согласия — кликаем «Разрешить/Allow/Продолжить».
            try {
                for (const sel of consentSelectors) {
                    if (await clickIfExists(googlePage, sel)) {
                        clickedConsentOnce = true;
                        break;
                    }
                }
            } catch (_) {}
        }

        if (!settled) {
            await writeAuthDebugArtifacts(page, 'login_timeout');
            return {
                ok: false,
                reason: clickedConsentOnce
                    ? 'вход не завершён за отведённое время после подтверждения доступа'
                    : 'вход не завершён за отведённое время',
            };
        }

        return { ok: true, data: { isAuthorized: true } };
    } catch (er) {
        const msg = er?.message || String(er);
        const stack = er?.stack || '';

        let url = '';
        let title = '';
        let html = '';

        try { url = page.url(); } catch (_) {}
        try { title = await page.title(); } catch (_) {}
        try {
            html = await page.content();
            html = String(html).slice(0, 4000);
        } catch (_) {}

        await writeAuthDebugArtifacts(page, 'exception');

        error(`[auth] login exception: ${msg}`);
        if (stack) error(`[auth] stack: ${stack}`);
        if (url) error(`[auth] url: ${url}`);
        if (title) error(`[auth] title: ${title}`);
        if (html) error(`[auth] html_head: ${html}`);

        return {
            ok: false,
            reason: `исключение входа: ${msg}`,
            data: { moreInformation: stack || msg, url, title },
        };
    }
}

/**
 * Селекторы кнопки «Stop» (генерация ещё идёт) для DeepSeek.
 * DeepSeek периодически меняет вёрстку кнопки Stop, поэтому здесь широкий набор
 * вариантов (класс stop, aria-label, role=button, SVG с stop-иконкой, текст).
 * Если ни один не совпадает с актуальной кнопкой — isStillGenerating всегда false,
 * и тогда единственный сигнал завершения — подтверждение перечитыванием (confirmMs).
 */
const STOP_GENERATING_XPATHS = [
    "//div[contains(@class,'stop') and not(contains(@class,'hidden'))]",
    "//div[@aria-label='stop' or @aria-label='Stop' or @aria-label='Stop generating' or @aria-label='stop generating']",
    "//button[contains(@class,'stop') or @aria-label='stop' or @aria-label='Stop' or @aria-label='Stop generating' or @aria-label='stop generating']",
    "//div[contains(@class,'ds-button') and .//svg[contains(@class,'stop')]]",
    "//div[contains(@class,'ds-button') and .//svg[contains(@class,'icon') and contains(@class,'stop')]]",
    "//button[contains(@class,'ds-button') and .//svg[contains(@class,'stop')]]",
    "//*[@role='button' and (contains(@aria-label,'stop') or contains(@aria-label,'Stop') or contains(@class,'stop'))]",
    "//button[.//text()[contains(.,'Stop') or contains(.,'Остановить') or contains(.,'stop')]]",
];

// Варианты answer-XPath БЕЗ reasoning: контейнер размышлений тоже носит
// ds-markdown — без исключения fallback ловит «простыню размышлений».
const NOT_THINK = " and not(contains(@class,'think')) and not(contains(@class,'reason'))";

/**
 * Экстракторы code-block'ов DeepSeek (в порядке применения).
 * Первый — специфичный для DeepSeek (md-code-block + span.d813de27),
 * второй — generic <pre><code class="language-x">.
 *
 * Сигнатура extract совпадает с сигнатурой колбэка String.replace:
 * (полное совпадение, ...capture-группы). Порядок важен.
 */
const PRE_EXTRACTORS = [
    {
        pattern: /<div\b[^>]*\bmd-code-block\b[^>]*>[\s\S]*?<pre\b[^>]*>([\s\S]*?)<\/pre>[\s\S]*?<\/div>/gi,
        // У этого паттерна ОДНА capture-группа (содержимое <pre>), а полное
        // совпадение (весь md-code-block) уходит в blockHtml — как в исходном
        // колбэке replace.
        extract: (blockHtml, preInner) => {
            const langMatch =
                blockHtml.match(/<span[^>]*\bd813de27\b[^>]*>([^<]+)<\/span>/i) ||
                blockHtml.match(/class="language-([^"]+)"/i);
            const lang = langMatch ? String(langMatch[1] || '').trim().toLowerCase() : '';
            return { lang, code: extractCodeFromPreInner(preInner) };
        },
    },
    {
        pattern: /<pre\b[^>]*>\s*<code\b([^>]*)>([\s\S]*?)<\/code>\s*<\/pre>/gi,
        extract: (_match, codeAttrs, codeInner) => {
            const langMatch = String(codeAttrs).match(/class\s*=\s*"[^"]*language-([^"\s]+)[^"]*"/i);
            const lang = langMatch ? langMatch[1].trim().toLowerCase() : '';
            return { lang, code: decodeHtmlEntities(codeInner).replace(/\r\n/g, '\n').trim() };
        },
    },
];

/**
 * Снимаем внешний div.ds-markdown — конвертер работает с содержимым.
 */
function stripOuterDiv(html) {
    let s = String(html || '').trim();
    return s
        .replace(/^<div\b[^>]*\bds-markdown\b[^>]*>/i, '')
        .replace(/<\/div>\s*$/i, '')
        .trim();
}

module.exports = {
    id: 'deepseek',
    label: 'Web DeepSeek',
    // Префикс env-переменных пула: DeepSeek использует «голые» имена
    // (PORT, MAX_BOT_COUNT, BOT_USERNAME...), Kimi — namespaced KIMI_*.
    envPrefix: '',
    // ЯВНАЯ карта имён env (не склейка из префикса — исключаем опечатки).
    envNames: {
        port: 'PORT',
        maxBotCount: 'MAX_BOT_COUNT',
        retryAfterSec: 'RETRY_AFTER_SEC',
        requestTimeoutMs: 'REQUEST_TIMEOUT_MS',
        queueWaitMs: 'BOT_POOL_QUEUE_WAIT_MS',
        serviceModel: 'SERVICE_MODEL',
        headless: 'HEADLESS',
        chromePath: 'CHROME_PATH',
        viewportW: 'VIEWPORT_W',
        viewportH: 'VIEWPORT_H',
        botUsername: 'BOT_USERNAME',
        botPassword: 'BOT_PASSWORD',
    },
    defaults: {
        port: 3000,
        maxBotCount: 3,
        retryAfterSec: 3,
        requestTimeoutMs: 300_000,
        queueWaitMs: 60_000,
        serviceModel: 'deepseek',
    },
    // Каталоги runtime остаются прежними (профили DeepSeek и Kimi не смешиваются).
    profileRoot: path.join(REPO_ROOT, 'WebDeepseek', 'worker', '.chrome-profiles'),
    logFile: path.join(REPO_ROOT, 'WebDeepseek', 'worker', 'logs', 'application.log'),
    logDir: LOG_DIR,
    data,
    auth: { login, checkAlreadyAuthorized },
    promtps: {
        // У DeepSeek есть переключатель «размышлений» (DeepThink).
        thinkingToggle: true,
        stopXPaths: STOP_GENERATING_XPATHS,
        labels: {
            typeFailed: "Failed to type into DeepSeek textarea",
            selectorsFailed: "All answer XPath selectors failed — DeepSeek UI may have changed (DOM diagnostics dumped to worker log)",
            emptyResponse: "DeepSeek returned an empty response after retries",
            emptyRetryLog: 'Empty/short response detected',
        },
        inputExists: async (page, site, service) =>
            elementExists(page, site.data.xpaths.chat.inputLabel[service]),
        typeInput: async (page, site, service, text) => {
            // Исходное поведение DeepSeek: результат ввода не проверяется —
            // при провале ввода запрос всё равно уходит на отправку.
            await waitAndTypeX(page, site.data.xpaths.chat.inputLabel[service], text);
            return true;
        },
        send: async (page, site, service) => {
            // Try clicking the send button via XPath. If the primary selector fails
            // (DeepSeek UI changed), fall back to pressing Enter in the textarea.
            const sendClicked = await waitAndClickX(page, site.data.xpaths.chat.sendMessageButton[service]);
            if (!sendClicked) {
                log('Send button not found via XPath, falling back to Enter key');
                try {
                    const textarea = await waitForXPathCompat(page, site.data.xpaths.chat.inputLabel[service], { timeout: 10000 });
                    await textarea.press('Enter');
                } catch (e2) {
                    // Last resort: page-level keyboard Enter
                    await page.keyboard.press('Enter');
                }
            }
        },
        answerXPaths: (site, service) => [
            site.data.xpaths.chat.answer[service],
            // Fallback 1: any element with ds-markdown class inside a message
            // (ограничены контейнером сообщения ds-message).
            "//div[contains(@class,'ds-message')]//div[contains(@class,'ds-markdown')]",
            "//div[contains(@class,'ds-message')]/div[contains(@class,'ds-markdown')" + NOT_THINK + "]",
            // Fallback 1a: markdown внутри сообщения (ds-message покрывает и
            // обёртку сообщения — ответ всегда внутри неё).
            "//div[contains(@class,'ds-message')]//div[contains(@class,'markdown')" + NOT_THINK + "]",
            // Fallback 2: ГЛОБАЛЬНЫЕ селекторы — крайний случай (сайт сменил
            // классы сообщений). Ответ всё равно проходит фильтр мусора:
            // служебные данные страницы (гидрация) до Django не дойдут.
            "//div[contains(@class,'ds-markdown')" + NOT_THINK + "]",
            "//div[contains(@class,'ds-markdown')]",
            // Fallback 3: any element with markdown class inside a chat message
            "//div[contains(@class,'message')]//div[contains(@class,'markdown')]",
        ],
        // DeepThink рассуждает ДО появления первого ds-markdown (ответа):
        // 20s appear-таймаут «фейлил» живой селектор, а робастный fallback
        // в это время ловил пузырь юзера. Первому (выверенному) селектору
        // при thinking даём 2 минуты на появление ответа.
        appearTimeoutMs: (xp, site, service, thinking) => {
            const isPrimary = xp === site.data.xpaths.chat.answer[service];
            return isPrimary && thinking ? 120000 : 20000;
        },
        stable: {
            timeoutMs: 240000,
            pollMs: 2000,
            stableTicks: 6,
            minContentLength: 50,
            checkStopButton: true,
            confirmMs: 8000,
        },
        robustStable: {
            timeoutMs: 120000,
            pollMs: 2000,
            stableTicks: 6,
            minContentLength: 50,
            checkStopButton: true,
            confirmMs: 5000,
        },
        retryStable: {
            timeoutMs: 120000,
            pollMs: 2000,
            stableTicks: 6,
            minContentLength: 50,
            checkStopButton: true,
            confirmMs: 5000,
        },
        robust: {
            extraThinkClass: null,
            extraThinkLabels: [],
            blockSelector: "[class*='ds-markdown'], [class*='markdown']",
        },
        htmlToMarkdown: (html) => htmlToMarkdown(html, {
            stripOuterDiv,
            preExtractors: PRE_EXTRACTORS,
        }),
    },
};
