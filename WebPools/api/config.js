/**
 * Конфиг бот-пула, собранный из env.
 *
 * Имена переменных ЗАВИСЯТ от сайта и перечислены ЯВНО в site.envNames
 * (не склеиваются из префикса — так исключается опечатка вроде
 * POOL_QUEUE_WAIT_MS вместо BOT_POOL_QUEUE_WAIT_MS):
 * - DeepSeek: PORT, MAX_BOT_COUNT, RETRY_AFTER_SEC, REQUEST_TIMEOUT_MS,
 *   BOT_POOL_QUEUE_WAIT_MS, SERVICE_MODEL, HEADLESS, BOT_USERNAME/BOT_PASSWORD.
 * - Kimi: KIMI_PORT, KIMI_MAX_BOT_COUNT, KIMI_RETRY_AFTER_SEC,
 *   KIMI_REQUEST_TIMEOUT_MS, KIMI_POOL_QUEUE_WAIT_MS, KIMI_SERVICE_MODEL,
 *   KIMI_HEADLESS, KIMI_BOT_USERNAME/KIMI_BOT_PASSWORD.
 *
 * Значения по умолчанию — ровно те, что были в WebDeepseek/api/config.js и
 * WebKimi/api/config.js.
 */

const { toInt, toBool } = require('../worker/utils/env');

/**
 * Собрать конфиг пула для сайта.
 * @param {object} site — дескриптор сайта (sites/<id>.js).
 */
function buildConfig(site) {
    const names = site.envNames;
    const d = site.defaults;
    const env = (key) => process.env[names[key] ?? key];

    return {
        port: toInt(env('port'), d.port),
        maxBotCount: toInt(env('maxBotCount'), d.maxBotCount),
        retryAfterSec: toInt(env('retryAfterSec'), d.retryAfterSec),
        requestTimeoutMs: toInt(env('requestTimeoutMs'), d.requestTimeoutMs),
        // Очередь вместо мгновенного 429; должно быть ЗАВЕДОМО меньше HTTP-таймаута
        // Django-клиента (300s) — иначе клиент оборвёт соединение раньше ответа.
        queueWaitMs: toInt(env('queueWaitMs'), d.queueWaitMs),
        // worker defaults
        serviceModel: env('serviceModel') || d.serviceModel,
        headless: toBool(env('headless'), false),
        chromePath: env('chromePath') || '',
        viewport: {
            width: toInt(env('viewportW'), 800),
            height: toInt(env('viewportH'), 800),
        },
        auth: {
            username: env('botUsername') || '',
            password: env('botPassword') || '',
        },
    };
}

module.exports = { buildConfig };
