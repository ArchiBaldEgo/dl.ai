# bot-pool-api — DeepSeek Web Bot

Сервис-обёртка над веб-интерфейсом [chat.deepseek.com](https://chat.deepseek.com), который притворяется обычным OpenAI-совместимым API. Под капотом — пул headless-браузеров (Puppeteer + Stealth), которые логинятся в DeepSeek и отправляют сообщения через настоящий UI.

Нужен, когда не хочется (или нет возможности) платить за официальный DeepSeek API.

---

## Содержание

1. [Архитектура](#архитектура)
2. [Структура проекта](#структура-проекта)
3. [Конфигурация (`.env`)](#конфигурация-env)
4. [Запуск](#запуск)
5. [HTTP API](#http-api)
6. [Как живёт пул ботов](#как-живёт-пул-ботов)
7. [Поддерживаемые модели](#поддерживаемые-модели)
8. [Логи и диагностика](#логи-и-диагностика)
9. [Типичные ошибки](#типичные-ошибки)
10. [Ограничения](#ограничения)

---

## Архитектура

```
   Django (ai/utils.py)
          │
          │  HTTP POST /api/send
          ▼
   ┌─────────────────────┐
   │   bot-pool-api      │   Express, порт 3000
   │   (api/index.js)    │
   └─────────┬───────────┘
             │
             │  acquire bot
             ▼
   ┌─────────────────────┐
   │   BotManager        │   пул из N браузеров
   │   (botManager.js)   │   состояния: STARTING / READY / BUSY / NOT_AUTORIZED / FAILED
   └─────────┬───────────┘
             │
             ▼
   ┌─────────────────────┐
   │   Worker (Bot)      │   Puppeteer + stealth
   │   (worker/bot.js)   │   headless Chrome
   └─────────┬───────────┘
             │
             ▼
   chat.deepseek.com (реальный сайт)
```

Один бот = один headless-Chrome с одной залогиненной сессией. Запросы между разными ботами идут параллельно, внутри одного бота — последовательно.

---

## Структура проекта

```
bot/
├── api/
│   ├── index.js         # entrypoint, поднимает Express
│   ├── server.js        # роуты /v1/chat/completions, /api/send, /health
│   ├── botManager.js    # пул ботов, состояния, реап мёртвых
│   ├── config.js        # парсинг env
│   └── openaiFormat.js  # форматирование ответов в стиле OpenAI
├── worker/
│   ├── index.js         # экспортит createBot()
│   ├── bot.js           # класс Bot, lifecycle (init/sendMessage/close)
│   ├── hist.js          # in-memory история диалогов (общая для всех ботов)
│   ├── data.json        # URL'ы и XPath'ы для chat.deepseek.com
│   ├── modules/
│   │   ├── auth.js      # логин на DeepSeek через UI
│   │   └── promtps.js   # отправка сообщения + парсинг HTML-ответа в Markdown
│   ├── core/
│   │   └── page-utils.js   # helpers для puppeteer (waitAndTypeX и т.п.)
│   └── utils/
│       ├── logger.js    # запись в worker/logs/application.log
│       └── helpers.js   # sleep и т.п.
├── .env                 # секреты (логин/пароль DeepSeek и настройки пула)
├── Dockerfile
├── docker-compose.linux.yml
└── package.json
```

---

## Конфигурация (`.env`)

| Переменная | Значение по умолчанию | Описание |
|---|---|---|
| `PORT` | `3000` | На каком порту слушает Express |
| `MAX_BOT_COUNT` | `3` | Максимум одновременных ботов в пуле. Каждый бот = отдельный Chrome, кушает ~150-250 MB RAM |
| `RETRY_AFTER_SEC` | `3` | Что писать в HTTP-заголовок `Retry-After` при 429/503 |
| `REQUEST_TIMEOUT_MS` | `180000` | Таймаут на один запрос к DeepSeek (3 минуты) |
| `SERVICE_MODEL` | `deepseek` | Ключ сервиса из `worker/data.json`. Сейчас поддерживается только `deepseek` |
| `HEADLESS` | `true` | Запускать ли Chrome без окна. В коде сейчас захардкожено `"new"` — параметр игнорируется |
| `VIEWPORT_W` / `VIEWPORT_H` | `800` / `800` | Размер viewport браузера |
| `BOT_USERNAME` | — | Email от аккаунта DeepSeek (**обязательно**) |
| `BOT_PASSWORD` | — | Пароль от аккаунта DeepSeek (**обязательно**) |

Пример `.env`:

```env
PORT=3000
MAX_BOT_COUNT=15
RETRY_AFTER_SEC=45
REQUEST_TIMEOUT_MS=180000

SERVICE_MODEL=deepseek
HEADLESS=true
VIEWPORT_W=800
VIEWPORT_H=800

BOT_USERNAME=your_email@gmail.com
BOT_PASSWORD=your_password
```

> ⚠️ Не коммить `.env` в git. Если репа публичная — обязательно ротируй пароль.

---

## Запуск

### Через Docker Compose (рекомендуется)

Требует, чтобы существовала external network `shared-network` (её обычно создаёт основной docker-compose Django-проекта):

```bash
docker network create shared-network   # один раз, если ещё нет
docker compose -f docker-compose.linux.yml up -d --build
```

Логи:
```bash
docker logs -f bot_pool_api
```

Остановка:
```bash
docker compose -f docker-compose.linux.yml down
```

### Локально (без Docker)

Нужен Node.js 20+ и Chromium, который скачает puppeteer.

```bash
npm install
node api/index.js
```

В консоли должно быть:
```
[api] listening on :3000
[api] MAX_BOT_COUNT=3
```

Первый бот стартанёт лениво — при первом запросе к `/api/send` или `/v1/chat/completions`.

---

## HTTP API

### `GET /health`

Проверка живости + список активных ботов. Боты, чьё окно/вкладка были закрыты, в список не попадают.

```json
{
  "ok": true,
  "bots": [
    { "id": 1, "state": "ready" },
    { "id": 2, "state": "busy" }
  ]
}
```

Возможные состояния:
- `starting` — бот запускается, ещё не залогинен
- `ready` — готов принимать запрос
- `busy` — обрабатывает запрос
- `not_autorized` — логин не прошёл (неверный пароль или DeepSeek просит капчу)
- `failed` — упал в процессе работы

---

### `POST /api/send` — простой формат

Минималистичный формат, удобный для своих сервисов.

**Запрос:**

```json
{
  "message": "Привет, как дела?",
  "model": "deepseek",
  "user_id": "user-42",
  "thinking": false,
  "conversation_id": "chat-123"
}
```

| Поле | Обязательное | Описание |
|---|---|---|
| `message` | ✅ | Текст пользователя |
| `model` | ✅ | Сервис из `data.json`. Сейчас всегда `deepseek` |
| `user_id` или `conversation_id` | ❌ | Ключ для разделения диалогов. Если не задан — используется `default` (все будут писать в один общий диалог!) |
| `thinking` | ❌ | `true` — включить DeepThink (reasoning), `false` — обычный режим |

**Успешный ответ (200):**

```json
{
  "ok": true,
  "data": {
    "content": "Привет! Всё отлично, спасибо. Чем могу помочь?"
  }
}
```

**Ошибки:**

| HTTP | `reason` | Что значит |
|---|---|---|
| `400` | `message and model are required` | Не передал обязательное поле |
| `401` | `not_autorized` | Логин/пароль неверный, или DeepSeek заблокировал аккаунт. Проверь `BOT_USERNAME` / `BOT_PASSWORD` |
| `429` | `All bots are busy...` | Достигнут лимит `MAX_BOT_COUNT`, все боты заняты. Retry-After в заголовке |
| `503` | `No bots ready. Starting a bot...` | Бот ещё инициализируется. Retry-After в заголовке |
| `500` | `Internal error` (или текст ошибки) | Что-то сломалось в процессе. Смотри логи |

---

### `POST /v1/chat/completions` — OpenAI-совместимый формат

Подходит для готовых OpenAI SDK / клиентов, которые умеют менять `base_url`.

**Запрос:**

```json
{
  "model": "gpt-4o",
  "messages": [
    { "role": "user", "content": "Привет!" }
  ],
  "thinking": false,
  "conversation_id": "chat-123"
}
```

> Поле `model` в теле сейчас игнорируется (всё уходит в DeepSeek). Заголовок `x-conversation-id` тоже принимается как `conversation_id`.

**Важно:** из массива `messages` бот берёт только **последнее user-сообщение**. Историю он ведёт **сам**, в памяти, по `conversation_id`. То есть нельзя через этот endpoint переписать историю — все предыдущие assistant-ответы он помнит из своего `hist`.

**Успешный ответ (200):**

```json
{
  "id": "chatcmpl_abc123...",
  "object": "chat.completion",
  "created": 1730000000,
  "model": "gpt-4o",
  "choices": [
    {
      "index": 0,
      "message": { "role": "assistant", "content": "Привет!" },
      "finish_reason": "stop"
    }
  ],
  "usage": { "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0 }
}
```

**Ошибки** — в формате OpenAI:

```json
{
  "error": {
    "message": "Bot is not authorized (login failed). Check BOT_USERNAME/BOT_PASSWORD.",
    "type": "invalid_request_error",
    "param": null,
    "code": "not_autorized"
  }
}
```

Возможные коды ошибок: `invalid_request`, `not_autorized`, `bot_starting`, `bots_busy`, `internal_error`.

---

## Как живёт пул ботов

1. **Ленивая инициализация.** Боты не стартуют при запуске сервиса. Первый запрос триггерит создание первого бота → 503 с `Retry-After`. Клиент должен повторить запрос.
2. **Один новый бот за раз.** Пока один бот стартует (логинится), новые не создаются — чтобы не задудосить страницу логина DeepSeek.
3. **Каждые 5 секунд** реап-таймер чистит «мёртвых» ботов — у кого браузер закрылся, или страница упала.
4. **`NOT_AUTORIZED`-боты не удаляются** — они нужны, чтобы API мог вернуть 401 с понятной причиной. Если поправил `.env` и хочешь, чтобы пул попробовал залогиниться заново — перезапусти сервис.
5. **История диалогов** живёт в `worker/hist.js` (in-memory). **При перезапуске сервиса вся история теряется.** Если нужна персистентность — это TODO.

---

## Поддерживаемые модели

Сейчас всего одна:

- `deepseek` — DeepSeek V3/V4 chat (с переключателем DeepThink через флаг `thinking`)

Добавить новый сервис — это:
1. Прописать URL'ы и XPath'ы в `worker/data.json` под новым ключом
2. Перезапустить

Подопытно работают логин/набор текста/клик отправки и парсинг ответа. На DeepSeek специфика парсинга HTML-ответа в Markdown лежит в `worker/modules/promtps.js` → `deepseekHtmlToApiMarkdown`. Для другого провайдера нужен свой конвертер.

---

## Логи и диагностика

- **Application лог:** `worker/logs/application.log` — старт ботов, логины, клики
- **Stdout/stderr Express'а:** прёт в `docker logs bot_pool_api`
- **Префикс `[bot#N]`** в логах — номер бота из пула

Что искать при проблемах:

```bash
# логин не проходит
docker logs bot_pool_api 2>&1 | grep -E "login|auth"

# бот падает
docker logs bot_pool_api 2>&1 | grep -E "failed|error|stack"

# что вообще происходит сейчас
curl http://localhost:3000/health
```

---

## Типичные ошибки

### `401 not_autorized`

- Проверь `BOT_USERNAME`/`BOT_PASSWORD` в `.env` — нет ли лишних пробелов, кавычек, экранирования
- Попробуй вручную залогиниться на chat.deepseek.com тем же логином — может, аккаунт временно залочен или DeepSeek просит капчу. Капчу бот не умеет решать.
- В Docker `.env` загружается из `env_file: .env` — убедись, что файл точно есть в контексте сборки

### `503 No bots ready. Starting a bot.`

Это **нормально на первый запрос** после старта сервиса. Бот логинится 5–10 секунд. Просто ретрайни через `Retry-After` секунд.

Если 503 не уходит:
- Посмотри `docker logs` — возможно, логин падает с исключением
- Возможно, заблокирована страница chat.deepseek.com из твоего региона — нужен прокси (сейчас не настроен в рантайме, только при сборке)

### `429 All bots are busy`

Текущий `MAX_BOT_COUNT` исчерпан. Либо подними лимит (но помни про RAM ~200 MB на бота), либо ретрайни через `Retry-After`.

### `500` с `waitLastOuterHtmlStable timeout`

DeepSeek не отдал ответ за `REQUEST_TIMEOUT_MS`. Бывает на сложных запросах с DeepThink. Подними таймаут, или переотправь запрос.

### `402` от клиента (Django-стороны)

⚠️ **Этот код возвращает не bot-pool-api**, а официальный DeepSeek API. Если в Django выставлен `DEEPSEEK_API_TOKEN` — запрос сначала идёт туда, а не в этот бот. 402 = недостаточно средств на платформе DeepSeek.

Решение: либо пополнить баланс, либо убрать `DEEPSEEK_API_TOKEN` из env Django, чтобы запросы шли только в web-бот.

---

## Ограничения

- **Капча.** Если DeepSeek покажет капчу при логине — бот не пройдёт, вернётся `NOT_AUTORIZED`.
- **Один аккаунт = много ботов.** Все боты пула логинятся под одним и тем же `BOT_USERNAME`. DeepSeek может это заметить и ограничить.
- **Память.** Каждый бот — отдельный Chrome ≈ 150–250 MB. `MAX_BOT_COUNT=15` это уже 2–4 GB только под бота.
- **История в памяти.** Перезапуск сервиса = чистая история всех диалогов.
- **Только один сервис.** `data.json` сейчас содержит только DeepSeek. Поддержки ChatGPT/Claude/etc. в коде есть «места под», но XPath'ов нет.
- **Поле `model` из OpenAI-запроса игнорируется** — всегда отвечает DeepSeek.
- **Стриминга нет.** Ответ возвращается целиком, после того как DeepSeek закончил генерацию.
- **DOM может поменяться.** Если DeepSeek обновит вёрстку — все XPath'ы в `data.json` нужно будет перепрописывать.
