'use strict';

/**
 * Санитайзер ответов веб-пулов (DeepSeek :3000 / Kimi :3001).
 *
 * ПРОБЛЕМА: вместо текста модели пул иногда отдаёт «мусор» — служебные
 * данные страницы (гидрация Next.js/Remix: `window.HYDRATION_INIT_STATE={...}`,
 * `self.__next_f.push(...)`, `__NEXT_DATA__`). Пользователь видел такое как
 * «код регионов» — JSON-куски разметки страницы вместо ответа.
 *
 * Модуль ОБЩИЙ для воркера (worker/modules/promtps.js) и HTTP-API
 * (api/server.js): правило отсечения мусора должно быть ровно ОДНО, иначе
 * «слой защиты» разъезжается с реальным поведением пула.
 *
 * Логика намеренно эвристическая (по подстрокам-маркерам): дешёво, не тянет
 * зависимостей и одинаково работает в Node и в браузере (в браузер маркеры
 * передаются строкой — RegExp через границу Puppeteer не проходит).
 */

/**
 * Маркеры служебных данных страницы (подстроки; регистр учитывается —
 * чтобы не ловить обычные слова вроде «hydration» в ответе модели).
 */
const GARBAGE_MARKERS = [
    'window.HYDRATION_INIT_STATE',
    'window.__INITIAL_STATE__',
    'window.__APOLLO_STATE__',
    'window.__NUXT__',
    'window.__remixContext',
    '__NEXT_DATA__',
    'self.__next_f.push',
    '__next_f.push',
    '"dehydratedAt"',
    '<!--$-->',
    '<!--/$-->',
];

/**
 * Маркеры, которые считаются мусором ТОЛЬКО в JSON-контексте (текст целиком
 * похож на JSON-объект/массив). Отдельный список нужен, чтобы нормальный
 * ответ модели (например, пример JSON с ключом "queries") не отбраковывался.
 */
const GARBAGE_JSON_MARKERS = [
    '"mutations"',
    '"queries"',
    '"pageProps"',
    '"dehydrated"',
    '"preloadedState"',
    '"initialState"',
];

/**
 * Регексы-маркеры. В браузере (page.evaluate) RegExp передать нельзя —
 * тот же паттерн дублируется в promtps.js.
 */
const GARBAGE_PATTERNS = [
    // window.HYDRATION_INIT_STATE = {...} / window.__FOO__ = {...}
    /window\.[A-Z][A-Z0-9_]{2,}\s*=/,
    // self.__next_f.push([1,"..."])
    /self\.__next_f\.push/,
];

/**
 * Ключи, по которым JSON-объект опознаётся как состояние страницы (гидрация).
 */
const HYDRATION_JSON_KEYS = [
    'mutations',
    'queries',
    'dehydratedAt',
    'dehydrated',
    'pageProps',
    'preloadedState',
    'initialState',
    '__NUXT__',
    '__remixContext',
];

/**
 * Похож ли текст на JSON-кусок (целиком JSON-объект/массив)?
 * @param {string} text — текст-кандидат.
 * @returns {boolean}
 */
function looksLikeJsonBlob(text) {
    const s = String(text || '').trim();
    if (s.length < 40) return false;
    return /^[[{]/.test(s);
}

/**
 * Похож ли текст на JSON-состояние страницы (гидрация)?
 *
 * @param {string} text — текст-кандидат.
 * @returns {boolean}
 */
function looksLikeHydrationJson(text) {
    const s = String(text || '').trim();
    if (!looksLikeJsonBlob(s)) return false;
    let parsed;
    try {
        parsed = JSON.parse(s);
    } catch (_) {
        return false;
    }
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return false;
    return Object.keys(parsed).some((k) => HYDRATION_JSON_KEYS.includes(k));
}

/**
 * Диагностировать «мусор» в тексте ответа.
 *
 * @param {string} text — текст ответа (любой).
 * @returns {string|null} — причина отбраковки (короткая метка) либо null,
 *   если текст выглядит как нормальный ответ.
 */
function describeGarbage(text) {
    const raw = String(text ?? '');
    const trimmed = raw.trim();
    if (!trimmed) return 'empty';
    for (const marker of GARBAGE_MARKERS) {
        if (raw.includes(marker)) return `marker:${marker}`;
    }
    for (const re of GARBAGE_PATTERNS) {
        if (re.test(raw)) return `pattern:${re.source}`;
    }
    // JSON-контекст: маркеры состояния страницы считаются мусором только
    // здесь — в обычном ответе (пример JSON с ключом "queries") это ложь.
    if (looksLikeJsonBlob(trimmed)) {
        for (const marker of GARBAGE_JSON_MARKERS) {
            if (raw.includes(marker)) return `json-marker:${marker}`;
        }
    }
    if (looksLikeHydrationJson(trimmed)) return 'hydration-json';
    return null;
}

/**
 * Похож ли текст на мусор (служебные данные страницы / пусто)?
 *
 * @param {string} text — текст ответа.
 * @returns {boolean}
 */
function isGarbageAnswer(text) {
    return describeGarbage(text) !== null;
}

/**
 * Санитайзер ответа: чистый текст — ок, мусор — отбраковка.
 *
 * @param {string} text — текст ответа.
 * @returns {{ ok: boolean, content: string, reason: string|null }}
 */
function sanitizeAnswer(text) {
    const content = String(text ?? '');
    const reason = describeGarbage(content);
    if (reason) return { ok: false, content: '', reason };
    return { ok: true, content, reason: null };
}

module.exports = {
    GARBAGE_MARKERS,
    GARBAGE_JSON_MARKERS,
    describeGarbage,
    isGarbageAnswer,
    sanitizeAnswer,
};
