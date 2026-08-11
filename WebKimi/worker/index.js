const { Bot } = require('./bot');

function createBot({ id, hist }) {
    void hist;
    return new Bot({ id });
}

module.exports = { createBot };
