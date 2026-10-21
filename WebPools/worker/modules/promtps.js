/**
 * Общий движок отправки сообщений через UI веб-сайта (Puppeteer).
 *
 * ЕДИНАЯ реализация для обоих пулов (DeepSeek :3000 и Kimi :3001).
 * Раньше эти ~900 строк дублировались в WebDeepseek/worker/modules/promtps.js
 * и WebKimi/worker/modules/promtps.js — любой фикс (анти-выкусывание, robust
 * fallback, эхо-гейт) приходилось применять ДВАЖДЫ. Теперь Site-специфика
 * (селекторы, ввод, отправка, тексты ошибок) лежит в sites/<site>/promtps.js,
 * а общий алгоритм — здесь.
 *
 * Сайт передаётся третьим аргументом и предоставляет хуки (site.promtps):
 *   htmlToMarkdown(html)      — конвертер HTML → Markdown;
 *   answerXPaths(service)     — список XPath-селекторов ответа;
 *   stopXPaths                — селекторы кнопки «Stop» (генерация идёт);
 *   typeInput/send            — ввод текста и отправка;
 *   stable/robust/retry       — тайминги стабилизации;
 *   appearTimeoutMs(...)      — таймаут появления ответа для конкретного XPath;
 *   snapshotBeforeSend(...)   — снимок ответа ДО отправки (гвард «stale answer»);
 *   finalGuards(...)          — финальные проверки ответа;
 *   labels                    — тексты ошибок/логов сайта.
 *
 * Общие инварианты (НЕ меняются при правках сайта):
 * - эхо-гейт: ответ, полностью содержащийся в промпте, отбраковывается;
 * - анти-обрыв: незакрытый <pre> = снимок сделан посреди код-блока;
 * - robust-fallback: класс-независимый поиск пузыря ассистента;
 * - DOM-диагностика в лог, когда не сработал ни один селектор.
 */

const { log } = require('../utils/logger');

const { clickIfExists } = require('../core/page-utils');

const { GARBAGE_MARKERS, GARBAGE_JSON_MARKERS, describeGarbage } = require('./answerSanitizer');

const { sleep } = require('../utils/helpers');

const { hist } = require('../hist');

// Декодирование HTML-сущностей (&nbsp;, &amp;, &#xNN;, &#NN; и т.д.) в обычные символы.
function decodeHtmlEntities(s) {
    return String(s ?? '')
        .replace(/&nbsp;/g, ' ')
        .replace(/&apos;/g, "'")
        .replace(/&amp;/g, '&')
        .replace(/&lt;/g, '<')
        .replace(/&gt;/g, '>')
        .replace(/&quot;/g, '"')
        // hex entities
        .replace(/&#x([0-9a-fA-F]+);/g, (_, hex) => String.fromCharCode(parseInt(hex, 16)))
        // dec entities
        .replace(/&#(\d+);/g, (_, n) => String.fromCharCode(Number(n)));
}

function extractCodeFromPreInner(preInnerHtml) {
    const raw = preInnerHtml
        .replace(/<br\s*\/?>/gi, '\n')
        .replace(/<\/?[^>]+>/g, ''); // снос всех тегов подсветки
    return decodeHtmlEntities(raw).replace(/\r\n/g, '\n').trim();
}

/**
 * Вырезает ТЕГИ вместе с их СОДЕРЖИМЫМ: <script>, <style>, <noscript>,
 * <template>.
 *
 * Зачем: содержимое inline-скриптов страницы (гидрация Next.js/Remix —
 * `window.HYDRATION_INIT_STATE={...}`, `self.__next_f.push(...)`) НЕ является
 * ответом модели, но раньше попадало в текст ответа («код регионов» у юзера).
 *
 * Порядок важен: функция вызывается ПОСЛЕ извлечения code blocks (они
 * хранятся в плейсхолдерах), поэтому пример кода с тегом <script> внутри
 * ответа модели не потеряется.
 */
function stripNonContentTags(html) {
    return String(html || '')
        // Парные теги — вместе с содержимым.
        .replace(/<(script|style|noscript|template)\b[\s\S]*?<\/\1>/gi, '')
        // Незакрытый тег (хвост разметки) — режем до конца строки.
        .replace(/<(script|style|noscript|template)\b[\s\S]*$/gi, '');
}

/**
 * Общий конвертер HTML → Markdown.
 *
 * Code blocks вырезаются в плейсхолдеры (чтобы чистка тегов их не сломала),
 * затем чистятся ссылки/форматирование/заголовки/списки, потом возвращаются
 * блоки кода. Сайт передаёт СВОИ pre-экстракторы (порядок важен: у DeepSeek
 * сначала вырезается md-code-block, у Kimi — сначала generic <pre><code>).
 *
 * @param {string} html — исходный HTML ответа.
 * @param {object} opts — { stripOuterDiv, preExtractors }.
 * @returns {string} Markdown.
 */
function htmlToMarkdown(html, { stripOuterDiv, preExtractors = [] }) {
    let s = stripOuterDiv(html);

    // 1) Вырезаем code blocks в плейсхолдеры, чтобы дальнейшая чистка не сломала их
    const codeBlocks = [];

    for (const extractor of preExtractors) {
        s = s.replace(extractor.pattern, (...args) => {
            // args = [полное совпадение, ...capture-группы] — как у колбэка replace.
            const { code, lang } = extractor.extract(...args);
            const fence = lang ? `\`\`\`${lang}\n${code}\n\`\`\`` : `\`\`\`\n${code}\n\`\`\``;
            const token = `@@CODEBLOCK_${codeBlocks.length}@@`;
            codeBlocks.push(fence);
            return token;
        });
    }

    // 1б) Вырезаем служебные блоки разметки (<script>/<style>/<noscript>/
    //     <template>) ВМЕСТЕ с содержимым — иначе гидрация страницы попадает
    //     в «ответ» модели. Сделано после извлечения code blocks: пример кода
    //     с <script> внутри ответа остаётся целым.
    s = stripNonContentTags(s);

    // 2) Ссылки
    // <a href="...">text</a> -> [text](url)
    s = s.replace(/<a\b[^>]*href="([^"]+)"[^>]*>([\s\S]*?)<\/a>/gi, (_, href, inner) => {
        const text = decodeHtmlEntities(inner.replace(/<\/?[^>]+>/g, '')).trim();
        const url = decodeHtmlEntities(href).trim();
        return text ? `[${text}](${url})` : url;
    });

    // 3) Inline форматирование
    s = s.replace(/<\/?strong\b[^>]*>/gi, '**');
    s = s.replace(/<\/?b\b[^>]*>/gi, '**');
    s = s.replace(/<\/?em\b[^>]*>/gi, '*');
    s = s.replace(/<\/?i\b[^>]*>/gi, '*');

    // 4) Заголовки h1..h6
    s = s.replace(/<h([1-6])\b[^>]*>/gi, (_, lvl) => '\n' + '#'.repeat(Number(lvl)) + ' ');
    s = s.replace(/<\/h[1-6]>/gi, '\n\n');

    // 5) Переносы строк и параграфы
    s = s.replace(/<br\s*\/?>/gi, '\n');
    s = s.replace(/<p\b[^>]*>/gi, '\n');
    s = s.replace(/<\/p>/gi, '\n\n');

    // 6) Списки
    // ol start="n" учитываем минимально: превращаем в "1. "
    s = s.replace(/<ol\b[^>]*start="(\d+)"[^>]*>/gi, (_, start) => `\n@@OLSTART_${start}@@\n`);
    s = s.replace(/<ol\b[^>]*>/gi, '\n@@OLSTART_1@@\n');
    s = s.replace(/<\/ol>/gi, '\n');

    s = s.replace(/<ul\b[^>]*>/gi, '\n');
    s = s.replace(/<\/ul>/gi, '\n');

    // li: для ul -> "- ", для ol -> "n. " (упрощенно через маркер OLSTART)
    // Сначала открывающий li
    s = s.replace(/<li\b[^>]*>/gi, '\n- ');
    s = s.replace(/<\/li>/gi, '\n');

    // 7) Убираем все прочие теги (span/div/svg/etc)
    s = s.replace(/<\/?[^>]+>/g, '');

    // 8) Декод entities в обычном тексте
    s = decodeHtmlEntities(s);

    // 9) Восстанавливаем нумерацию для OL (упрощенно)
    s = s.replace(/@@OLSTART_(\d+)@@/g, '');

    // 10) Возвращаем code blocks
    for (let i = 0; i < codeBlocks.length; i++) {
        const token = `@@CODEBLOCK_${i}@@`;
        // гарантируем пустые строки вокруг блоков
        s = s.replace(token, `\n\n${codeBlocks[i]}\n\n`);
    }

    // 11) Нормализация пробелов/пустых строк
    s = s
        .replace(/[ \t]+\n/g, '\n')
        .replace(/\n{3,}/g, '\n\n')
        .trim();

    return s;
}

// === Анти-обрыв: распознавание «пузырь юзера/эхо промпта» и «снимок снят
// посреди code-блока». Общие хелперы send-пайплайна.

// Нормализация пробелов: текст из DOM пузыря (innerText/outerHTML) приходит с
// переводами строк/отступами, не совпадающими с исходным промптом.
function normalizeWs(s) {
    return String(s || '').replace(/\s+/g, ' ').trim();
}

// Ответ, ПОЛНОСТЬЮ содержащийся в тексте запроса юзера и длинный (≥60 симв.)
// — это не решение: селектор поймал пузырь юзера (робастный fallback берёт
// «последний пузырь с текстом», которым до появления ответа остаётся запрос)
// или эхо промпта моделью. Короткие ответы («2», «да») не эхо — порог 60
// символов. Логичное расширение гвардов «экзакт-равенства» ниже: частичный
// фрагмент промпта (253/615 симв. в прогоне 142 у DeepSeek) равенством не
// ловился.
function isEchoOfUserMessage(innerText, messageText) {
    const a = normalizeWs(innerText);
    const b = normalizeWs(messageText);
    if (!a || !b || a.length < 60) return false;
    return b.indexOf(a) !== -1;
}

// Незакрытый <pre> в сыром outerHTML = снимок сделан посреди код-блока:
// генерация ещё шла, код оборван посреди слова. Конвертер всегда закрывает
// ```-оградки, поэтому проверять надо ДО конвертации, в сыром HTML.
function looksCutMidCode(rawHtml) {
    const html = String(rawHtml || '');
    const opens = (html.match(/<pre\b/gi) || []).length;
    const closes = (html.match(/<\/pre>/gi) || []).length;
    return opens > closes;
}

async function getLastOuterHtmlByXPath(page, xpath) {
    return page.evaluate((xp) => {
        const result = document.evaluate(
            xp,
            document,
            null,
            XPathResult.ORDERED_NODE_SNAPSHOT_TYPE,
            null
        );
        if (!result || result.snapshotLength === 0) return '';
        const node = result.snapshotItem(result.snapshotLength - 1);
        return node?.outerHTML || '';
    }, xpath);
}

async function waitForXPathCompat(page, xpath, {
    timeoutMs = 60000,
    visible = false,
} = {}) {
    if (typeof page.waitForXPath === 'function') {
        return page.waitForXPath(xpath, { timeout: timeoutMs, visible });
    }

    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
        const ok = await page.evaluate((xp, wantVisible) => {
            const res = document.evaluate(xp, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null);
            const node = res.singleNodeValue;
            if (!node) return false;
            if (!wantVisible) return true;
            if (!node.getBoundingClientRect) return false;
            const rect = node.getBoundingClientRect();
            return !!(rect && rect.width > 0 && rect.height > 0);
        }, xpath, visible);

        if (ok) return true;
        await sleep(250);
    }

    throw new Error(`waitForXPath timeout for xpath: ${xpath}`);
}

// Проверяет, идёт ли ещё потоковая генерация (видна ли кнопка Stop).
async function isStillGenerating(page, stopXPaths) {
    try {
        for (const xp of stopXPaths) {
            const found = await page.evaluate((x) => {
                const res = document.evaluate(x, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null);
                const node = res.singleNodeValue;
                if (!node) return false;
                const rect = node.getBoundingClientRect();
                return !!(rect && rect.width > 0 && rect.height > 0);
            }, xp);
            if (found) return true;
        }
    } catch (e) { /* ignore — safer to assume still generating */ }
    return false;
}

/**
 * Ожидание стабилизации HTML-контента: опрашивает outerHTML элемента по XPath,
 * пока он не перестанет меняться stableTicks раз подряд (потоковая генерация
 * завершена).
 *
 * Опции:
 * - checkStopButton: проверяет кнопку Stop в DOM — если видна, генерация
 *   продолжается (сбрасывает sameCount даже если HTML не менялся в паузе).
 * - minContentLength=50: игнорирует пустые/короткие ответы (< 50 символов).
 * - timeoutMs=300000 (5 мин): бюджет на стабилизацию длинного ответа.
 * - appearTimeoutMs=20000 (20с): таймаут ПЕРВОГО появления элемента в DOM.
 *   Реальный ответ появляется в DOM за 1-3с, поэтому 20с — щедрый запас.
 * - stableTicks=6 × pollMs=2000 = 12 секунд стабильности для уверенности.
 * - confirmMs: ПОДТВЕРЖДЕНИЕ перечитыванием. Когда накопилось stableTicks
 *   совпадений, ждём ещё confirmMs и перечитываем — если контент вырос, значит
 *   «стабильность» была паузой в генерации, а не завершением.
 * - maxEmptyStableTicks: ФЕЙЛ-ФАСТ на «пустом скелете». Значение undefined
 *   ОТКЛЮЧАЕТ проверку (поведение DeepSeek: мёртвый XPath выжигает полное
 *   окно стабильности). Число (в т.ч. 0) ВКЛЮЧАЕТ её: после N опросов подряд
 *   пустого/текстово-пустого контейнера бросается ошибка (поведение Kimi).
 *
 * xpathOrGetter: либо строка XPath, либо async-геттер (page) => html.
 */
async function waitLastOuterHtmlStable(page, xpathOrGetter, {
    timeoutMs = 300000,
    appearTimeoutMs = 20000,
    pollMs = 2000,
    stableTicks = 6,
    visible = true,
    minContentLength = 50,
    checkStopButton = true,
    confirmMs = 0,
    maxEmptyStableTicks = undefined,
    stopXPaths = [],
} = {}) {
    const isGetter = (typeof xpathOrGetter === 'function');
    const readNow = isGetter
        ? () => xpathOrGetter(page)
        : () => getLastOuterHtmlByXPath(page, xpathOrGetter);

    const start = Date.now();

    // Дожидаемся появления элемента (appearTimeoutMs, НЕ timeoutMs) — только для XPath.
    if (!isGetter) {
        await waitForXPathCompat(page, xpathOrGetter, { timeoutMs: appearTimeoutMs, visible });
    }

    let prev = null;
    let sameCount = 0;
    let lastNonEmpty = null;
    let emptyStableCount = 0;

    while (Date.now() - start < timeoutMs) {
        const cur = await readNow();

        // Признак «генерация ещё идёт» (видна кнопка Stop). Проверяется лениво
        // и НЕ кэшируется между итерациями: кнопка Stop исчезает по окончании
        // генерации, и закешированный «true» застрял бы навсегда.
        // При maxEmptyStableTicks=0 (DeepSeek) кнопка Stop опрашивается только
        // после того, как контент прошёл порог длины — как в исходном коде.
        let generating = null;
        const isGenerating = async () => {
            if (generating === null) {
                generating = checkStopButton ? await isStillGenerating(page, stopXPaths) : false;
            }
            return generating;
        };

        // Reject empty/too-short answers — модель ещё не начала генерировать.
        if (minContentLength > 0 && (!cur || cur.length < minContentLength)) {
            // Для XPath-пути элемент уже сматчен: если генерация не идёт, а
            // текста всё нет — это «скелет», фейл-фаст к следующему селектору.
            // Геттер-путь (robust) не фейл-фастится: у него нет гарантии
            // появления элемента, он честно ждёт свой timeout.
            if (maxEmptyStableTicks !== undefined && !isGetter && !(await isGenerating())) {
                emptyStableCount++;
                if (emptyStableCount >= maxEmptyStableTicks) {
                    throw new Error('matched an empty skeleton (no content appears)');
                }
            }
            sameCount = 0;
            prev = cur;
            await sleep(pollMs);
            continue;
        }

        // Сохраняем последний валидный контент (на случай timeout вернём его)
        lastNonEmpty = cur;

        // Проверка кнопки Stop: если видна — генерация ещё идёт,
        // даже если HTML временно не меняется (пауза в генерации).
        if (checkStopButton && await isGenerating()) {
            sameCount = 0;
            prev = cur;
            await sleep(pollMs);
            continue;
        }

        if (cur && cur === prev) {
            sameCount++;

            // Стабильный, но текстово-пустой контейнер — тоже скелет: outerHTML
            // прошёл minContentLength за счёт атрибутов/обёрток, а текста нет.
            if (maxEmptyStableTicks !== undefined) {
                const plainTextLen = cur.replace(/<[^>]*>/g, '').trim().length;
                if (plainTextLen < 1) {
                    emptyStableCount++;
                    if (emptyStableCount >= maxEmptyStableTicks) {
                        throw new Error('matched an empty skeleton (stable, no text content)');
                    }
                } else {
                    emptyStableCount = 0;
                }
            }

            if (sameCount >= stableTicks) {
                // Подтверждение завершения: перечитываем через confirmMs. Если контент
                // продолжил расти — генерация не закончилась (пауза), продолжаем ждать,
                // иначе ответ обрежется посередине предложения.
                if (confirmMs > 0) {
                    await sleep(confirmMs);
                    const recheck = await readNow();
                    if (recheck !== cur) {
                        log('waitLastOuterHtmlStable: content grew after stable window — generation paused, not finished (' + (recheck ? recheck.length : 0) + ' vs ' + cur.length + ' chars)');
                        prev = recheck;
                        sameCount = 0;
                        lastNonEmpty = recheck && recheck.length >= minContentLength ? recheck : lastNonEmpty;
                        continue;
                    }
                }
                return cur;
            }
        }
        else {
            prev = cur;
            sameCount = 0;
            emptyStableCount = 0;
        }

        await sleep(pollMs);
    }

    // Timeout: если есть какой-то непустой контент — возвращаем его,
    // иначе бросаем ошибку.
    if (lastNonEmpty && lastNonEmpty.length >= minContentLength) {
        log('waitLastOuterHtmlStable: timeout reached, returning last non-empty content (' + lastNonEmpty.length + ' chars)');
        return lastNonEmpty;
    }

    throw new Error(`waitLastOuterHtmlStable timeout for xpath: ${isGetter ? '<getter>' : xpathOrGetter}`);
}

/**
 * Класс-независимый robust-fallback: находит последний «пузырь» ответа
 * ассистента по устойчивым структурным признакам (data-testid, ARIA role,
 * class*='assistant'/'message', последний дочерний блок чат-контейнера) и
 * возвращает { html, text }. НЕ опирается на конкретные имена классов
 * ('ds-markdown' / 'markdown'), которые сайты регулярно меняют — поэтому
 * переживает смену вёрстки.
 *
 * @param {object} opts — { extraThinkClass, extraThinkLabels, blockSelector }.
 */
async function getRobustLastAnswerData(page, {
    extraThinkClass = null,
    extraThinkLabels = [],
    blockSelector = "[class*='markdown']",
} = {}) {
    // ВНИМАНИЕ: в page.evaluate передаются ТОЛЬКО сериализуемые значения —
    // RegExp через границу puppeteer не проходит (превращается в {}). Поэтому
    // паттерны передаются строками и собираются в RegExp ВНУТРИ браузера.
    const extraThinkSrc = extraThinkClass ? extraThinkClass : null;
    const extraLabelsSrc = extraThinkLabels.length
        ? extraThinkLabels.map((s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|')
        : null;
    // Маркеры мусора (гидрация страницы) передаются в браузер строкой-массивом:
    // список живёт в общем модуле answerSanitizer (единый источник правды).
    const garbageMarkersSrc = GARBAGE_MARKERS;
    const garbageJsonMarkersSrc = GARBAGE_JSON_MARKERS;

    const data = await page.evaluate((opts) => {
        const containerSels = [
            "[data-testid='virtuoso-item-list']",
            "[data-testid*='chat-list']",
            "[data-testid*='message-list']",
            "[data-testid*='conversation']",
            "[role='log']",
            "[role='feed']",
            "[class*='chat-content-list']",
            "[class*='message-list']",
            "[class*='chat-list']",
            "[class*='conversation']",
        ];
        let container = null;
        for (const sel of containerSels) {
            const el = document.querySelector(sel);
            if (el && (el.innerText || '').trim().length > 20) { container = el; break; }
        }

        // «Пузырь» ответа ассистента (по убыванию специфичности). Сайт-специфичные
        // class*='ds-message' / class*='chat-content-item-assistant' подсказки безвредны
        // на чужом сайте (просто не совпадают) и полезны на своём.
        const bubbleSels = [
            "[data-testid*='assistant']",
            "[data-testid*='answer']",
            "[class*='chat-content-item-assistant']",
            "[class*='ds-message']",
            "[class*='assistant']",
            "[class*='answer']",
            "[role='article']",
            "[class*='message']",
        ];

        // Служебные теги: их содержимое (гидрация, стили) НИКОГДА не является
        // ответом модели.
        const SKIP_TAGS = ['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE'];
        const isSkippedNode = (node) =>
            !!node && SKIP_TAGS.indexOf(String(node.tagName || '').toUpperCase()) !== -1;

        // Похож ли текст на мусор (служебные данные страницы)? RegExp сюда не
        // передаём — собираем прямо здесь, список маркеров общий.
        const garbageMarkers = Array.isArray(opts.garbageMarkers) ? opts.garbageMarkers : [];
        const garbageJsonMarkers = Array.isArray(opts.garbageJsonMarkers) ? opts.garbageJsonMarkers : [];
        const looksGarbage = (text) => {
            const s = String(text || '');
            if (!s.trim()) return true;
            for (const m of garbageMarkers) {
                if (s.includes(m)) return true;
            }
            // JSON-контекст: маркеры состояния страницы — мусор только если
            // весь текст похож на JSON-объект (иначе это обычный ответ).
            const t = s.trim();
            if (t.length >= 40 && /^[[{]/.test(t)) {
                for (const m of garbageJsonMarkers) {
                    if (s.includes(m)) return true;
                }
            }
            return /window\.[A-Z][A-Z0-9_]{2,}\s*=/.test(s);
        };

        const pickLastWithText = (root, allowChildrenFallback) => {
            if (!root) return null;
            for (const sel of bubbleSels) {
                const nodes = root.querySelectorAll(sel);
                for (let i = nodes.length - 1; i >= 0; i--) {
                    if (isSkippedNode(nodes[i])) continue;
                    const text = (nodes[i].innerText || '').trim();
                    if (text.length === 0) continue;
                    // Пузырь с гидрацией/мусором — не ответ: пропускаем и берём
                    // предыдущий (нормальный) пузырь.
                    if (looksGarbage(text)) continue;
                    return nodes[i];
                }
            }
            // Крайний случай (только для найденного контейнера чата): последний
            // дочерний блок с substantial текстом (user-сообщение идёт выше —
            // последний с текстом = текущий ответ ассистента). Для document.body
            // этот фолбэк ЗАПРЕЩЁН: последний child body — это обычно inline
            // <script> с гидрацией страницы (именно он раньше и становился
            // «ответом»). Скрипты/стили/шаблоны пропускаем в любом случае.
            if (!allowChildrenFallback) return null;
            const children = Array.from(root.children || []);
            for (let i = children.length - 1; i >= 0; i--) {
                if (isSkippedNode(children[i])) continue;
                const text = (children[i].innerText || '').trim();
                if (text.length <= 10) continue;
                if (looksGarbage(text)) continue;
                return children[i];
            }
            return null;
        };

        const bubble = pickLastWithText(container, true) || pickLastWithText(document.body, false);
        if (!bubble) return { html: '', text: '' };

        // Reasoning/«размышления» НЕ должны попадать в content ответа: python-сторона
        // срезает только think-теги, а у веб-пула модельного текста их нет — склеенный
        // «reasoning + ответ» превращается в простыню прозы, из которой экстрактор
        // кода на бэкенде не извлекает код. Контейнеры размышлений вырезаем по
        // классам (think/reason) и data-testid; заголовки («Thought for 12 s» /
        // «思考中…») — отдельные короткие надписи, тоже удаляются. Отсутствие
        // контейнеров не меняет поведение (удаление — нулевой ущерб).
        const extraThinkRe = opts.extraThinkClass ? new RegExp(opts.extraThinkClass, 'i') : null;
        const extraLabelsRe = opts.extraThinkLabels
            ? new RegExp(`^(Thought for|Thinking|思考|已深度思考|${opts.extraThinkLabels}|Поразмышлял)`, 'i')
            : null;
        const classOf = (node) =>
            String(node.getAttribute && node.getAttribute('class') || '');
        const isThinkNode = (node) =>
            (node.hasAttribute && node.hasAttribute('data-testid') &&
                /think/i.test(node.getAttribute('data-testid') || '')) ||
            /think/i.test(classOf(node)) ||
            /reason/i.test(classOf(node)) ||
            (extraThinkRe ? extraThinkRe.test(classOf(node)) : false);
        const clone = bubble.cloneNode(true);
        clone.querySelectorAll('*').forEach((n) => {
            if (isThinkNode(n)) { n.remove(); return; }
        });
        // Сверху вниз: большие контейнеры (длина ≥ 160) не режем — только надписи.
        clone.querySelectorAll('*').forEach((n) => {
            const label = (n.innerText || '').trim();
            if (label && label.length < 160 && extraLabelsRe && extraLabelsRe.test(label)) {
                n.remove();
            }
        });

        // В пузыре может быть НЕСКОЛЬКО markdown-блоков (размышления + ответ).
        // Брали ПОСЛЕДНИЙ блок с текстом, но финал модели не всегда последний
        // (сайт доклеивает свои служебные блоки) — и самый полезный текст
        // терялся. При НЕСКОЛЬКИХ блоках выбираем САМЫЙ СОДЕРЖАТЕЛЬНЫЙ (максимум
        // непустого текста; при равенстве длин — последний, ближе к финалу
        // генерации). Блоки-мусор (гидрация) в кандидаты не попадают.
        // Единственный блок — как раньше, отдаём весь пузырь (clone).
        const blocks = Array.from(clone.querySelectorAll(opts.blockSelector))
            .filter((n) => (n.innerText || '').trim().length > 0)
            .filter((n) => !looksGarbage(n.innerText || ''));
        let picked = clone;
        if (blocks.length > 1) {
            let best = blocks[0];
            for (let i = 1; i < blocks.length; i++) {
                const len = (blocks[i].innerText || '').trim().length;
                const bestLen = (best.innerText || '').trim().length;
                if (len >= bestLen) best = blocks[i];
            }
            picked = best;
        }
        return { html: picked.outerHTML || '', text: (picked.innerText || '').trim() };
    }, {
        extraThinkClass: extraThinkSrc,
        extraThinkLabels: extraLabelsSrc,
        garbageMarkers: garbageMarkersSrc,
        garbageJsonMarkers: garbageJsonMarkersSrc,
        blockSelector,
    });
    return data || { html: '', text: '' };
}

async function getRobustLastAnswerHtml(page, opts) {
    // Обёртка над getRobustLastAnswerData для waitLastOuterHtmlStable (геттер
    // должен возвращать строку outerHTML для сравнения на стабильность).
    const data = await getRobustLastAnswerData(page, opts);
    return data.html || '';
}

async function dumpDomDiagnostics(page) {
    // Когда ВСЕ селекторы ответа (включая robust) провалились — печатаем снимок
    // структуры DOM в лог, чтобы калибровать селекторы без живого доступа к сайту.
    // Логируем кандидатные элементы: селектор, кол-во, класс, длину текста, начало
    // outerHTML — этого хватает, чтобы понять актуальную структуру ответа ассистента.
    try {
        const diag = await page.evaluate(() => {
            const trunc = (s, n) => (s && s.length > n ? s.slice(0, n) + '…[' + s.length + ']' : s) || '';
            const sels = [
                "[data-testid='virtuoso-item-list']",
                "[data-testid*='chat']",
                "[data-testid*='message']",
                "[data-testid*='answer']",
                "[data-testid*='assistant']",
                "[role='log']",
                "[role='feed']",
                "[role='article']",
                "[class*='chat-content']",
                "[class*='chat-list']",
                "[class*='message']",
                "[class*='conversation']",
                "[class*='ds-message']",
                "[class*='ds-markdown']",
                "[class*='markdown']",
                "[class*='assistant']",
                "[class*='answer']",
            ];
            const candidates = [];
            for (const sel of sels) {
                const els = document.querySelectorAll(sel);
                if (els.length) {
                    const last = els[els.length - 1];
                    candidates.push({
                        sel,
                        count: els.length,
                        tag: last.tagName,
                        cls: trunc(String(last.className || ''), 140),
                        textLen: (last.innerText || '').trim().length,
                        outerHead: trunc(last.outerHTML || '', 500),
                    });
                }
            }
            return {
                url: location.href,
                title: document.title,
                bodyTextLen: (document.body.innerText || '').length,
                bodyTextTail: trunc(document.body.innerText || '', 600),
                candidates,
            };
        });
        log('DOM DIAGNOSTICS (all answer selectors failed): ' + JSON.stringify(diag).slice(0, 3500));
    } catch (e) {
        log('DOM diagnostics dump failed: ' + (e?.message || e));
    }
}

/**
 * Отправка сообщения через UI сайта.
 *
 * @param {object} ctx — { browser, page }.
 * @param {object} payload — { model, thinking, user_id, message }.
 * @param {object} site — дескриптор сайта (sites/<id>.js).
 * @returns {string|object} Markdown-строка с ответом или { ok: false, reason }.
 */
async function sendMessage(ctx, payload = {}, site) {
    const page = ctx?.page;

    if (!page)
        return {
            ok: false,
            reason: 'ctx.page is missing',
        };
    try {
        const cfg = site.promtps;
        const currentService = payload.model;
        const thinking = !!payload.thinking;
        // История общая для всех ботов пула, но РАЗДЕЛЕНА по сайтам: ключ
        // префиксуется id сайта, чтобы DeepSeek и Kimi (если запущены в одном
        // процессе) не путали диалоги с одинаковым user_id.
        const uid = `${site.id}:${payload.user_id ?? 'default'}`;

        hist[uid] ??= [];

        // Only navigate to a fresh chat page for the first message in a conversation.
        // Сайт держит свой серверный контекст; повторный переход сбросил бы
        // диалог и потерял бы предыдущий контекст.
        if (hist[uid].length === 0) {
            await page.goto(site.data.services[currentService]);
        }

        const sendingData = {
            "role": "user",
            "content": payload.message
        };

        hist[uid].push(sendingData);

        // Send only the user's message text — NOT the entire conversation history.
        // Сайт держит свой conversation context на сервере; отправка JSON-массива
        // прошлых сообщений запутала бы модель и испортила промпт.
        const messageText = payload.message;

        // 1) Переключатель «размышлений» (DeepSeek DeepThink). У Kimi его нет.
        if (cfg.thinkingToggle) {
            const sel = thinking
                ? site.data.xpaths.chat.thinkingButtonDisabled[currentService]
                : site.data.xpaths.chat.thinkingButtonEnabled[currentService];
            await clickIfExists(page, sel);
        }

        // 2) Поле ввода должно существовать (его нет — юзер не залогинен).
        if (!(await cfg.inputExists(page, site, currentService))) {
            return {
                ok: false,
                reason: "can't send message",
                data: {
                    "moreInformation": "Probably user doesn't authorized"
                }
            }
        }

        // 3) Ввод текста (сайт-специфично: textarea vs contenteditable).
        const typedOk = await cfg.typeInput(page, site, currentService, messageText);
        if (!typedOk) {
            return {
                ok: false,
                reason: "can't send message",
                data: { "moreInformation": cfg.labels.typeFailed }
            };
        }

        // 4) СНАПШОТ ДО отправки (Kimi): текст последнего assistant-пузыря до
        // отправки. Если после «отправки» extracted-ответ совпадёт с ним —
        // сообщение не отправилось, а extraction прочитал СТАРЫЙ пузырь.
        let preAnswerText = '';
        if (cfg.snapshotBeforeSend) {
            try {
                preAnswerText = (await cfg.snapshotBeforeSend(page, site)) || '';
            } catch (_) { /* снапшот опционален — гвард просто не сработает */ }
        }

        // 5) Отправка (сайт-специфично: кнопка отправки vs Enter).
        await cfg.send(page, site, currentService, messageText);

        // 6) Ожидание стабилизации ответа (потоковая генерация) + извлечение.
        //    Fallback XPath-селекторы: если основной не сработал (сайт сменил
        //    HTML), пробуем альтернативные, прежде чем сдаться.
        const answerXPaths = cfg.answerXPaths(site, currentService);
        const toMarkdown = (html) => cfg.htmlToMarkdown(html, site);

        let answer = null;
        let answerXpUsed = null;
        let inner = '';
        // Причина отбраковки кандидата как мусора (гидрация страницы) — нужна
        // для честной диагностики: Django увидит GARBAGE_ANSWER и не будет
        // ретраить пул по кругу.
        let garbageHit = '';
        for (const xp of answerXPaths) {
            try {
                // Снимок посреди код-блока (незакрытый <pre>) — ждать продолжения:
                // до 3 докруток; если обрыв не ушёл (лимит вывода) — отдаём как
                // есть, чтобы пара получила честный DL-вердикт.
                const cutBudget = 3;
                for (let cutTry = 0; cutTry <= cutBudget; cutTry++) {
                    answer = await waitLastOuterHtmlStable(page, xp, {
                        ...cfg.stable,
                        appearTimeoutMs: cfg.appearTimeoutMs(xp, site, currentService, thinking),
                        stopXPaths: cfg.stopXPaths,
                    });
                    inner = toMarkdown(answer);
                    if (!looksCutMidCode(answer)) break;
                    if (cutTry < cutBudget) {
                        log('Snapshot looks cut mid-<pre> (streaming), re-waiting (' + (cutTry + 1) + '/' + cutBudget + ')...');
                    } else {
                        log('Snapshot still looks cut mid-<pre> after ' + cutBudget + ' re-waits — accepting as is');
                    }
                }
                // НЕ принимаем структурно-набитый, но текстово-пустой контейнер:
                // minContentLength проверяет длину outerHTML, а не текста, поэтому
                // пустой «скелет» (≥50 символов HTML-обёртки, ~1 символ текста)
                // проходит проверку. Короткие ответы («2», «да», «42») — легитимны,
                // НЕ отбраковываем: порог 1 символ, а не 5.
                // Текст, ПОЛНОСТЬЮ содержащийся в промпте — пузырь юзера/эхо:
                // отбраковываем и идём к следующему селектору.
                const echoHit = isEchoOfUserMessage(inner, messageText);
                // Мусор (гидрация/служебные данные страницы) — это НЕ ответ:
                // такой кандидат отбраковываем и идём к следующему селектору.
                const garbageReason = describeGarbage(inner);
                if (garbageReason) garbageHit = garbageReason;
                if (inner && inner.trim().length >= 1 && !echoHit && !garbageReason) {
                    answerXpUsed = xp;
                    break;
                }
                log('XPath ' + xp + (echoHit
                    ? ' matched an echo of the user message'
                    : (garbageReason
                        ? ' matched site garbage (' + garbageReason + ')'
                        : ' matched an empty skeleton (' + inner.trim().length + ' chars text)')) + ', trying next selector');
                answer = null;
                inner = '';
            } catch (e) {
                log('Answer XPath failed: ' + xp + ' — ' + (e?.message || e));
            }
        }

        if (!answer) {
            // Robust класс-независимый fallback: XPath-селекторы опираются на
            // имена классов, которые сайты меняют — тогда они ловят пустой
            // скелет. Берём вместо этого последний «пузырь» ассистента целиком
            // по структурным признакам и конвертируем весь outerHTML — текст
            // ответа + code blocks сохраняются.
            try {
                const robustHtml = await waitLastOuterHtmlStable(page,
                    (p) => getRobustLastAnswerHtml(p, cfg.robust),
                    {
                        ...cfg.robustStable,
                        stopXPaths: cfg.stopXPaths,
                    });
                if (robustHtml) {
                    inner = toMarkdown(robustHtml);
                    const robustEcho = isEchoOfUserMessage(inner, messageText);
                    const robustGarbage = describeGarbage(inner);
                    if (robustGarbage) garbageHit = robustGarbage;
                    if (inner && inner.trim().length >= 1 && !robustEcho && !robustGarbage) {
                        answer = robustHtml;
                        answerXpUsed = 'robust-fallback';
                        log('Robust fallback succeeded: extracted answer from generic assistant bubble (' + inner.trim().length + ' chars text)');
                    } else if (robustEcho) {
                        // «Последний пузырь с текстом» до появления ответа — это
                        // промпт юзера; раньше возвращался как «ответ модели».
                        log('Robust fallback matched a bubble whose text is a fragment of the user message — rejected as echo');
                    } else if (robustGarbage) {
                        // Пузырь оказался служебными данными страницы (гидрация) —
                        // ответом это быть не может.
                        log('Robust fallback matched a bubble with site garbage (' + robustGarbage + ') — rejected');
                    } else {
                        log('Robust fallback matched a bubble but converter yielded empty text');
                    }
                }
            } catch (e) {
                log('Robust fallback failed: ' + (e?.message || e));
            }
        }

        if (!answer) {
            // Все селекторы (включая robust) провалились — дампим структуру DOM
            // в лог, чтобы калибровать селекторы без живого доступа к сайту.
            await dumpDomDiagnostics(page);
            return {
                ok: false,
                reason: "can't send message",
                data: {
                    "moreInformation": garbageHit
                        // Кандидат нашёлся, но это были служебные данные страницы
                        // (гидрация) — честная метка для Django: не ретраить.
                        ? `GARBAGE_ANSWER: ${garbageHit}`
                        : cfg.labels.selectorsFailed
                }
            };
        }

        // Retry on EMPTY response (0 текста) — иногда сайт отдаёт пустой
        // контейнер, пока ещё генерирует. Короткие ответы (≥1) НЕ триггерят
        // retry — «2», «Four», «42» легитимны.
        if (!inner || inner.trim().length < 1) {
            log(cfg.labels.emptyRetryLog + ' (' + (inner?.length || 0) + ' chars), retrying once more...');
            await sleep(3000);
            for (const xp of answerXPaths) {
                try {
                    const retryAnswer = await waitLastOuterHtmlStable(page, xp, {
                        ...cfg.retryStable,
                        stopXPaths: cfg.stopXPaths,
                    });
                    const retryInner = toMarkdown(retryAnswer);
                    if (retryInner && retryInner.trim().length >= 1 && retryInner.trim().length > inner.trim().length
                        && !isEchoOfUserMessage(retryInner, messageText)
                        && !describeGarbage(retryInner)) {
                        inner = retryInner;
                        answer = retryAnswer;
                        answerXpUsed = xp;
                        break;
                    }
                } catch (e) {
                    log('Retry XPath failed: ' + xp + ' — ' + (e?.message || e));
                }
            }
        }

        // Final check: if still empty, return error
        if (!inner || !inner.trim()) {
            return {
                ok: false,
                reason: "can't send message",
                data: { "moreInformation": cfg.labels.emptyResponse }
            };
        }

        // Сайт-специфичные финальные гварды (Kimi: «stale answer» — ответ
        // совпадает с пузырём, который был ДО отправки, значит сообщение не
        // ушло; плюс повторная проверка эха).
        if (cfg.finalGuards) {
            const guard = cfg.finalGuards(inner, messageText, preAnswerText, site);
            if (guard) {
                return guard;
            }
        }

        // ФИНАЛЬНЫЙ ГЕЙТ МУСОРА: ответ, который выглядит как служебные данные
        // страницы (гидрация Next.js/Remix: window.HYDRATION_INIT_STATE=…,
        // __NEXT_DATA__, self.__next_f.push(…)), пользователю отдавать нельзя —
        // это не ответ модели. Отдаём честную ошибку (Django покажет её юзеру
        // и НЕ будет ретраить пул по кругу).
        const finalGarbage = describeGarbage(inner);
        if (finalGarbage) {
            log('Answer rejected as site garbage (' + finalGarbage + ') — not returning it to the client');
            return {
                ok: false,
                reason: "can't send message",
                data: { "moreInformation": `GARBAGE_ANSWER: ${finalGarbage}` }
            };
        }

        const answerData = {
            "role": "assistant",
            "content": inner
        }
        hist[uid].push(answerData);

        return inner;

    }
    catch (er) {
        return {
            ok: false,
            reason: "can't send message",
            data: {
                "moreInformation": er
            }
        }
    }
}

module.exports = {
    sendMessage,
    htmlToMarkdown,
    decodeHtmlEntities,
    extractCodeFromPreInner,
    isEchoOfUserMessage,
    looksCutMidCode,
    waitForXPathCompat,
    waitLastOuterHtmlStable,
    getRobustLastAnswerData,
    getRobustLastAnswerHtml,
    dumpDomDiagnostics,
};
