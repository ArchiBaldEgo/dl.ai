'use strict';

/**
 * Реестр сайтов бот-пулов.
 *
 * Каждый сайт — самодостаточный дескриптор (sites/<id>.js): конфиг пула,
 * селекторы, сценарий входа, site-специфичные хуки отправки сообщения.
 * Общий код (api/, worker/) не знает ни про DeepSeek, ни про Kimi — только
 * про этот контракт.
 */

const deepseek = require('./deepseek');
const kimi = require('./kimi');

const SITES = {
    [deepseek.id]: deepseek,
    [kimi.id]: kimi,
};

/**
 * Найти дескриптор сайта по id.
 * @param {string} id — 'deepseek' | 'kimi'.
 */
function getSite(id) {
    const site = SITES[String(id || '').toLowerCase()];
    if (!site) {
        throw new Error(`Unknown site: ${id}. Known: ${Object.keys(SITES).join(', ')}`);
    }
    return site;
}

/**
 * Список id всех сайтов (порядок = порядок запуска в CMD).
 */
function siteIds() {
    return Object.keys(SITES);
}

module.exports = { getSite, siteIds, SITES };
