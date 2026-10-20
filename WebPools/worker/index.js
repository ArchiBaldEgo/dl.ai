const { Bot } = require('./bot');

/**
 * Фабрика бота для BotManager.
 * @param {object} params — { id, hist, site }.
 */
function createBot({ id, hist, site }) {
    void hist;
    return new Bot({ id, site });
}

module.exports = { createBot };
