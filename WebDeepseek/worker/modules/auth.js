const fs = require('fs');
const path = require('path');

const data = require('../data.json');

const { error } = require('../utils/logger');
const { sleep } = require('../utils/helpers');
const {
    waitAndType,
    waitAndClick,
    waitAndClickX,
    waitForXPathCompat,
    elementExists,
    clickIfExists,
} = require('../core/page-utils');
const { toBool } = require('../utils/env');

const LOG_DIR = path.join(__dirname, '../logs');

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

module.exports = {
    login,
    checkAlreadyAuthorized,
};