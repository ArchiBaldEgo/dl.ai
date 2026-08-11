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
    port: toInt(process.env.KIMI_PORT, 3001),
    maxBotCount: toInt(process.env.KIMI_MAX_BOT_COUNT, 3),
    retryAfterSec: toInt(process.env.KIMI_RETRY_AFTER_SEC, 3),
    requestTimeoutMs: toInt(process.env.KIMI_REQUEST_TIMEOUT_MS, 300_000),
    // worker defaults
    serviceModel: process.env.KIMI_SERVICE_MODEL || 'kimi',
    headless: toBool(process.env.KIMI_HEADLESS, false),
    chromePath: process.env.KIMI_CHROME_PATH || '',
    viewport: {
        width: toInt(process.env.KIMI_VIEWPORT_W, 800),
        height: toInt(process.env.KIMI_VIEWPORT_H, 800),
    },
    // Вход в Kimi НЕ автоматизируется (телефон/WeChat/email). Сессия сидируется
    // вручную один раз в постоянный Chrome-профиль (KIMI_HEADLESS=false + дисплей),
    // после чего checkAlreadyAuthorized переиспользует сессионную куку.
    // KIMI_BOT_USERNAME/PASSWORD оставлены для полноты, но login() их не использует.
    auth: {
        username: process.env.KIMI_BOT_USERNAME || '',
        password: process.env.KIMI_BOT_PASSWORD || '',
    },
};