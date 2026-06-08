# Бот для DeepSeek

Обёртка над `chat.deepseek.com`: пул headless-браузеров (Puppeteer + stealth) логинится в DeepSeek и шлёт сообщения через настоящий UI, а наружу торчит как обычный OpenAI-совместимый API. Смысл — не платить за официальный доступ.

Стек: Node, Express (порт `3000`), Puppeteer `20.9.0`. Две части:
- **`api`** — диспетчер: принимает HTTP, держит пул, раздаёт запросы свободным ботам.
- **`worker`** — сам бот: Chrome, логин, печать, парсинг ответа.

```
клиент → api (Express :3000) → botManager (пул) → worker/bot.js (Chrome) → chat.deepseek.com
```

---

## Структура

```
api/
  index.js        старт: поднимает BotManager + Express, ловит SIGINT/SIGTERM
  server.js       роуты /health, /v1/chat/completions, /api/send
  botManager.js   пул ботов, состояния, reap мёртвых
  config.js       чтение env
  openaiFormat.js сборка ответа/ошибки в формате OpenAI
worker/
  index.js        createBot()
  bot.js          класс Bot: init / sendMessage / isAlive / close
  data.json       URL'ы + XPath'ы DeepSeek  ← правится при смене вёрстки
  hist.js         история диалогов (in-memory, общая на все боты)
  modules/auth.js     логин (и register-заглушка)
  modules/promtps.js  отправка сообщения + HTML→markdown
  core/page-utils.js  helpers puppeteer (waitAndTypeX и т.п.)
  utils/              logger, sleep
```

---

## HTTP API

| Метод | Что |
|---|---|
| `GET /health` | `{ ok, bots: [{id, state}] }` — только живые боты |
| `POST /api/send` | простой формат: `{ message, model, thinking?, conversation_id?/user_id? }` → `{ ok, data: { content } }` |
| `POST /v1/chat/completions` | OpenAI-формат: `{ model, messages[], thinking? }` → стандартный `chat.completion` |

Коды ошибок: `400` нет обязательных полей, `401 not_autorized` логин не прошёл, `429` все боты заняты, `503` бот ещё стартует (оба с `Retry-After`), `500` упало в процессе.

**Важно по OpenAI-эндпоинту:** из `messages[]` берётся **только последнее user-сообщение** (`extractLastUserMessage`), историю бот ведёт сам по `conversation_id`. Поле `model` в роутинг не идёт — всё уходит в DeepSeek. `conversation_id` резолвится так: `conversation_id` → заголовок `x-conversation-id` → `user_id` → `default`.

---

## worker → bot.js

Один бот и весь его жизненный цикл. Четыре метода:

- **`init()`** — запускает headless Chrome (флаг захардкожен `"new"`, env `HEADLESS` игнорируется), идёт на DeepSeek, логинится из `BOT_USERNAME`/`BOT_PASSWORD`. Не пустили → `NotAuthorizedError` (`code: not_autorized`), бот сам себя закрывает.
- **`sendMessage(payload)`** — печатает текст, при `thinking:true` жмёт DeepThink, отправляет, ждёт стабилизации HTML ответа (`waitLastOuterHtmlStable`), парсит в markdown.
- **`isAlive()`** — жив ли браузер/вкладка; этим пул отсеивает дохлых.
- **`close()`** — гасит Chrome (+ локальный прокси, если поднимался через `proxy-chain`).

Один бот = один Chrome = один аккаунт. Прокси опционально: `BOT_PROXY` / `BOT_PROXY_USER` / `BOT_PROXY_PASS`.

---

## data.json — карта кнопок (тут чинится вёрстка)

Бот не видит страницу — он ходит по **XPath**'ам из `worker/data.json` (поле ввода, кнопка отправки, контейнер ответа, поля логина, тумблер DeepThink).

⚠️ DeepSeek периодически меняет вёрстку → XPath перестаёт находить элемент → сыплется `can't send message` / `waitLastOuterHtmlStable timeout`, **при живом логине и интернете**. Лечится **только правкой `data.json`**: открыть сайт, снять новый DOM, переписать XPath, перезапустить. Не паролями и не настройками.

**Добавить новый сервис:** дописать в `data.json` блок под новым ключом (`services`, `loginUrls`, `xpaths.*`). Но парсинг ответа заточен под DeepSeek (`promtps.js → deepseekHtmlToApiMarkdown`) — под другой сайт нужен свой конвертер HTML→markdown.

---

## Пул ботов (botManager.js)

Просто массив ботов в памяти. Правила:

- размер ограничен `MAX_BOT_COUNT`, каждый бот ест ~150–250 МБ.
- все логинятся под **одним** аккаунтом.
- **ленивый старт**: на старте пул пуст, первый бот рождается на первый запрос → `503 Retry-After`, клиент ретраит.
- спавн **по одному** (`_spawning`), чтобы не долбить логин параллельно.
- reap-таймер раз в 5 сек выкидывает `failed` и мёртвых (закрытый браузер/вкладка). `not_autorized` **не удаляются** — чтобы API отдавал внятный 401; чтобы перелогиниться после правки env, перезапусти сервис.
- состояния: `starting → ready → busy`, плюс `not_autorized` / `failed`. `acquireReadyBot()` сразу ставит `busy`, в `finally` запроса — `markReady()`.

---

## Подводные камни (важно для поддержки)

- **История в памяти и общая.** `worker/hist.js` — один объект на все боты, ключ = `conversation_id`. **Перезапуск = всё стёрлось.** Персистентности нет.
- **Контекст шлётся как JSON в поле ввода.** В `promtps.js` весь массив `hist[uid]` сериализуется и печатается в textarea целиком. Т.е. «память» — это не нативные треды DeepSeek, а JSON-простыня в каждом сообщении. Растёт линейно, длинные диалоги упрутся в лимиты.
- **Капчу бот не решает** — при ней логин падает в `not_autorized`.
- **Один аккаунт на всех ботов** — DeepSeek может ограничить за параллель.
- **Стриминга нет** — ответ отдаётся целиком после генерации.
- **`register()` в auth.js — заглушка** (код подтверждения захардкожен), на проде не использовать.

---

## Запуск (Windows)

В `run_api.bat` вписать данные и запустить даблкликом:

```
set "BOT_USERNAME=email@gmail.com"
set "BOT_PASSWORD=пароль"
```

Первый раз сам поставит зависимости (`npm i`). Готово, когда в консоли `listening on :3000`. Без `.bat`: `npm install && npm start`.

> Пароль в `.bat`/`.env` лежит в открытом виде — публично не выкладывать.

---

## Настройки (env)

| env | что | дефолт |
|---|---|---|
| `BOT_USERNAME` / `BOT_PASSWORD` | аккаунт DeepSeek (обязательно) | — |
| `MAX_BOT_COUNT` | размер пула, ~200 МБ на бота | `3` |
| `PORT` | порт Express | `3000` |
| `REQUEST_TIMEOUT_MS` | таймаут одного запроса к DeepSeek | `180000` |
| `RETRY_AFTER_SEC` | значение заголовка `Retry-After` при 429/503 | `3` |
| `SERVICE_MODEL` | ключ сервиса из `data.json` | `deepseek` |
| `BOT_PROXY` / `_USER` / `_PASS` | прокси (опц.) | — |
| `AUTH_DEBUG` / `AUTH_TIMEOUT_MS` | дамп html+скрин при сбое логина в `worker/logs/` | `0` / `45000` |

---

## Если сломалось

- **503 на первый запрос** — норма, бот логинится (5–10 сек), ретрай.
- **401 / not_autorized** — логин/пароль или капча. Проверить вход руками; при `AUTH_DEBUG=1` смотреть дамп в `worker/logs/`.
- **429** — пул занят: поднять `MAX_BOT_COUNT` (следить за RAM) или ретрай.
- **`timeout` / не нашёл поле при верном логине** — почти всегда сменилась вёрстка → чинить `data.json`.
- **история пропала** — перезапускали сервис, ожидаемо.

Логи действий ботов — `worker/logs/application.log`, префикс `[bot#N]` = номер бота. Stdout Express уходит в консоль/`docker logs`.

---

## Диагностика из Docker
> Просто ctrl+C -> ctrl+V на сервере для теста

Контейнер — `dl_ai_web`, бот внутри лежит в `/app/bot`, Chromium от puppeteer — в `/opt/puppeteer-runtime`. Проверки гоняем через `docker exec -w /app/bot` (так `require('puppeteer')` и `require('./worker')` резолвятся из бота). Команды одноразовые, контейнер должен быть запущен.

> ⚠️ **Egress только через прокси с авторизацией.** Голый `puppeteer.launch` упрётся в `net::ERR_INVALID_AUTH_CREDENTIALS` — Chromium ушёл в прокси без логина/пароля. Прокси лежит в `bot/.env` (`BOT_PROXY` / `BOT_PROXY_USER` / `BOT_PROXY_PASS`), и `bot.js` оборачивает его через `proxy-chain` в локальный прокси без авторизации. Поэтому **тест №2 (через рабочий код бота) проходит, а сырые puppeteer-команды — нет**. В тестах ниже та же прокси-обвязка вшита в начало скрипта.

### 1. Есть ли вообще выход в сеть (puppeteer → google → title)

Базовый санити-чек: поднимается ли Chromium и ходит ли он наружу через прокси.

```bash
docker exec -w /app/bot dl_ai_web node -e "
const puppeteer=require('puppeteer');
const proxyChain=require('proxy-chain');
const clean=v=>String(v||'').trim().replace(/^['\"]|['\"]$/g,'');
(async()=>{
  let px=null, s=clean(process.env.BOT_PROXY), u=clean(process.env.BOT_PROXY_USER), pw=clean(process.env.BOT_PROXY_PASS);
  if(s&&u){const host=s.replace(/^https?:\/\//,''); px=await proxyChain.anonymizeProxy('http://'+encodeURIComponent(u)+':'+encodeURIComponent(pw)+'@'+host);}
  const b=await puppeteer.launch({headless:'new',args:[...(px?['--proxy-server='+px]:[]),'--no-sandbox','--disable-setuid-sandbox','--disable-dev-shm-usage']});
  const p=await b.newPage();
  await p.goto('https://www.google.com',{waitUntil:'domcontentloaded',timeout:60000});
  console.log('TITLE:', await p.title());
  await b.close(); if(px) await proxyChain.closeAnonymizedProxy(px,true);
})().catch(e=>{console.error('ERR:',e.message);process.exit(1);});
"
```

Ждём `TITLE: Google`. Если всё равно падает на прокси — проверь сами значения `BOT_PROXY*` в `bot/.env`. Если виснет без прокси-ошибки — Chromium не стартует (не хватает системных либ) или прокси недоступен.

### 2. Создаётся ли вообще бот (init + логин в DeepSeek)

Сквозная проверка через рабочий код бота (`worker/index.js`) — он сам поднимает прокси и логинится. Логин/пароль и прокси берутся из env контейнера (`bot/.env`).

```bash
docker exec -w /app/bot dl_ai_web node -e "
const {createBot}=require('./worker');
(async()=>{
  const bot=createBot({id:999});
  await bot.init();
  console.log('BOT OK, alive =', bot.isAlive());
  await bot.close();
})().catch(e=>{console.error('BOT FAIL:', e.code||'', e.message);process.exit(1);});
"
```

`BOT OK` → бот создаётся и логинится (если так — сеть/прокси/логин в порядке, проблема где-то выше). `BOT FAIL ... not_autorized` → логин не прошёл (пароль/капча). Другая ошибка → почти наверняка съехала вёрстка → `data.json`. После закрытия в логах мелькнёт `page closed` — это норма, бот сам себя погасил. Учти: тест логинится тем же аккаунтом, что и боевой пул, — гоняй разово.

### 3. Снять с DeepSeek кнопки/поля (для починки data.json)

Когда XPath перестал находить элемент, снимаем актуальный DOM и пересобираем XPath. Прокси-обвязка та же, что в тесте 1.

> DeepSeek — SPA: после загрузки страница ещё раз сама себя перерисовывает, из-за чего `evaluate` ловит `Execution context was destroyed`. Поэтому ждём не `networkidle2`, а появления конкретного элемента (`input`/`textarea`) и оборачиваем съём DOM в ретрай.

**Страница логина** (поля логина/пароля + кнопка авторизации):

```bash
docker exec -w /app/bot dl_ai_web node -e "
const puppeteer=require('puppeteer');
const proxyChain=require('proxy-chain');
const clean=v=>String(v||'').trim().replace(/^['\"]|['\"]$/g,'');
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
(async()=>{
  let px=null, s=clean(process.env.BOT_PROXY), u=clean(process.env.BOT_PROXY_USER), pw=clean(process.env.BOT_PROXY_PASS);
  if(s&&u){const host=s.replace(/^https?:\/\//,''); px=await proxyChain.anonymizeProxy('http://'+encodeURIComponent(u)+':'+encodeURIComponent(pw)+'@'+host);}
  const b=await puppeteer.launch({headless:'new',args:[...(px?['--proxy-server='+px]:[]),'--no-sandbox','--disable-setuid-sandbox','--disable-dev-shm-usage']});
  const p=await b.newPage();
  await p.goto('https://chat.deepseek.com/sign_in',{waitUntil:'domcontentloaded',timeout:60000});
  await p.waitForSelector('input',{timeout:30000}).catch(()=>{});
  async function grab(fn){for(let i=0;i<6;i++){try{return await p.evaluate(fn);}catch(e){if(/context was destroyed|detached|Cannot find context/i.test(e.message)){await sleep(1500);continue;}throw e;}}throw new Error('evaluate keeps failing');}
  const d=await grab(()=>{
    const short=el=>el.outerHTML.slice(0,160).replace(/\s+/g,' ');
    return {
      inputs:[...document.querySelectorAll('input')].map(el=>({type:el.type,placeholder:el.placeholder,html:short(el)})),
      buttons:[...document.querySelectorAll('button,[role=button],div.ds-button')].map(el=>({text:el.innerText.trim().slice(0,40),cls:el.className}))
    };
  });
  console.log(JSON.stringify(d,null,2));
  await b.close(); if(px) await proxyChain.closeAnonymizedProxy(px,true);
})().catch(e=>{console.error('ERR:',e.message);process.exit(1);});
"
```
