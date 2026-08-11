const fs = require('fs');
const path = require('path');

const data = require('../data.json');

const { error } = require('../utils/logger');
const {
    waitForXPathCompat,
} = require('../core/page-utils');
const { toBool } = require('../utils/env');

const LOG_DIR = path.join(__dirname, '../logs');

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

/**
 * Быстрый путь: если в постоянном профиле уже есть живая сессия, то после
 * открытия kimi.moonshot.cn сразу появится поле ввода чата — логин не нужен.
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

module.exports = {
    login,
    checkAlreadyAuthorized,
};