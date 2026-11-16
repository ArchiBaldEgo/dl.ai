'use strict';

/**
 * Тесты конфигурации сайта Web Kimi (node --test).
 *
 * Kimi переделал чат (октябрь 2026): Lexical-редактор вместо contenteditable
 * с paste-инжектом, кнопка отправки send-button-container, ответ разбит на
 * segment-content-box (текст + карточка кода), размышления — в toolcall-*,
 * а код рендерится highlight.js'ом (внутри <pre><code> — span'ы подсветки).
 *
 * Эти тесты фиксируют новые селекторы и главное — что из ответа выходит
 * ЧИСТЫЙ код, без разметки подсветки.
 *
 * Запуск: npm test   (или node --test WebPools/tests/)
 */

const test = require('node:test');
const assert = require('node:assert');

const kimi = require('../sites/kimi');

// Реалистичный кусок ответа Kimi в НОВОЙ вёрстке:
// segment-content-box → (toolcall-размышления) + markdown-текст + карточка кода
// с подсветкой highlight.js внутри <pre><code>.
const KIMI_ANSWER_HTML = `
<div class="segment-content-box">
  <div class="toolcall-container thinking-container">
    <div class="toolcall-title-container-text">Размышления</div>
    <div class="toolcall-content-text">секретные размышления модели</div>
  </div>
  <div class="markdown-container">
    <div class="markdown"><p>Вот решение:</p><ul><li>первый шаг</li></ul></div>
  </div>
  <div class="segment-code">
    <div class="segment-code-header">
      <div class="segment-code-header-content"><span class="segment-code-lang">Python</span></div>
    </div>
    <div class="syntax-highlighter">
      <div class="segment-code-content">
        <pre class="language-python"><code class="language-python"><span class="hljs-keyword">def</span> <span class="hljs-title function_">solve</span><span class="hljs-params">():</span>
    <span class="hljs-keyword">return</span> <span class="hljs-number">42</span></code></pre>
      </div>
    </div>
  </div>
</div>`;

test('Kimi: ответ достаётся из segment-content-box, а не из одного markdown-блока', () => {
    const [primary] = kimi.promtps.answerXPaths(kimi, 'kimi');
    assert.match(primary, /chat-content-item-assistant/);
    assert.match(primary, /segment-content-box/);
    // Старый селектор брал только последний markdown-блок (терялся текст).
    assert.ok(
        kimi.promtps.answerXPaths(kimi, 'kimi').some((xp) => /segment-content-box/.test(xp)),
        'в списке селекторов должен быть segment-content-box'
    );
});

test('Kimi: поле ввода — Lexical-редактор, кнопка отправки — send-button-container', () => {
    assert.match(kimi.data.xpaths.chat.inputLabel.kimi, /chat-input-editor/);
    assert.match(kimi.data.xpaths.chat.inputLabelCss.kimi, /chat-input-editor/);
    assert.match(kimi.data.xpaths.chat.sendMessageButton.kimi, /send-button-container/);
    // Кнопка Stop = та же кнопка с модификатором 'stop'.
    assert.ok(
        kimi.promtps.stopXPaths.some((xp) => /send-button-container/.test(xp) && /stop/.test(xp)),
        'в stopXPaths должен быть индикатор генерации по send-button-container.stop'
    );
});

test('Kimi: размышления и вызовы инструментов вырезаются из ответа', () => {
    const sels = kimi.promtps.robust.stripSelectors;
    assert.ok(sels.some((s) => /toolcall-/.test(s)), 'нужен селектор toolcall-*');
    assert.ok(sels.some((s) => /think/.test(s)), 'нужен селектор think*');
    // Рассуждения не должны попасть в ответ целиком.
    assert.match(kimi.promtps.robust.extraThinkClass, /toolcall-/);
});

test('Kimi: код извлекается БЕЗ разметки подсветки highlight.js', () => {
    const md = kimi.promtps.htmlToMarkdown(KIMI_ANSWER_HTML, kimi);
    assert.match(md, /```python/);
    assert.match(md, /def solve\(\):/);
    assert.match(md, /return 42/);
    // Разметка подсветки в «код» попадать не должна.
    assert.ok(!/hljs/.test(md), 'в ответе не должно быть тегов подсветки');
    assert.ok(!/<span/.test(md), 'в ответе не должно быть сырых тегов');
});

test('Kimi: текст ответа сохраняется, а не только код', () => {
    const md = kimi.promtps.htmlToMarkdown(KIMI_ANSWER_HTML, kimi);
    assert.match(md, /Вот решение:/);
    assert.match(md, /первый шаг/);
});

test('Kimi: гость (сессии нет) не считается авторизованным', async () => {
    // Kimi показывает редактор и гостю — раньше этого хватало, чтобы пул
    // считал гостя авторизованным и падал на каждом запросе.
    const guestPage = {
        goto: async () => {},
        waitForXPath: async () => ({ dispose: async () => {} }),
        evaluate: async () => true, // найден маркер гостя (.not-login-icon и т.п.)
    };
    const asGuest = await kimi.auth.checkAlreadyAuthorized({ page: guestPage }, { model: 'kimi' });
    assert.equal(asGuest, false, 'гость не должен считаться авторизованным');

    const userPage = {
        goto: async () => {},
        waitForXPath: async () => ({ dispose: async () => {} }),
        evaluate: async () => false, // маркеров гостя нет
    };
    const asUser = await kimi.auth.checkAlreadyAuthorized({ page: userPage }, { model: 'kimi' });
    assert.equal(asUser, true, 'живая сессия должна считаться авторизованной');
});
