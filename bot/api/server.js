const express = require('express');
const { makeChatCompletion, makeOpenAIError } = require('./openaiFormat');

function withTimeout(promise, ms) {
  return new Promise((resolve, reject) => {
    const t = setTimeout(() => reject(new Error('Request timeout')), ms);
    promise.then(
      (v) => {
        clearTimeout(t);
        resolve(v);
      },
      (e) => {
        clearTimeout(t);
        reject(e);
      }
    );
  });
}

function extractLastUserMessage(messages) {
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    if (m && m.role === 'user') return m.content;
  }
  return '';
}

function getConversationId(req, body) {
  return body?.conversation_id || req.header('x-conversation-id') || body?.user_id || 'default';
}

function createServer({ botManager, config, logger }) {
  const log = logger?.log ?? (() => {});
  const err = logger?.error ?? (() => {});

  const app = express();
  app.use(express.json({ limit: '2mb' }));

  app.get('/health', (req, res) => {
    // Report only actually live bots (closed window/page/browser should disappear from /health).
    res.json({ ok: true, bots: botManager.list({ onlyAlive: true, includeNotAutorized: false }) });
  });

  // Minimal OpenAI-like endpoint
  app.post('/v1/chat/completions', async (req, res) => {
    const body = req.body || {};
    const model = body.model || 'gpt-4o';
    const messages = Array.isArray(body.messages) ? body.messages : null;
    if (!messages) {
      return res.status(400).json(
        makeOpenAIError({
          message: "Invalid request: 'messages' must be an array",
          code: 'invalid_request',
          type: 'invalid_request_error',
        })
      );
    }

    let bot = botManager.acquireReadyBot();
    if (!bot) {
      if (botManager.hasNotAutorized()) {
        return res.status(401).json(
          makeOpenAIError({
            message: 'Bot is not authorized (login failed). Check BOT_USERNAME/BOT_PASSWORD.',
            code: 'not_autorized',
            type: 'invalid_request_error',
          })
        );
      }
      const spawning = botManager.ensureSpawnIfNeeded();
      res.set('Retry-After', String(config.retryAfterSec));
      if (spawning) {
        return res.status(503).json(
          makeOpenAIError({
            message: `No bots ready. Starting a bot. Retry after ~${config.retryAfterSec}s.`,
            code: 'bot_starting',
            type: 'server_error',
          })
        );
      }
      return res.status(429).json(
        makeOpenAIError({
          message: `All bots are busy (max_bot_count=${config.maxBotCount}). Retry after ~${config.retryAfterSec}s.`,
          code: 'bots_busy',
          type: 'rate_limit_error',
        })
      );
    }

    const conversationId = getConversationId(req, body);
    const lastUserMessage = extractLastUserMessage(messages);

    try {
      const payload = {
        mode: 'openai',
        conversationId,
        model: body.model,
        thinking: !!body.thinking,
        message: lastUserMessage,
        raw: body,
      };

      const raw = await withTimeout(bot.sendMessage(payload), config.requestTimeoutMs);
      const content = typeof raw === 'string' ? raw : raw?.content ?? raw?.text ?? '';
      const usage = typeof raw === 'object' ? raw?.usage : undefined;

      return res.json(makeChatCompletion({ model, content, usage }));
    } catch (e) {
      err(`[bot#${bot.id}] request failed: ${e?.stack || e}`);
      if (e?.code === 'not_autorized') {
        return res.status(401).json(
          makeOpenAIError({
            message: 'Bot authorization failed. Check BOT_USERNAME/BOT_PASSWORD.',
            code: 'not_autorized',
            type: 'invalid_request_error',
          })
        );
      }
      return res.status(500).json(
        makeOpenAIError({
          message: e?.message || 'Internal error',
          code: 'internal_error',
          type: 'server_error',
        })
      );
    } finally {
      bot.markReady();
    }
  });

  // Simple compact endpoint, closer to your worker payload
  app.post('/api/send', async (req, res) => {
    const body = req.body || {};
    const message = body.message;
    const model = body.model;
    if (!message || !model) {
      return res.status(400).json({ ok: false, reason: 'message and model are required' });
    }

    let bot = botManager.acquireReadyBot();
    if (!bot) {
      if (botManager.hasNotAutorized()) {
        return res.status(401).json({
          ok: false,
          reason: 'not_autorized',
          message: 'Bot is not authorized (login failed). Check BOT_USERNAME/BOT_PASSWORD.',
        });
      }
      const spawning = botManager.ensureSpawnIfNeeded();
      res.set('Retry-After', String(config.retryAfterSec));
      return res.status(spawning ? 503 : 429).json({
        ok: false,
        reason: spawning
          ? `No bots ready. Starting a bot. Retry after ~${config.retryAfterSec}s.`
          : `All bots are busy (max_bot_count=${config.maxBotCount}). Retry after ~${config.retryAfterSec}s.`,
      });
    }

    const conversationId = getConversationId(req, body);

    try {
      const payload = {
        mode: 'simple',
        conversationId,
        model: body.model,
        thinking: !!body.thinking,
        message: String(body.message),
        raw: body,
      };
      const out = await withTimeout(bot.sendMessage(payload), config.requestTimeoutMs);
      const content = typeof out === 'string' ? out : out?.content ?? out?.text ?? '';
      res.json({ ok: true, data: { content } });
    } catch (e) {
      err(`[bot#${bot.id}] request failed: ${e?.stack || e}`);
      if (e?.code === 'not_autorized') {
        return res.status(401).json({
          ok: false,
          reason: 'not_autorized',
          message: 'Bot authorization failed. Check BOT_USERNAME/BOT_PASSWORD.',
        });
      }
      res.status(500).json({ ok: false, reason: e?.message || 'Internal error' });
    } finally {
      bot.markReady();
    }
  });

  app.use((req, res) => {
    res.status(404).json({ ok: false, reason: 'not_found' });
  });

  app.on('error', (e) => err(e));
  log('server configured');

  return app;
}

module.exports = { createServer };
