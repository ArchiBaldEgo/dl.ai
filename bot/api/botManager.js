const path = require('path');

const BotState = Object.freeze({
    STARTING: 'starting',
    READY: 'ready',
    BUSY: 'busy',
    NOT_AUTORIZED: 'not_autorized',
    FAILED: 'failed',
});

function loadWorkerFactory() {
    const workerEntrypoint = path.resolve(__dirname, '..', 'worker', 'index.js');
    const mod = require(workerEntrypoint);
    if (!mod || typeof mod.createBot !== 'function') {
        throw new Error(`worker/index.js must export createBot(...). Got: ${Object.keys(mod || {})}`);
    }
    return mod.createBot;
}

class BotWrapper {
    constructor({ id, createBot, sharedHist }) {
        this.id = id;
        this.state = BotState.STARTING;
        this._impl = createBot({ id, hist: sharedHist });
    }

    isAlive() {
        try {
            if (typeof this._impl?.isAlive === 'function') return !!this._impl.isAlive();
            return true;
        } catch {
            return false;
        }
    }

    async init() {
        try {
            await this._impl.init();
            this.state = BotState.READY;
        } catch (e) {
            if (e?.code === BotState.NOT_AUTORIZED) {
                this.state = BotState.NOT_AUTORIZED;
                await this.close().catch(() => { });
            } else {
                this.state = BotState.FAILED;
                await this.close().catch(() => { });
            }
            throw e;
        }
    }

    async sendMessage(payload) {
        this.state = BotState.BUSY;
        try {
            return await this._impl.sendMessage(payload);
        } catch (e) {
            if (e?.code === BotState.NOT_AUTORIZED) {
                this.state = BotState.NOT_AUTORIZED;
                await this.close().catch(() => { });
            }
            throw e;
        }
    }

    markReady() {
        if (this.state !== BotState.FAILED && this.state !== BotState.NOT_AUTORIZED && this.isAlive()) {
            this.state = BotState.READY;
        }
    }

    markNotAutorized() {
        this.state = BotState.NOT_AUTORIZED;
    }

    markFailed() {
        this.state = BotState.FAILED;
    }

    async close() {
        if (typeof this._impl.close === 'function') {
            await this._impl.close();
        }
    }
}

class BotManager {
    constructor({ maxBotCount, sharedHist, logger }) {
        this.maxBotCount = maxBotCount;
        this.sharedHist = sharedHist;
        this.log = logger?.log ?? (() => { });
        this.err = logger?.error ?? (() => { });

        this._createBot = loadWorkerFactory();
        this._bots = [];
        this._nextId = 1;
        this._spawning = null;

        this._reapTimer = setInterval(() => this._reapDeadBots(), 5000);
        if (typeof this._reapTimer?.unref === 'function') this._reapTimer.unref();
    }

    _reapDeadBots() {
        const before = this._bots.length;
        this._bots = this._bots.filter((b) => {
            // Keep NOT_AUTORIZED entries so the API can return 401 and surface the reason.
            if (b.state === BotState.NOT_AUTORIZED) return true;

            // Drop failed bots.
            if (b.state === BotState.FAILED) return false;

            // Drop bots whose underlying browser/page is gone (e.g. user closed the window).
            if (!b.isAlive()) {
                this.err(`[bot#${b.id}] detected dead browser/page. Removing from pool.`);
                b.close().catch(() => { });
                return false;
            }
            return true;
        });
        const after = this._bots.length;
        if (after !== before) this.log(`[pool] reaped ${before - after} dead bot(s)`);
    }

    list(opts = {}) {
        const onlyAlive = !!opts.onlyAlive;
        const includeNotAutorized = opts.includeNotAutorized !== false;

        this._reapDeadBots();

        return this._bots
            .filter((b) => {
                if (!includeNotAutorized && b.state === BotState.NOT_AUTORIZED) return false;
                if (onlyAlive && !b.isAlive()) return false;
                return true;
            })
            .map((b) => ({ id: b.id, state: b.state }));
    }

    hasNotAutorized() {
        return this._bots.some((b) => b.state === BotState.NOT_AUTORIZED);
    }

    getNotAutorizedBots() {
        return this._bots
            .filter((b) => b.state === BotState.NOT_AUTORIZED)
            .map((b) => ({ id: b.id, state: b.state }));
    }

    acquireReadyBot() {
        this._reapDeadBots();
        const bot = this._bots.find((b) => b.state === BotState.READY);
        if (!bot) return null;
        bot.state = BotState.BUSY; // reserve
        return bot;
    }

    canSpawn() {
        this._reapDeadBots();
        return this._bots.length < this.maxBotCount;
    }

    ensureSpawnIfNeeded() {
        this._reapDeadBots();
        if (!this.canSpawn()) return false;
        if (this._spawning) return true;

        const id = this._nextId++;
        const bot = new BotWrapper({ id, createBot: this._createBot, sharedHist: this.sharedHist });
        this._bots.push(bot);

        this._spawning = bot
            .init()
            .then(() => this.log(`[bot#${id}] ready`))
            .catch((e) => {
                if (e?.code === BotState.NOT_AUTORIZED || bot.state === BotState.NOT_AUTORIZED) {
                    bot.markNotAutorized();
                    this.err(`[bot#${id}] not authorized: ${e?.message || e}`);

                    return;
                }

                bot.markFailed();
                this._bots = this._bots.filter((x) => x !== bot);
                this.err(`[bot#${id}] init failed: ${e?.stack || e}`);
            })
            .finally(() => {
                this._spawning = null;
            });

        return true;
    }

    async shutdown() {
        if (this._reapTimer) clearInterval(this._reapTimer);
        await Promise.allSettled(this._bots.map((b) => b.close()));
    }
}

module.exports = { BotManager, BotState };
