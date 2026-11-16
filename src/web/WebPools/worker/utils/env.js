/**
 * Общие хелперы парсинга env для общего кода бот-пулов (WebPools/).
 *
 * Единственный источник toInt/toBool/cleanEnvStr для всего пакета: и api/
 * (config.js), и worker/ (bot.js, modules/*) живут в ОДНОМ Node-процессе
 * (botManager подключает worker/index.js через require, а не spawn), поэтому
 * копии хелперов больше не нужны.
 */

/**
 * Разбор целого числа из env. Нечисловое значение → def.
 */
function toInt(v, def) {
    const n = parseInt(String(v ?? ''), 10);
    return Number.isFinite(n) ? n : def;
}

/**
 * Разбор булева значения из env ('1'/'true'/'yes'/'y'/'on' → true,
 * '0'/'false'/'no'/'n'/'off' → false). Пустое/неизвестное → def.
 */
function toBool(v, def) {
    if (v === undefined || v === null || v === '') return def;
    const s = String(v).toLowerCase().trim();
    if (['1', 'true', 'yes', 'y', 'on'].includes(s)) return true;
    if (['0', 'false', 'no', 'n', 'off'].includes(s)) return false;
    return def;
}

/**
 * Чистка env-строки: trim + снятие обрамляющих кавычек (docker-compose
 * env_file иногда сохраняет кавычки как часть значения).
 */
function cleanEnvStr(v) {
    if (v === undefined || v === null) return '';
    let s = String(v).trim();
    if ((s.startsWith('"') && s.endsWith('"')) || (s.startsWith("'") && s.endsWith("'"))) {
        s = s.slice(1, -1);
    }
    return s.trim();
}

module.exports = { toInt, toBool, cleanEnvStr };
