/**
 * Общие хелперы парсинга env для воркер-процесса (worker/bot.js, worker/modules/auth.js).
 *
 * ВНИМАНИЕ: api/config.js имеет собственные копии toInt/toBool — api и worker
 * запускаются как РАЗНЫЕ Node-процессы (botManager спавнит worker/index.js),
 * поэтому общий require между ними не используется намеренно.
 */

// Имя сервиса по умолчанию (ключ в worker/data.json). Не хардкодьте 'kimi'
// по коду — берите отсюда, чтобы менять в одном месте.
const DEFAULT_SERVICE = 'kimi';

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

module.exports = { toInt, toBool, DEFAULT_SERVICE };