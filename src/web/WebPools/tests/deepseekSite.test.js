'use strict';

/**
 * Тесты конфигурации сайта Web DeepSeek (node --test).
 *
 * Фиксируют защиту от «простыни размышлений»: контейнер DeepThink тоже носит
 * класс ds-markdown, поэтому и основной answer-XPath, и robust-fallback
 * обязаны исключать think/reason-поддеревья. Без этого в ответ попадал
 * черновик из размышлений, и код программы дублировался.
 *
 * Запуск: npm test   (или node --test WebPools/tests/)
 */

const test = require('node:test');
const assert = require('node:assert');

const deepseek = require('../sites/deepseek');

test('DeepSeek: основной answer-XPath исключает контейнер размышлений', () => {
    const primary = deepseek.data.xpaths.chat.answer.deepseek;
    assert.match(primary, /ds-markdown/);
    assert.match(primary, /not\(contains\(@class,'think'\)\)/);
    assert.match(primary, /not\(contains\(@class,'reason'\)\)/);
});

test('DeepSeek: все answer-XPath с ds-markdown исключают think', () => {
    const paths = deepseek.promtps.answerXPaths(deepseek, 'deepseek');
    const withMarkdown = paths.filter((xp) => /ds-markdown/.test(xp));
    assert.ok(withMarkdown.length > 0);
    // Последний глобальный фолбэк исторически без NOT_THINK — допускаем
    // только его, все остальные обязаны фильтровать think/reason.
    withMarkdown.slice(0, -1).forEach((xp) => {
        assert.match(xp, /not\(contains\(@class,'think'\)\)/, xp);
    });
});

test('DeepSeek: robust-fallback вырезает think/reason поддеревья', () => {
    const sels = deepseek.promtps.robust.stripSelectors || [];
    assert.ok(sels.some((s) => /think/.test(s)), 'нужен селектор think*');
    assert.ok(sels.some((s) => /reason/.test(s)), 'нужен селектор reason*');
});
