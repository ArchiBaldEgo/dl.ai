const config = require('./config');
const { BotManager } = require('./botManager');
const { createServer } = require('./server');

const logger = {
    log: (...a) => console.log('[api]', ...a),
    error: (...a) => console.error('[api]', ...a),
};

// shared history across ALL bots (same reference as worker/modules)
const { hist } = require('../worker/hist');

async function main() {
    const botManager = new BotManager({
        maxBotCount: config.maxBotCount,
        sharedHist: hist,
        logger,
    });

    const app = createServer({ botManager, config, logger });
    // Bind to loopback only: the bot pool is an internal service consumed by
    // Django over localhost. It must never be reachable from public networks.
    const server = app.listen(config.port, '127.0.0.1', () => {
        logger.log(`listening on 127.0.0.1:${config.port}`);
        logger.log(`MAX_BOT_COUNT=${config.maxBotCount}`);
    });

    const shutdown = async (signal) => {
        logger.log(`${signal} received, shutting down...`);
        server.close(() => { });
        await botManager.shutdown();
        process.exit(0);
    };

    process.on('SIGINT', () => shutdown('SIGINT'));
    process.on('SIGTERM', () => shutdown('SIGTERM'));
}

main().catch((e) => {
    logger.error(e?.stack || e);
    process.exit(1);
});
