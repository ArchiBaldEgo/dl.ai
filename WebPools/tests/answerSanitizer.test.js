'use strict';

/**
 * Тесты санитайзера ответов веб-пулов (node --test).
 *
 * Покрывают ровно тот баг, из-за которого юзер получал «код регионов»:
 * вместо ответа модели пул отдавал служебные данные страницы (гидрация
 * Next.js/Remix), а конвертер HTML→Markdown оставлял содержимое <script>.
 *
 * Запуск: npm test   (или node --test WebPools/tests/)
 */

const test = require('node:test');
const assert = require('node:assert');

const {
    sanitizeAnswer,
    isGarbageAnswer,
    describeGarbage,
} = require('../worker/modules/answerSanitizer');
const { htmlToMarkdown } = require('../worker/modules/promtps');

// Минимальный pre-экстрактор, как в sites/kimi.js.
const PRE_EXTRACTORS = [
    {
        pattern: /<pre\b[^>]*>\s*<code\b([^>]*)>([\s\S]*?)<\/code>\s*<\/pre>/gi,
        extract: (_match, codeAttrs, codeInner) => {
            const langMatch = String(codeAttrs).match(/class\s*=\s*"[^"]*language-([^"\s]+)[^"]*"/i);
            const lang = langMatch ? langMatch[1].trim().toLowerCase() : '';
            const code = String(codeInner)
                .replace(/&lt;/g, '<')
                .replace(/&gt;/g, '>')
                .replace(/&amp;/g, '&')
                .replace(/&quot;/g, '"')
                .trim();
            return { lang, code };
        },
    },
];

const toMarkdown = (html) => htmlToMarkdown(html, {
    stripOuterDiv: (h) => String(h || '').trim(),
    preExtractors: PRE_EXTRACTORS,
});

test('санитайзер отбрасывает window.HYDRATION_INIT_STATE', () => {
    const junk = 'window.HYDRATION_INIT_STATE={"mutations":[],"queries":[{"dehydratedAt":1788301366911}]}';
    assert.ok(isGarbageAnswer(junk));
    assert.ok(describeGarbage(junk).startsWith('marker:'));
    const result = sanitizeAnswer(junk);
    assert.strictEqual(result.ok, false);
    assert.strictEqual(result.content, '');
});

test('санитайзер отбрасывает __NEXT_DATA__ и self.__next_f.push', () => {
    assert.ok(isGarbageAnswer('<script id="__NEXT_DATA__" type="application/json">{"props":{}}</script>'));
    assert.ok(isGarbageAnswer('self.__next_f.push([1,"$","div",null,{"children":[1,2,3]}])'));
});

test('санитайзер отбрасывает JSON-состояние страницы с dehydratedAt', () => {
    const junk = JSON.stringify({ dehydratedAt: 1788301366911, props: { pageProps: {} } });
    assert.ok(isGarbageAnswer(junk));
});

test('санитайзер пропускает нормальный ответ с кодом', () => {
    const good = 'Вот решение:\n\n```asm\n.model small\n.code\nstart:\n  nop\n```';
    const result = sanitizeAnswer(good);
    assert.strictEqual(result.ok, true);
    assert.strictEqual(result.content, good);
});

test('пустой ответ считается мусором', () => {
    assert.strictEqual(describeGarbage('   \n  '), 'empty');
    assert.ok(isGarbageAnswer(''));
});

test('обычный ответ со словом "queries" не отбраковывается', () => {
    // JSON-маркеры («queries», «mutations») считаются мусором ТОЛЬКО в
    // JSON-контексте — обычный ответ модели проходит.
    const good = 'Сначала выполните queries, затем соберите результат из таблицы users.';
    assert.strictEqual(describeGarbage(good), null);
    assert.ok(!isGarbageAnswer(good));
});

test('htmlToMarkdown вырезает содержимое <script> вместе с тегом', () => {
    const html = '<div class="markdown">Ответ модели<script>window.HYDRATION_INIT_STATE={"a":1}</script></div>';
    const md = toMarkdown(html);
    assert.ok(!md.includes('HYDRATION_INIT_STATE'), 'содержимое script не должно попадать в ответ');
    assert.ok(!md.includes('<script'), 'тег script не должен попадать в ответ');
    assert.ok(md.includes('Ответ модели'));
});

test('htmlToMarkdown вырезает <style>, <noscript> и <template>', () => {
    const html = '<div class="markdown">Текст<style>.a{color:red}</style>'
        + '<noscript>Включите JS</noscript><template><p>заготовка</p></template></div>';
    const md = toMarkdown(html);
    assert.ok(!md.includes('color:red'));
    assert.ok(!md.includes('Включите JS'));
    assert.ok(!md.includes('заготовка'));
    assert.ok(md.includes('Текст'));
});

test('htmlToMarkdown сохраняет пример кода с <script> внутри <pre>', () => {
    const html = '<div class="markdown"><pre><code class="language-html">&lt;script&gt;alert(1)&lt;/script&gt;</code></pre></div>';
    const md = toMarkdown(html);
    assert.ok(md.includes('alert(1)'), 'код ответа не должен теряться при чистке script-тегов');
});

test('htmlToMarkdown разводит соседние блоки переводами строк', () => {
    // Реальный баг DeepSeek: две программы лежали в соседних <div>, шаг
    // «снести все теги» склеивал их в «jmp ends ;$Ejmp begin» — маркер конца
    // первой программы приклеивался к началу второй.
    const html = '<div class="markdown"><div>jmp ends ;$E</div><div>jmp begin</div></div>';
    const md = toMarkdown(html);
    assert.ok(!/\$Ejmp/.test(md), 'текст соседних блоков не должен слипаться');
    assert.match(md, /;\$E\n+jmp begin/);
});

test('htmlToMarkdown разводит строки таблицы и ячейки', () => {
    const html = '<table><tr><td>1</td><td>2</td></tr><tr><td>3</td><td>4</td></tr></table>';
    const md = toMarkdown(html);
    assert.ok(!/12/.test(md), 'соседние ячейки не должны слипаться');
    assert.match(md, /1\t2/);
    assert.match(md, /3\t4/);
});
