const fs = require('fs');
const path = require('path');

const data = require('../data.json');

const { error } = require('../utils/logger');
const { waitAndTypeX, waitAndClickX, waitForXPathCompat } = require('../core/page-utils');

const LOG_DIR = path.join(__dirname, '../logs');

function ensureLogDir() {
    if (!fs.existsSync(LOG_DIR)) fs.mkdirSync(LOG_DIR, { recursive: true });
}

function toBool(v, def = false) {
    if (v === undefined || v === null || v === '') return def;
    const s = String(v).toLowerCase().trim();
    if (['1', 'true', 'yes', 'y', 'on'].includes(s)) return true;
    if (['0', 'false', 'no', 'n', 'off'].includes(s)) return false;
    return def;
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

async function login(ctx, payload = {}) {
    const page = ctx?.page;

    if (!page) 
        return { 
            ok: false, 
            reason: 'ctx.page is missing',
            data: {
                "isAuthorized": false
            }
        };

    const currentService = payload.model;
    const timeWait = getTimeoutMs();
    const loginUrl = data?.loginUrls?.[currentService];
    const loginXPath = data?.xpaths?.auth?.loginLabel?.[currentService];
    const passwordXPath = data?.xpaths?.auth?.passwordLabel?.[currentService];
    const authButtonXPath = data?.xpaths?.auth?.authButton?.[currentService];
    const incorrectPassXPath = data?.xpaths?.auth?.incorrectPassMessage?.[currentService];

    if (!loginUrl || !loginXPath || !passwordXPath || !authButtonXPath) {
        return {
            ok: false,
            reason: 'missing auth selectors or login url for service',
        };
    }

    try {
        await ctx.page.goto(loginUrl, { waitUntil: 'domcontentloaded', timeout: timeWait });

        const loginOk = await waitAndTypeX(page, loginXPath, payload.username);
        if (!loginOk) {
            await writeAuthDebugArtifacts(page, 'login_field_missing');
            return { ok: false, reason: 'login field not found' };
        }

        const passOk = await waitAndTypeX(page, passwordXPath, payload.password);
        if (!passOk) {
            await writeAuthDebugArtifacts(page, 'password_field_missing');
            return { ok: false, reason: 'password field not found' };
        }

        const navPromise = page.waitForNavigation({ waitUntil: 'networkidle2', timeout: timeWait })
            .then(() => 'nav')
            .catch(() => 'nav_timeout');

        const badPromise = waitForXPathCompat(ctx.page, incorrectPassXPath, { timeout: timeWait })
            .then(() => 'bad')
            .catch(() => 'bad_timeout');

        const clickOk = await waitAndClickX(page, authButtonXPath);
        if (!clickOk) {
            await writeAuthDebugArtifacts(page, 'auth_button_missing');
            return { ok: false, reason: 'click failed' };
        }

        const winner = await Promise.race([navPromise, badPromise]);

        if (winner === 'bad') {
            return {
                ok: false,
                reason: "incorrect password or account don't reggered"
            }
        }

        // DeepSeek (SPA) often does not trigger a full navigation after login.
        // Treat a navigation timeout as SUCCESS only if we actually reached the chat UI.
        const chatInputXPath =
            data?.xpaths?.chat?.inputLabel?.[currentService] ||
            '//textarea';
        try {
            await waitForXPathCompat(ctx.page, chatInputXPath, { timeout: timeWait });
        } catch {
            await writeAuthDebugArtifacts(page, 'chat_input_missing');
            return {
                ok: false,
                reason: 'login did not reach chat UI (input not found)'
            };
        }

        return {
            ok: true,
            data: {
                "isAuthorized": true
            }
        }
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
            html = String(html).slice(0, 4000); // чтобы не заспамить лог
        } catch (_) {}

        await writeAuthDebugArtifacts(page, 'exception');

        error(`[auth] login exception: ${msg}`);
        if (stack) error(`[auth] stack: ${stack}`);
        if (url) error(`[auth] url: ${url}`);
        if (title) error(`[auth] title: ${title}`);
        if (html) error(`[auth] html_head: ${html}`);

        return {
            ok: false,
            reason: `login exception: ${msg}`,
            data: { moreInformation: stack || msg, url, title }
        };
    }
}

async function register(ctx, payload) {
    const page = ctx?.page;

    if (!page) 
        return { ok: false, reason: 'ctx.page is missing' };

    try {
        let currentService = payload.model;

        await ctx.page.goto(data.registerUrls[currentService]);

        await waitAndTypeX(page, data.xpaths.register.emailLabel[currentService], payload.username);
        await waitAndTypeX(page, data.xpaths.register.passwordLabel[currentService], payload.password);
        await waitAndTypeX(page, data.xpaths.register.confirmPasswordLabel[currentService], payload.password);
        await waitAndClickX(page, data.xpaths.register.sendCodeButton[currentService]);

        let code = "123123";
        //let code = await getCodeByAPI(..., payload.emailApiKey, ...);
        await waitAndTypeX(page, data.xpaths.register.codeLabel[currentService], code);
        await waitAndClickX(page, data.xpaths.register.signUpButton[currentService]);

        // тут логика для получения ошибки о том что такая учетка уже создана или возвращение ok = true

        /*const navPromise = page.waitForNavigation({ waitUntil: 'networkidle2', timeout: timeWait })
            .then(() => 'nav')
            .catch(() => 'nav_timeout');

        const badPromise = ctx.page.waitForXPath(data.xpaths.incorrectPassMessage[currentService], {timeout: timeWait})
            .then(() => 'bad')
            .catch(() => 'bad_timeout');

        const clickOk = await waitAndClickX(page, data.xpaths.authButton[currentService]);
        if (!clickOk) 
            return { ok: false, reason: 'click failed' };

        const winner = await Promise.race([navPromise, badPromise]);

        if (winner === 'bad') {
            return {
                ok: false,
                reason: "incorrect password or account don't reggered"
            }
        }*/

        return {
            ok: true
        }
    } catch (er) {
        return {
            ok: false
        }
    };
}

module.exports = {
    login,
    register
};
