'use strict';

/**
 * Entrypoint бот-пула (общий для DeepSeek и Kimi).
 *
 * Запуск одного сайта:
 *   node api/index.js --site=deepseek
 *   node api/index.js --site=kimi
 *
 * Запуск обоих сайтов в ОДНОМ процессе (по умолчанию, если --site не задан):
 *   node api/index.js
 *
 * Каждый сайт поднимает свой Express-сервер на своём порту (:3000 DeepSeek,
 * :3001 Kimi) и ведёт СВОЙ пул ботов, СВОИ Chrome-профили и СВОЙ лог-файл.
 * Разделение достигается тем, что модули состояния (история, логгер) — НЕ
 * глобальные: logger получает файл от сайта, а история ключуется по id сайта.
 */

const { getSite, siteIds } = require('../sites');
const { buildConfig } = require('./config');
const { BotManager } = require('./botManager');
const { createServer } = require('./server');
const { setLogFile } = require('../worker/utils/logger');

// shared history across ALL bots (same reference as worker/modules)
const { hist } = require('../worker/hist');

/**
 * Разбор аргумента --site=<id>. Пусто → все сайты.
 */
function parseSiteArg(argv = process.argv.slice(2)) {
    const arg = argv.find((a) => a.startsWith('--site='));
    if (!arg) return null;
    return arg.slice('--site='.length).trim().toLowerCase();
}

/**
 * Поднять пул одного сайта.
 * @param {string} siteId — 'deepseek' | 'kimi'.
 * @returns {Promise<object>} — { server, botManager }.
 */
async function startSite(siteId) {
    const site = getSite(siteId);
    // Каждый сайт пишет в СВОЙ лог-файл (как и до объединения пулов).
    setLogFile(site.logFile);

    const logger = {
        log: (...a) => console.log(`[api:${site.id}]`, ...a),
        error: (...a) => console.error(`[api:${site.id}]`, ...a),
    };

    const config = buildConfig(site);

    const botManager = new BotManager({
        maxBotCount: config.maxBotCount,
        sharedHist: hist,
        logger,
        site,
    });

    const app = createServer({ botManager, config, logger, site });
    // Bind to loopback only: the bot pool is an internal service consumed by
    // Django over localhost. It must never be reachable from public networks.
    const server = app.listen(config.port, '127.0.0.1', () => {
        logger.log(`listening on 127.0.0.1:${config.port}`);
        logger.log(`MAX_BOT_COUNT=${config.maxBotCount}`);
    });

    return { site, server, botManager };
}

async function main() {
    const requested = parseSiteArg();
    const ids = requested ? [requested] : siteIds();

    const started = [];
    for (const id of ids) {
        started.push(await startSite(id));
    }

    const shutdown = async (signal) => {
        for (const { server, botManager, site } of started) {
            console.log(`[api:${site.id}] ${signal} received, shutting down...`);
            server.close(() => { });
            await botManager.shutdown();
        }
        process.exit(0);
    };

    process.on('SIGINT', () => shutdown('SIGINT'));
    process.on('SIGTERM', () => shutdown('SIGTERM'));
}

main().catch((e) => {
    console.error('[api] fatal:', e?.stack || e);
    process.exit(1);
});
