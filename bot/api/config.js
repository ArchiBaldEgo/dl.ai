function toInt(v, def) {
    const n = parseInt(String(v ?? ''), 10);
    return Number.isFinite(n) ? n : def;
}

function toBool(v, def) {
    if (v === undefined || v === null || v === '') return def;
    const s = String(v).toLowerCase().trim();
    if (['1', 'true', 'yes', 'y', 'on'].includes(s)) return true;
    if (['0', 'false', 'no', 'n', 'off'].includes(s)) return false;
    return def;
}

module.exports = {
    port: toInt(process.env.PORT, 3000),
    maxBotCount: toInt(process.env.MAX_BOT_COUNT, 3),
    retryAfterSec: toInt(process.env.RETRY_AFTER_SEC, 3),
    requestTimeoutMs: toInt(process.env.REQUEST_TIMEOUT_MS, 180_000),
    // worker defaults
    serviceModel: process.env.SERVICE_MODEL || 'deepseek',
    headless: toBool(process.env.HEADLESS, false),
    chromePath: process.env.CHROME_PATH || '',
    viewport: {
        width: toInt(process.env.VIEWPORT_W, 800),
        height: toInt(process.env.VIEWPORT_H, 800),
    },
    auth: {
        username: process.env.BOT_USERNAME || '',
        password: process.env.BOT_PASSWORD || '',
    },
};
