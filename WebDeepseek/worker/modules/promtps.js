/**
 * Модуль отправки сообщений через UI сайта DeepSeek (Puppeteer-автоматизация).
 *
 * Основная функция: sendMessage(ctx, payload) — вводит текст в поле ввода,
 * нажимает кнопку отправки, ждёт стабилизации ответа (потоковая генерация токенов),
 * извлекает HTML ответа и конвертирует его в Markdown (deepseekHtmlToApiMarkdown).
 *
 * Содержит хелперы для работы с XPath, ожидания стабилизации контента,
 * декодирования HTML-сущностей и конвертации HTML → Markdown.
 *
 * XPath-селекторы загружаются из data.json (data.xpaths.chat.*).
 */

const data = require("../data.json")

const { log, error } = require('../utils/logger');

const { waitAndType,
	waitAndTypeX,
	waitAndClick,
	waitAndClickX,
	elementExists,
	clickIfExists,
	getCurrentUrl,
	isCurrentUrlContains } = require('../core/page-utils')

const { sleep } = require('../utils/helpers');

const { hist } = require('../hist');

function decodeHtmlEntities(s) {
  // Декодирование HTML-сущностей (&nbsp;, &amp;, &#xNN;, &#NN; и т.д.) в обычные символы.
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

function stripOuterDiv(html) {
  let s = String(html || '').trim();
  return s
    .replace(/^<div\b[^>]*\bds-markdown\b[^>]*>/i, '')
    .replace(/<\/div>\s*$/i, '')
    .trim();
}

function extractCodeFromPreInner(preInnerHtml) {
  const raw = preInnerHtml
    .replace(/<br\s*\/?>/gi, '\n')
    .replace(/<\/?[^>]+>/g, ''); // снос всех тегов подсветки
  return decodeHtmlEntities(raw).replace(/\r\n/g, '\n').trim();
}

function deepseekHtmlToApiMarkdown(html) {
  // Конвертация HTML-ответа DeepSeek в Markdown.
  // 1) Извлечение code blocks в плейсхолдеры (чтобы не сломать при чистке тегов).
  // 2) Преобразование ссылок, форматирования, заголовков, списков.
  // 3) Удаление остальных тегов, декодирование сущностей.
  // 4) Восстановление code blocks из плейсхолдеров.
  // 5) Нормализация пробелов и пустых строк.
  let s = stripOuterDiv(html);

  // 1) Вырезаем code blocks в плейсхолдеры, чтобы дальнейшая чистка не сломала их
  const codeBlocks = [];

  s = s.replace(
    /<div\b[^>]*\bmd-code-block\b[^>]*>[\s\S]*?<pre\b[^>]*>([\s\S]*?)<\/pre>[\s\S]*?<\/div>/gi,
    (blockHtml, preInner) => {
      const langMatch =
        blockHtml.match(/<span[^>]*\bd813de27\b[^>]*>([^<]+)<\/span>/i) ||
        blockHtml.match(/class="language-([^"]+)"/i);

      const lang = langMatch
        ? String(langMatch[1] || '').trim().toLowerCase()
        : '';

      const code = extractCodeFromPreInner(preInner);
      const fence = lang ? `\`\`\`${lang}\n${code}\n\`\`\`` : `\`\`\`\n${code}\n\`\`\``;

      const token = `@@CODEBLOCK_${codeBlocks.length}@@`;
      codeBlocks.push(fence);
      return token;
    }
  );

  // 1b) Обычный markdown html: <pre><code class="language-x">...</code></pre>
  s = s.replace(
    /<pre\b[^>]*>\s*<code\b([^>]*)>([\s\S]*?)<\/code>\s*<\/pre>/gi,
    (_, codeAttrs, codeInner) => {
      const langMatch = String(codeAttrs).match(/class\s*=\s*"[^"]*language-([^"\s]+)[^"]*"/i);
      const lang = langMatch ? langMatch[1].trim().toLowerCase() : '';
      const code = decodeHtmlEntities(codeInner).replace(/\r\n/g, '\n').trim();
      const fence = lang ? `\`\`\`${lang}\n${code}\n\`\`\`` : `\`\`\`\n${code}\n\`\`\``;

      const token = `@@CODEBLOCK_${codeBlocks.length}@@`;
      codeBlocks.push(fence);
      return token;
    }
  );

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
  // @@OLSTART_n@@ меняем на ничего, а "- " внутри ol можно вручную заменить если надо.
  // Если хочешь реальную нумерацию — скажи, сделаю полноценный проход.
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

// XPath-селекторы для признака «генерация ещё идёт» (видна кнопка Stop).
// DeepSeek периодически меняет вёрстку кнопки Stop, поэтому здесь широкий набор
// вариантов (класс stop, aria-label, role=button, SVG с stop-иконкой, текст).
// Если ни один не совпадает с актуальной кнопкой — isStillGenerating всегда false,
// и тогда единственный сигнал завершения — подтверждение перечитыванием (confirmMs).
const STOP_GENERATING_XPATHS = [
  "//div[contains(@class,'stop') and not(contains(@class,'hidden'))]",
  "//div[@aria-label='stop' or @aria-label='Stop' or @aria-label='Stop generating' or @aria-label='stop generating']",
  "//button[contains(@class,'stop') or @aria-label='stop' or @aria-label='Stop' or @aria-label='Stop generating' or @aria-label='stop generating']",
  "//div[contains(@class,'ds-button') and .//svg[contains(@class,'stop')]]",
  "//div[contains(@class,'ds-button') and .//svg[contains(@class,'icon') and contains(@class,'stop')]]",
  "//button[contains(@class,'ds-button') and .//svg[contains(@class,'stop')]]",
  "//*[@role='button' and (contains(@aria-label,'stop') or contains(@aria-label,'Stop') or contains(@class,'stop'))]",
  "//button[.//text()[contains(.,'Stop') or contains(.,'Остановить') or contains(.,'stop')]]",
];

async function isStillGenerating(page) {
  // Проверяет, идёт ли ещё потоковая генерация (видна ли кнопка Stop).
  try {
    for (const xp of STOP_GENERATING_XPATHS) {
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

async function waitLastOuterHtmlStable(page, xpathOrGetter, {
    timeoutMs = 300000,
    appearTimeoutMs = 20000,
    pollMs = 2000,
    stableTicks = 6,
    visible = true,
    minContentLength = 50,
    checkStopButton = true,
    confirmMs = 0,
} = {}) {
    // Ожидание стабилизации HTML-контента: опрашивает outerHTML элемента по XPath,
    // пока он не перестанет меняться stableTicks раз подряд (потоковая генерация завершена).
    //
    // Улучшения:
    // - checkStopButton: проверяет кнопку Stop в DOM — если видна, генерация продолжается
    //   (сбрасывает sameCount даже если HTML не менялся в течение паузы).
    // - minContentLength=50: игнорирует пустые/короткие ответы (< 50 символов).
    // - timeoutMs=300000 (5 мин): бюджет на стабилизацию длинного ответа (.poll-цикл).
    // - appearTimeoutMs=20000 (20с): таймаут ПЕРВОГО появления элемента в DOM
    //   (waitForXPathCompat). Если за 20с после клика «отправить» контейнер ответа
    //   не появился — XPath устарел (DeepSeek сменил вёрстку), фейл-фаст к следующему
    //   fallback-селектору. Раньше тут был тот же timeoutMs=300000 → один устаревший
    //   селектор ждал 5 мин пустого «не нашлось», а 4 селектора × 300с = 20 мин холостого
    //   вращения держали все боты BUSY → 429 «Все боты заняты». Реальный ответ
    //   появляется в DOM за 1-3с, поэтому 20с — щедрый запас, на норм режим не влияет.
    // - stableTicks=6 × pollMs=2000 = 12 секунд стабильности для уверенности.
    // - confirmMs: ПОДТВЕРЖДЕНИЕ перечитыванием. Когда накопилось stableTicks
    //   совпадений, ждём ещё confirmMs и перечитываем — если контент вырос, значит
    //   12с «стабильности» были паузой в генерации, а не завершением (продолжаем
    //   ждать). Это единственный надёжный сигнал завершения, если селектор кнопки
    //   Stop устарел — без него ответ обрезается посередине (DeepSeek генерит паузами).
    // xpathOrGetter: либо строка XPath (как раньше), либо async-геттер (page) => html.
    // Геттер используется класс-независимым robust-fallback (getRobustLastAnswerHtml):
    // для него появления ждёт сам poll-цикл (minContentLength), waitForXPathCompat не нужен.
    const isGetter = (typeof xpathOrGetter === 'function');
    const readNow = isGetter
        ? () => xpathOrGetter(page)
        : () => getLastOuterHtmlByXPath(page, xpathOrGetter);

    const start = Date.now();

    // Дожидаемся появления элемента (appearTimeoutMs, НЕ timeoutMs — см. комментарий выше) — только для XPath.
    if (!isGetter) {
        await waitForXPathCompat(page, xpathOrGetter, { timeoutMs: appearTimeoutMs, visible });
    }

    let prev = null;
    let sameCount = 0;
    let lastNonEmpty = null;

    while (Date.now() - start < timeoutMs) {
        const cur = await readNow();

        // Reject empty/too-short answers — DeepSeek hasn't started generating yet.
        if (minContentLength > 0 && (!cur || cur.length < minContentLength)) {
            sameCount = 0;
            prev = cur;
            await sleep(pollMs);
            continue;
        }

        // Сохраняем последний валидный контент (на случай timeout вернём его)
        lastNonEmpty = cur;

        // Проверка кнопки Stop: если видна — генерация ещё идёт,
        // даже если HTML временно не меняется (пауза в генерации).
        if (checkStopButton && await isStillGenerating(page)) {
            sameCount = 0;
            prev = cur;
            await sleep(pollMs);
            continue;
        }

        if (cur && cur === prev) {
            sameCount++;

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

async function getDeepseekLastAnswerHtml(ctx, data, {
    timeoutMs = 120000,
    pollMs = 1000,
    stableTicks = 2,
} = {}) {
    const page = ctx?.page;
    if (!page) throw new Error('ctx.page is required');

    const fullXPath = data.xpaths.chat.fullAnswer.deepseek; // //div[contains(@class,'ds-message')]
    const ansXPath  = data.xpaths.chat.answer.deepseek;     // //div[contains(@class,'ds-message')]/div[contains(@class,'ds-markdown')]

    // Ждём стабилизацию отдельно для full и answer
    const fullHtml = await waitLastOuterHtmlStable(page, fullXPath, { timeoutMs, pollMs, stableTicks });
    const answerHtml = await waitLastOuterHtmlStable(page, ansXPath, { timeoutMs, pollMs, stableTicks });

    return { fullHtml, answerHtml };
}

async function getRobustLastAnswerHtml(page) {
  // Класс-независимый robust-fallback: находит последний «пузырь» ответа ассистента
  // по устойчивым структурным признакам (data-testid, ARIA role, class*='assistant'/
  // 'message', последний дочерний блок чат-контейнера) и возвращает его outerHTML.
  // НЕ опирается на конкретные имена классов ('ds-markdown' и т.п.), которые DeepSeek
  // регулярно меняет — поэтому переживает смену вёрстки. Весь пузырь целиком скарм-
  // ливается deepseekHtmlToApiMarkdown: она снимет теги и сохранит code blocks.
  return page.evaluate(() => {
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

    const pickLastWithText = (root) => {
      if (!root) return null;
      for (const sel of bubbleSels) {
        const nodes = root.querySelectorAll(sel);
        for (let i = nodes.length - 1; i >= 0; i--) {
          if ((nodes[i].innerText || '').trim().length > 0) return nodes[i];
        }
      }
      // Крайний случай: последний дочерний блок контейнера с substantial текстом
      // (user-сообщение идёт выше — последний с текстом = текущий ответ ассистента).
      const children = Array.from(root.children || []);
      for (let i = children.length - 1; i >= 0; i--) {
        if ((children[i].innerText || '').trim().length > 10) return children[i];
      }
      return null;
    };

    const bubble = pickLastWithText(container) || pickLastWithText(document.body);
    if (!bubble) return '';
    return bubble.outerHTML || '';
  });
}

async function dumpDomDiagnostics(page) {
  // Когда ВСЕ селекторы ответа (включая robust) провалились — печатаем снимок
  // структуры DOM в лог, чтобы калибровать селекторы без живого доступа к сайту.
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

async function sendMessage(ctx, payload = {}) {
    // Отправка сообщения через UI сайта DeepSeek.
    // payload: { model, thinking, user_id, message }
    // Возвращает Markdown-стрроку с ответом модели или { ok: false, reason: ... }.
    //
    // Логика:
    // 1) При первом сообщении в диалоге — переход на страницу чата.
    // 2) Включение/выключение DeepThink (thinking mode) по необходимости.
    // 3) Ввод текста в поле ввода, нажатие кнопки отправки.
    // 4) Ожидание стабилизации ответа (потоковая генерация токенов).
    // 5) Конвертация HTML ответа в Markdown.
    const page = ctx?.page;

    if (!page) 
        return { 
            ok: false, 
            reason: 'ctx.page is missing',
        };
    try {
        let currentService = payload.model;
        let isUsetThinking = payload.thinking;
        let uid = payload.user_id;

        hist[uid] ??= [];

        // Only navigate to a fresh chat page for the first message in a conversation.
        // DeepSeek maintains its own server-side context; re-navigating would reset
        // the conversation and lose all prior context.
        if (hist[uid].length === 0) {
            await page.goto(data.services[currentService]);
        }

        let sendingData = {
            "role": "user",
            "content": payload.message
        }

        hist[uid].push(sendingData);

        // Send only the user's message text — NOT the entire conversation history.
        // DeepSeek keeps its own conversation context server-side; sending a JSON
        // array of previous messages would confuse the model and corrupt the prompt.
        const messageText = payload.message;

        // DeepThink toggle: "Enabled" = DeepThink is currently ON (we want to turn it OFF).
        //                    "Disabled" = DeepThink is currently OFF (we want to turn it ON).
        if (isUsetThinking) {
            await clickIfExists(page, data.xpaths.chat.thinkingButtonDisabled[currentService]);
        }
        else {
            await clickIfExists(page, data.xpaths.chat.thinkingButtonEnabled[currentService]);
        }

        if (!(await elementExists(page, data.xpaths.chat.inputLabel[currentService]))) {
            return {
            ok: false,
            reason: "can't send message",
            data: {
                "moreInformation": "Probably user doesn't authorized"
            }
        }
        }

        await waitAndTypeX(page, data.xpaths.chat.inputLabel[currentService], messageText);

        // Try clicking the send button via XPath. If the primary selector fails
        // (DeepSeek UI changed), fall back to pressing Enter in the textarea.
        const sendClicked = await waitAndClickX(page, data.xpaths.chat.sendMessageButton[currentService]);
        if (!sendClicked) {
            log('Send button not found via XPath, falling back to Enter key');
            try {
                const textarea = await waitForXPathCompat(page, data.xpaths.chat.inputLabel[currentService], { timeout: 10000 });
                await textarea.press('Enter');
            } catch (e2) {
                // Last resort: page-level keyboard Enter
                await page.keyboard.press('Enter');
            }
        }

        // Wait for the answer to stabilize — DeepSeek streams tokens, so we
        // need enough stable ticks to ensure generation is truly complete.
        // 6 ticks × 2000ms = 12s of stability required before reading.
        // Also wait until the answer has actual content (min 50 chars HTML).
        // timeoutMs=300000 (5 min) — enough for long answers.
        //
        // Fallback XPath selectors: if the primary selector fails (DeepSeek
        // changed their HTML), try alternative selectors before giving up.
        const answerXPaths = [
            data.xpaths.chat.answer[currentService],
            // Fallback 1: any element with ds-markdown class inside a message
            "//div[contains(@class,'ds-message')]//div[contains(@class,'ds-markdown')]",
            // Fallback 2: any element with ds-markdown class (broader)
            "//div[contains(@class,'ds-markdown')]",
            // Fallback 3: any element with markdown class inside a chat message
            "//div[contains(@class,'message')]//div[contains(@class,'markdown')]",
        ];

        let answer = null;
        let answerXpUsed = null;
        let inner = '';
        for (const xp of answerXPaths) {
            try {
                answer = await waitLastOuterHtmlStable(page, xp, {
                    timeoutMs: 300000,
                    pollMs: 2000,
                    stableTicks: 6,
                    minContentLength: 50,
                    checkStopButton: true,
                    confirmMs: 5000,
                });
                // НЕ принимаем структурно-набитый, но текстово-пустой контейнер:
                // minContentLength проверяет длину outerHTML, а не текста, поэтому
                // пустой «скелет» ds-markdown (≥50 символов HTML-обёртки, ~1 символ
                // текста) проходит проверку. Реальный ответ при этом часто живёт в
                // другом контейнере, который поймает следующий селектор — поэтому
                // конвертируем и провéряем, что есть настоящий текст, иначе идём
                // к следующему XPath (это и есть источник «Empty/short response
                // detected (1 chars)» в логах).
                inner = deepseekHtmlToApiMarkdown(answer);
                // Короткие ответы («2», «да», «Four», «42») — легитимны, НЕ
                // отбраковываем: порог 1 символ, а не 5. Скелет-плейсхолдер
                // даёт 0 текста, реальный ответ — ≥1. (Аналогично Kimi.)
                if (inner && inner.trim().length >= 1) {
                    answerXpUsed = xp;
                    break;
                }
                log('XPath ' + xp + ' matched an empty skeleton (' + inner.trim().length + ' chars text), trying next selector');
                answer = null;
                inner = '';
            } catch (e) {
                log('Answer XPath failed: ' + xp + ' — ' + (e?.message || e));
            }
        }

        if (!answer) {
            // Robust класс-независимый fallback: XPath-селекторы опираются на имена
            // классов ('ds-markdown'), которые DeepSeek меняет — тогда они ловят
            // пустой скелет. Берём вместо этого последний «пузырь» ассистента целиком
            // по структурным признакам (getRobustLastAnswerHtml) и конвертируем весь
            // outerHTML — текст ответа + code blocks сохраняются.
            try {
                const robustHtml = await waitLastOuterHtmlStable(page, getRobustLastAnswerHtml, {
                    timeoutMs: 120000,
                    pollMs: 2000,
                    stableTicks: 6,
                    minContentLength: 50,
                    checkStopButton: true,
                    confirmMs: 5000,
                });
                if (robustHtml) {
                    inner = deepseekHtmlToApiMarkdown(robustHtml);
                    if (inner && inner.trim().length >= 1) {
                        answer = robustHtml;
                        answerXpUsed = 'robust-fallback';
                        log('Robust fallback succeeded: extracted answer from generic assistant bubble (' + inner.trim().length + ' chars text)');
                    } else {
                        log('Robust fallback matched a bubble but converter yielded empty text');
                    }
                }
            } catch (e) {
                log('Robust fallback failed: ' + (e?.message || e));
            }
        }

        if (!answer) {
            // Все селекторы (включая robust) провалились — дампим структуру DOM в лог,
            // чтобы калибровать селекторы без живого доступа к сайту.
            await dumpDomDiagnostics(page);
            return {
                ok: false,
                reason: "can't send message",
                data: { "moreInformation": "All answer XPath selectors failed — DeepSeek UI may have changed (DOM diagnostics dumped to worker log)" }
            };
        }

        // Retry on EMPTY response (0 текста) — sometimes DeepSeek returns an
        // empty container while still generating. Короткие ответы (≥1) НЕ
        // триггерят retry — «2», «Four», «42» легитимны.
        if (!inner || inner.trim().length < 1) {
            log('Empty/short response detected (' + (inner?.length || 0) + ' chars), retrying once more...');
            await sleep(3000);
            for (const xp of answerXPaths) {
                try {
                    const retryAnswer = await waitLastOuterHtmlStable(page, xp, {
                        timeoutMs: 120000,
                        pollMs: 2000,
                        stableTicks: 6,
                        minContentLength: 50,
                        checkStopButton: true,
                        confirmMs: 5000,
                    });
                    const retryInner = deepseekHtmlToApiMarkdown(retryAnswer);
                    if (retryInner && retryInner.trim().length >= 1 && retryInner.trim().length > inner.trim().length) {
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
                data: { "moreInformation": "DeepSeek returned an empty response after retries" }
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
    sendMessage
};