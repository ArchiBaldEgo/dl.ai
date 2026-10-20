'use strict';

/**
 * Puppeteer-бот для взаимодействия с веб-сайтом через браузерную автоматизацию.
 *
 * Общий класс для обоих пулов (DeepSeek :3000 и Kimi :3001). Различия сайта
 * приходят через дескриптор sites/<id>.js: env-имена, профиль, вход, отправка.
 *
 * Класс Bot управляет жизненным циклом Puppeteer-сессии:
 * - init(): запуск Chrome (с stealth-плагином, опциональным прокси), логин на сайте.
 * - sendMessage(): отправка сообщения через UI сайта (worker/modules/promtps.js).
 * - close(): закрытие браузера и прокси.
 *
 * Поддерживает авто-восстановление: при закрытии страницы создаётся новая и
 * выполняется повторный логин. Определяет отключение браузера через _disconnected.
 *
 * Используется WebPools/api/server.js (HTTP API) и WebPools/api/botManager.js
 * (управление пулом ботов).
 */

const fs = require('fs');
const path = require('path');

const proxyChain = require('proxy-chain');

const puppeteerExtra = require('puppeteer-extra');
const StealthPlugin = require('puppeteer-extra-plugin-stealth');

const { log, error } = require('./utils/logger');
const { toBool, cleanEnvStr } = require('./utils/env');
const { sleep } = require('./utils/helpers');
const { sendMessage: sendMessageViaUi } = require('./modules/promtps');

/**
 * Постоянные Chrome-профили: хранят сессию сайта, чтобы не логиниться заново
 * при каждом перезапуске бота. Каталог можно переопределить через
 * BOT_PROFILE_DIR (DeepSeek) / KIMI_BOT_PROFILE_DIR (Kimi).
 *
 * ВАЖНО: профили DeepSeek и Kimi РАЗНЫЕ каталоги (site.profileRoot) — сессия
 * Google (DeepSeek) и сессия Kimi не должны попадать в один user-data-dir.
 */
function getProfileDir(id, site) {
    const overrideName = site.envPrefix
        ? `${site.envPrefix}BOT_PROFILE_DIR`
        : 'BOT_PROFILE_DIR';
    const root = cleanEnvStr(process.env[overrideName]) || site.profileRoot;
    return path.join(root, `bot-${id}`);
}

// При жёстком падении (OOM-kill) Chrome оставляет SingletonLock/SingletonCookie/
// SingletonSocket — новый Chrome не сможет захватить профиль. Чистим их перед запуском.
function cleanStaleLocks(profileDir) {
    for (const name of ['SingletonLock', 'SingletonCookie', 'SingletonSocket']) {
        try { fs.rmSync(path.join(profileDir, name), { force: true }); } catch (_) {}
    }
}

// avoid double-registering stealth plugins when multiple bots are created
let stealthApplied = false;
function ensureStealth() {
    if (stealthApplied) return;
    puppeteerExtra.use(StealthPlugin());
    stealthApplied = true;
}

function getViewport(site) {
    const prefix = site.envPrefix;
    const w = parseInt(process.env[prefix ? `${prefix}VIEWPORT_W` : 'VIEWPORT_W'] || '800', 10);
    const h = parseInt(process.env[prefix ? `${prefix}VIEWPORT_H` : 'VIEWPORT_H'] || '800', 10);
    return {
        width: Number.isFinite(w) ? w : 800,
        height: Number.isFinite(h) ? h : 800,
    };
}

/**
 * Прокси бота. Сайт-специфичные имена (BOT_PROXY* / KIMI_BOT_PROXY*) имеют
 * приоритет; если они пусты — используются общие PUPPETEER_PROXY_* (те же
 * имена, что и в корневом .env).
 */
function resolveProxy(site) {
    const p = site.envPrefix;
    let server = cleanEnvStr(process.env[p ? `${p}BOT_PROXY` : 'BOT_PROXY']);
    let user = cleanEnvStr(process.env[p ? `${p}BOT_PROXY_USER` : 'BOT_PROXY_USER']);
    let pass = cleanEnvStr(process.env[p ? `${p}BOT_PROXY_PASS` : 'BOT_PROXY_PASS']);
    if (!server) {
        // Names already used in the root .env for Puppeteer/Chromium.
        server = cleanEnvStr(process.env.PUPPETEER_PROXY_SERVER);
        user = cleanEnvStr(process.env.PUPPETEER_PROXY_USERNAME);
        pass = cleanEnvStr(process.env.PUPPETEER_PROXY_PASSWORD);
    }
    return { server, user, pass };
}

class Bot {
    /**
     * Puppeteer-бот для одного сеанса взаимодействия с сайтом.
     * Управляет браузером, страницей, логином и отправкой сообщений.
     */
    constructor({ id, site }) {
        this.id = id;
        this.site = site;
        this.browser = null;
        this.page = null;
        this._closed = false;
        this._isLoggedIn = false;
        this._disconnected = false;
        // Флаг краша рендерера ('Page crashed!'): страница-объект puppeteer при
        // этом НЕ закрывается (page.isClosed() === false — #closed ставится только
        // при target-destroy), поэтому isAlive() возвращал бы true и пул держал
        // бы зомби-бота, а все запросы падали с 429/500. По флагу бот считается
        // мёртвым (reap) и/или страница пересоздаётся в _ensurePage() прямо
        // внутри текущего запроса.
        this._pageCrashed = false;
    }

    static NotAuthorizedError = class NotAuthorizedError extends Error {
        constructor(message) {
            super(message);
            this.name = 'NotAuthorizedError';

            this.code = 'not_autorized';
        }
    };

    /** Имя env-переменной сайта: KIMI_SERVICE_MODEL для Kimi, SERVICE_MODEL для DeepSeek. */
    _env(name) {
        return this.site.envPrefix ? `${this.site.envPrefix}${name}` : name;
    }

    async _login() {
        const model = process.env[this._env('SERVICE_MODEL')] || this.site.defaults.serviceModel;
        const username = cleanEnvStr(process.env[this._env('BOT_USERNAME')]);
        const password = cleanEnvStr(process.env[this._env('BOT_PASSWORD')]);

        const ctx = { browser: this.browser, page: this.page };

        log(`[bot#${this.id}] login start (model=${model})`);
        // Вход в Kimi НЕ автоматизируется (телефон/WeChat/email): auth.login()
        // лишь диагностирует отсутствие сессии и инструктирует пересидировать
        // профиль — см. sites/kimi.js.
        const res = await this.site.auth.login(ctx, { model, username, password });
        if (!res?.ok) {
            this._isLoggedIn = false;

            await this.close();
            throw new Bot.NotAuthorizedError(`login failed: ${res?.reason || 'unknown'}`);
        }
        this._isLoggedIn = true;
        log(`[bot#${this.id}] login ok`);
    }

    async _ensureAuthorized() {
        // Сначала проверяем сохранённую сессию (постоянный профиль), при неудаче —
        // полный вход (DeepSeek) либо диагностика (Kimi). Используется и при
        // старте, и при восстановлении страницы: новые вкладки в том же профиле
        // уже имеют сессию сайта.
        const model = process.env[this._env('SERVICE_MODEL')] || this.site.defaults.serviceModel;
        const ctx = { browser: this.browser, page: this.page };
        const alreadyAuth = await this.site.auth.checkAlreadyAuthorized(ctx, { model }).catch(() => false);
        if (alreadyAuth) {
            this._isLoggedIn = true;
            log(`[bot#${this.id}] сессия сохранена в профиле — вход без повторного логина`);
            return;
        }
        log(`[bot#${this.id}] сессия отсутствует — выполняем вход`);
        await this._login();
    }

    async init() {
        // Инициализация: запуск Chrome, настройка stealth-плагина, прокси, логин.
        ensureStealth();

        const headless = toBool(process.env[this._env('HEADLESS')], false) ? 'new' : false;
        const { server: proxyServerRaw, user: proxyUser, pass: proxyPass } = resolveProxy(this.site);
        let proxyServer = proxyServerRaw;
        log(`[bot#${this.id}] proxy configured: ${proxyServer ? 'yes' : 'no'}`);
        if (proxyServer && proxyUser) {
            const host = proxyServer.replace(/^https?:\/\//, '');
            const upstream = `http://${encodeURIComponent(proxyUser)}:${encodeURIComponent(proxyPass)}@${host}`;
            this._localProxy = await proxyChain.anonymizeProxy(upstream);
            proxyServer = this._localProxy;
            log(`[bot#${this.id}] anonymized proxy: ${proxyServer}`);
        }

        // Постоянный профиль: сессия сайта переживает рестарт бота.
        const profileDir = getProfileDir(this.id, this.site);
        try { fs.mkdirSync(profileDir, { recursive: true }); } catch (_) {}
        cleanStaleLocks(profileDir);
        log(`[bot#${this.id}] chrome profile: ${profileDir}`);

        const launchOpts = {
            headless,
            protocolTimeout: 180000, // 3 min — prevents Input.insertText/dispatchKeyEvent timeouts on slow pages
            userDataDir: profileDir,
            args: [
                ...(proxyServer ? [`--proxy-server=${proxyServer}`] : []),
                ...(headless ? ['--disable-gpu'] : []),
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
        log(`[bot#${this.id}] launching chrome with args: ${launchOpts.args.join(' ')}`);

        this.browser = await puppeteerExtra.launch(launchOpts);
        this.browser.on('disconnected', () => {

            this._disconnected = true;
            this.page = null;
            this.browser = null;
            if (!this._closed) error(`[bot#${this.id}] browser disconnected`);
        });

        const pages = await this.browser.pages().catch(() => []);
        this.page = pages[0] ?? (await this.browser.newPage());
        await this.page.setViewport(getViewport(this.site)).catch(() => { });

        this._attachPageEvents(this.page);

        // Вход: либо по сохранённой сессии профиля, либо полный логин сайта.
        await this._ensureAuthorized();

        const model = process.env[this._env('SERVICE_MODEL')] || this.site.defaults.serviceModel;
        log(`[bot#${this.id}] init ok (model=${model})`);
    }

    isAlive() {
        // Проверка жив ли бот: браузер подключён, страница не закрыта.
        // Если puppeteer получил disconnect — бот мёртв.
        if (this._disconnected) return false;
        // Краш рендерера делает страницу нерабочей, формально «открытой»:
        // считаем такой бот мёртвым (пул заберёт его reap'ом и заспавнит нового).
        if (this._pageCrashed) return false;
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
        p.on('error', (err) => {
            error(`[bot#${this.id}] page error: ${err?.stack || err}`);
            // 'error' у Page = краш таргета (renderer OOM и т.п.). Страница при
            // этом «не закрыта» для puppeteer: помечаем краш и роняем ссылку,
            // чтобы isAlive() стал false (reap) либо _ensurePage() собрал новую
            // страницу прямо в текущем запросе.
            const msg = String(err?.message || err || '');
            if (/Page crashed|Target closed|Session closed|Target.{0,20}detached/i.test(msg)) {
                this._pageCrashed = true;
                this.page = null;
            }
        });
        p.on('close', () => {
            error(`[bot#${this.id}] page closed`);
            if (!this._closed) {
                this._disconnected = true;
                this._pageCrashed = true;
                void this.close().catch(() => {});
            }
        });
    }

    async _ensurePage() {
        // If page was closed/crashed, create a fresh one and re-login.
        if (!this.browser) return;
        if (this._pageCrashed || !this.page || (typeof this.page.isClosed === 'function' && this.page.isClosed())) {
            log(`[bot#${this.id}] page lost/crashed, creating fresh page and re-login`);
            // ВСЕГДА новая вкладка: browser.pages()[0] после краша может вернуть
            // сам крашнутый таргет (он ещё числится живым) — переиспользовать его нельзя.
            const oldPage = this.page;
            this.page = null;
            try { await oldPage?.close().catch(() => {}); } catch { /* ignore */ }
            this.page = await this.browser.newPage();
            await this.page.setViewport(getViewport(this.site)).catch(() => {});
            this._attachPageEvents(this.page);
            this._pageCrashed = false;
            this._isLoggedIn = false;
            await this._ensureAuthorized();
        }
    }

    async sendMessage(payload) {
        // Отправка сообщения через UI сайта.
        // Авто-восстановление: если страница закрыта/упала — создаётся новая и выполняется повторный логин.
        if (!this.browser) throw new Error('bot browser is not initialized');

        // Auto-recovery: if page was closed/crashed, recreate and re-login.
        // (после краша this.page === null — _ensurePage соберёт новую вкладку и
        // отлогинится по сохранённой сессии профиля, запрос при этом НЕ падает)
        await this._ensurePage();
        if (!this.page) throw new Error('bot page is not initialized');

        // Safety: if session expired or init didn't finish properly, enforce login before messaging.
        if (!this._isLoggedIn) {
            await this._ensureAuthorized();
        }

        const ctx = { browser: this.browser, page: this.page };

        // Map API payload -> worker UI payload
        // API может прислать model в стиле OpenAI (например "gpt-4o").
        // Для UI-бота нам нужен ключ сервиса из site.data.services.
        const serviceModelEnv = process.env[this._env('SERVICE_MODEL')] || this.site.defaults.serviceModel;
        const requested = payload?.serviceModel || payload?.model || serviceModelEnv;
        const model = (this.site.data?.services && this.site.data.services[requested]) ? requested : serviceModelEnv;
        const uid = payload?.conversationId ?? payload?.raw?.user_id ?? 'default';
        const message = String(payload?.message ?? '');
        const thinking = !!payload?.thinking;

        const out = await sendMessageViaUi(ctx, {
            user_id: uid,
            message,
            model,
            thinking,
        }, this.site);

        if (typeof out === 'object' && out && out.ok === false) {
            // Поверх reason докидываем data.moreInformation — в нём живёт диагноз
            // ("All answer XPath selectors failed — DeepSeek UI may have changed"),
            // который Django-клиент ищет, чтобы быстро сдаться при смене вёрстки
            // сайта (иначе 3×300с бессмысленных retry-ов). Сервер проксирует
            // e.message в { reason }, так что строка доходит до Django.
            const detail = out?.data?.moreInformation;
            const detailStr = detail instanceof Error
                ? `${detail.name}: ${detail.message}`
                : (detail != null ? String(detail) : '');
            throw new Error(
                detailStr
                    ? `${out.reason || 'sendMessage failed'}: ${detailStr}`
                    : (out.reason || 'sendMessage failed')
            );
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

        if (this._localProxy) {
            await proxyChain.closeAnonymizedProxy(this._localProxy, true).catch(() => {});
            this._localProxy = null;
        }
    }
}

module.exports = { Bot };
