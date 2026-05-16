'use strict';

const fs = require('fs');
const path = require('path');

const puppeteerExtra = require('puppeteer-extra');
const StealthPlugin = require('puppeteer-extra-plugin-stealth');

const { log, error } = require('./utils/logger');
const { login } = require('./modules/auth');
const { sendMessage: sendMessageViaUi } = require('./modules/promtps');
const data = require('./data.json');

// avoid double-registering stealth plugins when multiple bots are created
let stealthApplied = false;
function ensureStealth() {
    if (stealthApplied) return;
    puppeteerExtra.use(StealthPlugin());
    stealthApplied = true;
}

// function resolveChromePath() {
//     const env = process.env.CHROME_PATH;
//     if (env && fs.existsSync(env)) return env;

//     const portable = path.resolve(__dirname, 'chr', 'chrome.exe');
//     if (fs.existsSync(portable)) return portable;

//     return '';
// }

function toBool(v, def) {
    if (v === undefined || v === null || v === '') return def;
    const s = String(v).toLowerCase().trim();
    if (['1', 'true', 'yes', 'y', 'on'].includes(s)) return true;
    if (['0', 'false', 'no', 'n', 'off'].includes(s)) return false;
    return def;
}

function getViewport() {
    const w = parseInt(process.env.VIEWPORT_W || '800', 10);
    const h = parseInt(process.env.VIEWPORT_H || '800', 10);
    return {
        width: Number.isFinite(w) ? w : 800,
        height: Number.isFinite(h) ? h : 800,
    };
}

function cleanEnvStr(v) {
    if (v === undefined || v === null) return '';
    let s = String(v).trim();
    // Docker/Compose env_file sometimes keeps surrounding quotes; strip once.
    if ((s.startsWith('"') && s.endsWith('"')) || (s.startsWith("'") && s.endsWith("'"))) {
        s = s.slice(1, -1);
    }
    return s.trim();
}

function getProxyConfig() {
    const noProxy = toBool(process.env.PUPPETEER_NO_PROXY, false);
    const proxyServer = cleanEnvStr(process.env.PUPPETEER_PROXY_SERVER);
    const proxyUsername = cleanEnvStr(process.env.PUPPETEER_PROXY_USERNAME);
    const proxyPassword = cleanEnvStr(process.env.PUPPETEER_PROXY_PASSWORD);
    return { noProxy, proxyServer, proxyUsername, proxyPassword };
}

class Bot {
    constructor({ id }) {
        this.id = id;
        this.browser = null;
        this.page = null;
        this._closed = false;
        this._isLoggedIn = false;
        this._disconnected = false;
    }

    static NotAuthorizedError = class NotAuthorizedError extends Error {
        constructor(message) {
            super(message);
            this.name = 'NotAuthorizedError';

            this.code = 'not_autorized';
        }
    };

    async _login() {
        const model = process.env.SERVICE_MODEL || 'deepseek';
        const username = cleanEnvStr(process.env.BOT_USERNAME);
        const password = cleanEnvStr(process.env.BOT_PASSWORD);
        if (!username || !password) {
            throw new Error('BOT_USERNAME/BOT_PASSWORD are required for bot login');
        }

        const ctx = { browser: this.browser, page: this.page };

        log(`[bot#${this.id}] login start (model=${model})`);
        const res = await login(ctx, { model, username, password });
        if (!res?.ok) {
            this._isLoggedIn = false;

            await this.close();
            throw new Bot.NotAuthorizedError(`login failed: ${res?.reason || 'unknown'}`);
        }
        this._isLoggedIn = true;
        log(`[bot#${this.id}] login ok`);
    }

    async init() {
        ensureStealth();

        const headless = "new";//toBool(process.env.HEADLESS, false);
        //const executablePath = resolveChromePath();

        const proxy = getProxyConfig();
        const proxyServer = proxy.noProxy ? '' : proxy.proxyServer;

        const launchOpts = {
            headless,
            args: [
				...(headless ? ['--disable-gpu'] : []),
                ...(proxy.noProxy ? ['--no-proxy-server'] : []),
                ...(proxyServer ? [`--proxy-server=${proxyServer}`] : []),
                '--disable-extensions',
                '--disable-default-apps',
                '--disable-component-update',
                '--disable-sync',
                '--disable-translate',
                '--disable-notifications',
                '--no-first-run',
                '--no-default-browser-check',
                '--disable-background-networking',
                '--disable-backgrounding-occluded-windows',
                '--disable-renderer-backgrounding',
                '--disable-features=BackForwardCache',
                '--disk-cache-size=1',
                '--media-cache-size=1',
                '--mute-audio',
				'--no-sandbox', //для докер
				'--disable-setuid-sandbox', //для докер
				'--disable-dev-shm-usage' //для докер
            ] // for minimize ram usage
        };
        //if (executablePath) launchOpts.executablePath = executablePath;

        this.browser = await puppeteerExtra.launch(launchOpts);
        this.browser.on('disconnected', () => {

            this._disconnected = true;
            this.page = null;
            this.browser = null;
            if (!this._closed) error(`[bot#${this.id}] browser disconnected`);
        });

        const pages = await this.browser.pages().catch(() => []);
        this.page = pages[0] ?? (await this.browser.newPage());
        if (proxyServer && (proxy.proxyUsername || proxy.proxyPassword)) {
            await this.page.authenticate({
                username: proxy.proxyUsername,
                password: proxy.proxyPassword,
            });
        }
        await this.page.setViewport(getViewport()).catch(() => { });

        this._attachPageEvents(this.page);

        // Login at bot start (required by your lifecycle)
        await this._login();

        const model = process.env.SERVICE_MODEL || 'deepseek';
        log(`[bot#${this.id}] init ok (model=${model})`);
    }

    isAlive() {
        // If puppeteer got a disconnect event, this bot is dead.
        if (this._disconnected) return false;
        const b = this.browser;
        if (!b) return false;

        // If the user closed the visible window/tab, the Page will be closed even if Chrome keeps running
        // (e.g. background apps setting). Such a bot is unusable, so treat it as dead.
        const p = this.page;
        if (!p) return false;
        try {
            if (typeof p.isClosed === 'function' && p.isClosed()) return false;
        } catch {
            // ignore
        }

        // Puppeteer Browser has isConnected() in most versions.
        try {
            if (typeof b.isConnected === 'function') return !!b.isConnected();
        } catch {
            // ignore
        }
        return true;
    }

    _attachPageEvents(p) {
        p.on('pageerror', (err) => error(`[bot#${this.id}] pageerror: ${err?.stack || err}`));
        p.on('error', (err) => error(`[bot#${this.id}] page error: ${err?.stack || err}`));
        p.on('close', () => {
            error(`[bot#${this.id}] page closed`);
            // If page was closed manually, consider bot dead and let API reap it.
            if (!this._closed) {
                this._disconnected = true;
                // Best-effort close to avoid leaving a background Chrome process.
                void this.close().catch(() => { });
            }
        });
    }

    async sendMessage(payload) {
        if (!this.page) throw new Error('bot page is not initialized');

        // Safety: if session expired or init didn't finish properly, enforce login before messaging.
        if (!this._isLoggedIn) {
            await this._login();
        }

        const ctx = { browser: this.browser, page: this.page };

        // Map API payload -> worker UI payload
        // API может прислать model в стиле OpenAI (например "gpt-4o").
        // Для UI-бота нам нужен ключ сервиса из worker/data.json (например "deepseek").
        const requested = payload?.serviceModel || payload?.model || process.env.SERVICE_MODEL || 'deepseek';
        const model = (data?.services && data.services[requested]) ? requested : (process.env.SERVICE_MODEL || 'deepseek');
        const uid = payload?.conversationId ?? payload?.raw?.user_id ?? 'default';
        const message = String(payload?.message ?? '');
        const thinking = !!payload?.thinking;

        const out = await sendMessageViaUi(ctx, {
            user_id: uid,
            message,
            model,
            thinking,
        });

        if (typeof out === 'object' && out && out.ok === false) {
            throw new Error(out.reason || 'sendMessage failed');
        }
        return out;
    }

    async close() {
        this._closed = true;
        this._disconnected = true;
        try {
            await this.browser?.close();
        } catch {
            // ignore
        } finally {
            this.browser = null;
            this.page = null;
        }
    }
}

module.exports = { Bot };
