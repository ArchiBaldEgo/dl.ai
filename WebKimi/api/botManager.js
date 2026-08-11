/**
 * Менеджер пула Puppeteer-ботов для Web Kimi.
 *
 * BotManager управляет жизненным циклом пула ботов:
 * - Создание ботов (до maxBotCount).
 * - Auto-spawn: если нет живых ботов — спавнит новый каждые 10с.
 * - Reap: каждые 5с удаляет мёртвые боты (упавший браузер/страница).
 * - acquireReadyBot(): занимает готовый бот для обработки запроса.
 * - restartAll(): автоподъём — закрытие всех ботов и спавн нового.
 *
 * Состояния бота: STARTING → READY → BUSY → READY (или FAILED / NOT_AUTORIZED).
 */

const path = require('path');

/** Состояния бота в пуле. */
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

/**
 * Обёртка над Bot (worker/bot.js) для управления состоянием в пуле.
 * Отслеживает state (STARTING/READY/BUSY/FAILED/NOT_AUTORIZED),
 * делегирует init/sendMessage/close внутреннему _impl.
 */
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

/**
 * Менеджер пула ботов: создание, поиск готовых, авто-spawn, reap мёртвых, restart.
 */
class BotManager {
    constructor({ maxBotCount, sharedHist, logger }) {
        this.maxBotCount = maxBotCount;
        this.sharedHist = sharedHist;
        this.log = logger?.log ?? (() => { });
        this.err = logger?.error ?? (() => { });

        this._createBot = loadWorkerFactory();
        this._bots = [];
        this._spawning = null;

        this._reapTimer = setInterval(() => this._reapDeadBots(), 5000);
        if (typeof this._reapTimer?.unref === 'function') this._reapTimer.unref();

        // Auto-spawn: ensure at least 1 bot is always trying to start.
        this._autoSpawnTimer = setInterval(() => this._autoSpawn(), 10000);
        if (typeof this._autoSpawnTimer?.unref === 'function') this._autoSpawnTimer.unref();
    }

    _autoSpawn() {
        if (this._spawning) return;
        const aliveCount = this._bots.filter(b => b.state !== BotState.FAILED && b.state !== BotState.NOT_AUTORIZED).length;
        if (aliveCount === 0 && this.canSpawn()) {
            this.log('[pool] auto-spawn: no alive bots, spawning new one');
            this.ensureSpawnIfNeeded();
        }
    }

    _reapDeadBots() {
        const before = this._bots.length;
        this._bots = this._bots.filter((b) => {
            // Don't touch bots that are still starting up — their browser/page
            // simply hasn't been created yet, so isAlive() would return false
            // and we'd kill a healthy bot mid-init.
            if (b.state === BotState.STARTING) return true;

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

        // Стабильный slot-id: наименьший свободный в [1..maxBotCount], переиспользуется
        // после смерти бота. Нужен для постоянных Chrome-профилей (userDataDir в bot.js):
        // новый бот с тем же id подхватывает ту же сессию Google и не логинится заново.
        const usedIds = new Set(this._bots.map((b) => b.id));
        let id = 1;
        while (usedIds.has(id) && id <= this.maxBotCount) id++;
        if (id > this.maxBotCount) return false;

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
        if (this._autoSpawnTimer) clearInterval(this._autoSpawnTimer);
        await Promise.allSettled(this._bots.map((b) => b.close()));
    }

    async restartAll() {
        // "Автоподъём": close every bot (kills the underlying browsers) and
        // respawn a fresh one so the pool recovers from a stuck/dead state.
        // Waits for any in-flight spawn first so we don't race init() with close().
        this.log('[pool] restartAll requested');

        if (this._spawning) {
            try {
                await this._spawning;
            } catch {
                /* spawn failure is handled below by respawning */
            }
        }

        const bots = this._bots.splice(0);
        await Promise.allSettled(bots.map((b) => b.close()));
        if (bots.length) this.log(`[pool] restartAll closed ${bots.length} bot(s)`);

        // Spawn one fresh bot immediately; the rest spawn on demand.
        try {
            this.ensureSpawnIfNeeded();
        } catch (e) {
            this.err(`[pool] restartAll spawn failed: ${e?.stack || e}`);
            throw e;
        }

        return { closed: bots.length };
    }
}

module.exports = { BotManager, BotState };
