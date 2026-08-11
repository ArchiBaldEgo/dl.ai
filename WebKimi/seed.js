'use strict';

/**
 * Одноразовый ручной засев Chrome-профиля для WebKimi (login НЕ автоматизируется —
 * Kimi входит по телефону/SMS, WeChat QR или email, что требует ручного шага).
 *
 * Скрипт открывает ВИДИМЫЙ Chromium с тем же userDataDir, что использует бот слота
 * <id> (по умолчанию bot-1), переходит на kimi.moonshot.cn и держит окно открытым,
 * пока оператор не войдёт вручную. Появление поля ввода чата
 * ([data-testid='msh-chatinput-editor']) = вход завершён → скрипт выжидает пару
 * секунд (куки устаканиваются), закрывает браузер и выходит 0. Дальше бот (даже
 * headless) подхватывает сессию через checkAlreadyAuthorized() — повторный логин
 * не нужен.
 *
 * Почему отдельный скрипт, а не «просто запустить бот с KIMI_HEADLESS=false»:
 * modules/auth.js::login() при отсутствии сессии сразу возвращает {ok:false} и
 * bot.js::_login() ЗАКРЫВАЕТ браузер — окна для ручного входа не остаётся.
 * Этот скрипт обходит init()/login() и держит окно до подтверждения.
 *
 * Запуск (локально, нужен дисплей):
 *   cd WebKimi
 *   node seed.js            # сидирует слот 1 (bot-1)
 *   node seed.js --id=2     # сидирует слот 2 (bot-2)
 *
 * Через прокси (корпоративное окружение): переменные KIMI_BOT_PROXY* или
 * PUPPETEER_PROXY_* читаются автоматически — тот же сетевой путь, что у бота.
 * Таймаут ожидания входа: KIMI_SEED_TIMEOUT_MS (дефолт 900000 = 15 мин).
 *
 * После засева — проверить headless-подъём:
 *   KIMI_HEADLESS=true node api/index.js   # в другом терминале
 *   curl -s http://localhost:3001/health  # -> {"ok":true,"ready_count":1,...}
 */

const fs = require('fs');
const path = require('path');

const proxyChain = require('proxy-chain');
const puppeteerExtra = require('puppeteer-extra');
const StealthPlugin = require('puppeteer-extra-plugin-stealth');

const CHAT_URL = 'https://www.kimi.com/';
// Поле ввода чата Kimi = contenteditable div. Старый селектор [data-testid='msh-chatinput-editor']
// устарел (Kimi убрал data-testid) — реальный класс поля: .chat-input-editor (role=textbox).
const INPUT_CSS = ".chat-input-editor[role='textbox']";

function cleanEnvStr(v) {
    if (v === undefined || v === null) return '';
    let s = String(v).trim();
    if ((s.startsWith('"') && s.endsWith('"')) || (s.startsWith("'") && s.endsWith("'"))) {
        s = s.slice(1, -1);
    }
    return s.trim();
}

function getProfileDir(id) {
    const root = cleanEnvStr(process.env.KIMI_BOT_PROFILE_DIR)
        || path.join(__dirname, 'worker', '.chrome-profiles');
    return path.join(root, `bot-${id}`);
}

function cleanStaleLocks(profileDir) {
    for (const name of ['SingletonLock', 'SingletonCookie', 'SingletonSocket']) {
        try { fs.rmSync(path.join(profileDir, name), { force: true }); } catch (_) {}
    }
}

function resolveProxy() {
    let server = cleanEnvStr(process.env.KIMI_BOT_PROXY);
    let user = cleanEnvStr(process.env.KIMI_BOT_PROXY_USER);
    let pass = cleanEnvStr(process.env.KIMI_BOT_PROXY_PASS);
    if (!server) {
        server = cleanEnvStr(process.env.PUPPETEER_PROXY_SERVER);
        user = cleanEnvStr(process.env.PUPPETEER_PROXY_USERNAME);
        pass = cleanEnvStr(process.env.PUPPETEER_PROXY_PASSWORD);
    }
    return { server, user, pass };
}

function getViewport() {
    const w = parseInt(process.env.KIMI_VIEWPORT_W || '800', 10);
    const h = parseInt(process.env.KIMI_VIEWPORT_H || '800', 10);
    return {
        width: Number.isFinite(w) ? w : 800,
        height: Number.isFinite(h) ? h : 800,
    };
}

function getSeedTimeoutMs() {
    const raw = Number(process.env.KIMI_SEED_TIMEOUT_MS || 900000);
    return Number.isFinite(raw) && raw > 0 ? raw : 900000;
}

function parseSlotId() {
    // --id=N (дефолт 1)
    const arg = process.argv.find((a) => a.startsWith('--id='));
    if (arg) {
        const n = parseInt(arg.slice('--id='.length), 10);
        if (Number.isFinite(n) && n > 0) return n;
    }
    return 1;
}

function isDiagMode() {
    return process.argv.includes('--diag');
}

async function isInputVisible(page) {
    try {
        const el = await page.$(INPUT_CSS);
        if (!el) return false;
        const box = await el.boundingBox().catch(() => null);
        return !!(box && box.width > 0 && box.height > 0);
    } catch (_) {
        return false;
    }
}

// Диагностический дамп DOM: помогает подобрать актуальный селектор поля ввода
// Kimi, когда ожидаемый [data-testid='msh-chatinput-editor'] не находится
// (Kimi сменил вёрстку). Логирует URL, title, все data-testid на странице и
// кандидатов поля ввода (contenteditable / textarea).
async function dumpDiagnostics(page) {
    try {
        const url = page.url();
        const title = await page.title().catch(() => '?');
        const info = await page.evaluate(() => {
            const out = { testids: [], contentEditables: [], textareas: [], buttons: [], containers: [], loginHints: [] };
            const seen = new Set();
            for (const el of document.querySelectorAll('[data-testid]')) {
                const v = el.getAttribute('data-testid');
                if (v && !seen.has(v)) { seen.add(v); out.testids.push(v); }
                if (out.testids.length >= 60) break;
            }
            for (const el of document.querySelectorAll('[contenteditable="true"]')) {
                if (out.contentEditables.length >= 10) break;
                out.contentEditables.push({
                    tag: el.tagName.toLowerCase(),
                    testid: el.getAttribute('data-testid') || '',
                    cls: (el.className || '').toString().slice(0, 80),
                    role: el.getAttribute('role') || '',
                });
            }
            for (const el of document.querySelectorAll('textarea')) {
                if (out.textareas.length >= 10) break;
                out.textareas.push({
                    testid: el.getAttribute('data-testid') || '',
                    cls: (el.className || '').toString().slice(0, 80),
                    ph: el.getAttribute('placeholder') || '',
                });
            }
            // Кнопки — для селектора «отправить» / «stop».
            for (const el of document.querySelectorAll('button')) {
                if (out.buttons.length >= 30) break;
                const r = el.getBoundingClientRect();
                if (r.width < 5 || r.height < 5) continue; // скрытые
                out.buttons.push({
                    cls: (el.className || '').toString().slice(0, 80),
                    testid: el.getAttribute('data-testid') || '',
                    aria: el.getAttribute('aria-label') || '',
                    text: (el.innerText || '').trim().slice(0, 30),
                    type: el.getAttribute('type') || '',
                });
            }
            // Message/ответ-контейнеры: ищем по классу/роли, характерным для чата Kimi.
            const re = /message|markdown|conversation|answer|response|chat-content|chat-body|chat-message|chat-item|msg-|agent|reply|bubble|prose|content-render|virtuoso|list-item/i;
            for (const el of document.querySelectorAll('div, section, article')) {
                if (out.containers.length >= 40) break;
                const cls = (el.className || '').toString();
                if (!re.test(cls)) continue;
                const r = el.getBoundingClientRect();
                if (r.width < 50 || r.height < 20) continue;
                out.containers.push({
                    tag: el.tagName.toLowerCase(),
                    cls: cls.slice(0, 100),
                    role: el.getAttribute('role') || '',
                    testid: el.getAttribute('data-testid') || '',
                    kids: el.children.length,
                });
            }
            const t = (document.body?.innerText || '').slice(0, 500).toLowerCase();
            for (const kw of ['войти', 'sign in', 'log in', '登录', 'wechat', 'телефон', 'phone', '扫码', '二维码']) {
                if (t.includes(kw)) out.loginHints.push(kw);
            }
            return out;
        }).catch(() => null);
        console.log(`[diag] url=${url}`);
        console.log(`[diag] title=${title}`);
        if (info) {
            console.log(`[diag] data-testid (${info.testids.length}): ${info.testids.join(', ') || '—'}`);
            if (info.contentEditables.length) {
                console.log('[diag] contenteditable:');
                for (const c of info.contentEditables) {
                    console.log(`       <${c.tag} class="${c.cls}" role="${c.role}">`);
                }
            }
            if (info.buttons.length) {
                console.log(`[diag] buttons (${info.buttons.length}):`);
                for (const b of info.buttons) {
                    console.log(`       <button class="${b.cls}" aria="${b.aria}" text="${b.text}" type="${b.type}">`);
                }
            }
            if (info.containers.length) {
                console.log(`[diag] message/answer containers (${info.containers.length}):`);
                for (const c of info.containers) {
                    console.log(`       <${c.tag} class="${c.cls}" role="${c.role}" kids=${c.kids}>`);
                }
            }
            if (info.loginHints.length) {
                console.log(`[diag] login-подсказки на странице: ${info.loginHints.join(', ')} (вход ещё НЕ завершён)`);
            }
        } else {
            console.log('[diag] не удалось прочитать DOM');
        }
    } catch (e) {
        console.log(`[diag] ошибка: ${e?.message || e}`);
    }
}

async function main() {
    const slotId = parseSlotId();
    const profileDir = getProfileDir(slotId);
    const timeoutMs = getSeedTimeoutMs();
    const diagMode = isDiagMode();

    try { fs.mkdirSync(profileDir, { recursive: true }); } catch (_) {}
    cleanStaleLocks(profileDir);

    console.log(`[seed] слот bot-${slotId}`);
    console.log(`[seed] профиль: ${profileDir}`);
    console.log(`[seed] таймаут ожидания входа: ${Math.round(timeoutMs / 1000)}с`);
    if (diagMode) console.log('[seed] режим --diag: окно НЕ закроется само по полю ввода — для калибровки селекторов. Закрой окно руками, когда закончишь.');

    puppeteerExtra.use(StealthPlugin());

    const { server: proxyServerRaw, user: proxyUser, pass: proxyPass } = resolveProxy();
    let proxyServer = proxyServerRaw;
    let localProxy = null;
    if (proxyServer && proxyUser) {
        const host = proxyServer.replace(/^https?:\/\//, '');
        const upstream = `http://${encodeURIComponent(proxyUser)}:${encodeURIComponent(proxyPass)}@${host}`;
        localProxy = await proxyChain.anonymizeProxy(upstream);
        proxyServer = localProxy;
        console.log(`[seed] прокси: anonymized (${proxyServerRaw})`);
    } else if (proxyServer) {
        console.log(`[seed] прокси: ${proxyServer} (без авторизации)`);
    } else {
        console.log('[seed] прокси: нет (прямое соединение)');
    }

    const launchOpts = {
        headless: false, // ВИДИМЫЙ — весь смысл засева
        protocolTimeout: 180000,
        userDataDir: profileDir,
        args: [
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
            '--no-sandbox',
            '--disable-setuid-sandbox',
            '--disable-dev-shm-usage',
        ],
    };

    const browser = await puppeteerExtra.launch(launchOpts);
    let settled = false;

    browser.on('disconnected', () => {
        if (!settled) console.log('[seed] браузер закрыт (или упал).');
    });

    const pages = await browser.pages().catch(() => []);
    const page = pages[0] ?? (await browser.newPage());
    await page.setViewport(getViewport()).catch(() => {});

    console.log(`[seed] открываю ${CHAT_URL} ...`);
    await page.goto(CHAT_URL, { waitUntil: 'domcontentloaded', timeout: 60000 }).catch((e) => {
        console.log(`[seed] goto: ${e?.message || e} — продолжаю, окно открыто для ручного входа`);
    });

    // Дать SPA Kimi 4с на отрисовку, потом первый дамп DOM.
    await new Promise((r) => setTimeout(r, 4000));
    await dumpDiagnostics(page);

    console.log('[seed] ✓ окно Chromium открыто. Войди в Kimi вручную (телефон/SMS, WeChat QR или email).');
    console.log('[seed]   скрипт сам закроется, когда увидит поле чата после входа.');

    const start = Date.now();
    let seenOnce = false;
    let lastDiagAt = -1;
    while (Date.now() - start < timeoutMs) {
        if (!browser.isConnected()) {
            // Оператор закрыл окно.
            if (seenOnce) {
                console.log('[seed] окно закрыто после того, как поле чата успело появиться — кука, скорее всего, сохранена.');
            } else {
                console.log('[seed] ✗ окно закрыто до завершения входа — профиль НЕ сидирован. Повтори запуск.');
            }
            await cleanupProxy(localProxy);
            process.exit(seenOnce ? 0 : 1);
        }

        if (await isInputVisible(page)) {
            seenOnce = true;
            if (!diagMode) {
                const elapsed = Math.round((Date.now() - start) / 1000);
                console.log(`[seed] ✓ поле чата появилось (${elapsed}с) — вход подтверждён. Сохраняю куку...`);
                // Дадим браузеру пару секунд досохранить cookies/session в профиль.
                await new Promise((r) => setTimeout(r, 5000));
                settled = true;
                break;
            }
            // diag-режим: не закрываем, продолжаем 20с-дампы — оператор калибрует
            // селекторы (шлёт сообщение, ловит контейнер ответа).
        }

        const elapsed = Math.round((Date.now() - start) / 1000);
        // Каждые 20с — свежий дамп DOM (страница меняется по мере входа/чата).
        if (elapsed > 0 && elapsed % 20 === 0 && elapsed !== lastDiagAt) {
            lastDiagAt = elapsed;
            console.log(`[seed] жду входа... ${elapsed}с / ${Math.round(timeoutMs / 1000)}с`);
            await dumpDiagnostics(page);
        } else if (elapsed % 10 === 0 && elapsed > 0) {
            console.log(`[seed] жду входа... ${elapsed}с / ${Math.round(timeoutMs / 1000)}с`);
        }
        await new Promise((r) => setTimeout(r, 2000));
    }

    if (!settled) {
        console.log(`[seed] ✗ таймаут ${Math.round(timeoutMs / 1000)}с — вход не завершён. Профиль НЕ сидирован. Повтори запуск.`);
        await browser.close().catch(() => {});
        await cleanupProxy(localProxy);
        process.exit(1);
    }

    console.log('[seed] закрываю браузер...');
    await browser.close().catch(() => {});
    await cleanupProxy(localProxy);

    console.log('');
    console.log('[seed] ✓✓ профиль засеян:', profileDir);
    console.log('[seed] теперь запусти бот headless и проверь /health:');
    console.log('       KIMI_HEADLESS=true node api/index.js');
    console.log('       curl -s http://localhost:3001/health');
    process.exit(0);
}

async function cleanupProxy(localProxy) {
    if (localProxy) {
        await proxyChain.closeAnonymizedProxy(localProxy, true).catch(() => {});
    }
}

main().catch((e) => {
    console.error('[seed] fatal:', e?.stack || e);
    process.exit(1);
});