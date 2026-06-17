# Запуск проекта на сервере (prod, без sudo)

Контекст для вашего кейса: логин происходит на основном сайте `https://dl.gsu.by`, и после успешной авторизации пользователь заходит в `/ai/...`. Этот репозиторий **не реализует общий логин сам по себе** (в `ai/views.py` нет `login_required`), поэтому типовой вариант для продакшена — держать этот стек **за** существующим фронтовым nginx/прокси на `dl.gsu.by`:

- основной сайт терминирует HTTPS и делает авторизацию
- запросы на `/ai/` (и WebSocket `/ai/chat/ws/...`) проксируются во внутренний порт этого стека
- сам стек слушает **порт >1024** (работает без `sudo` и не конфликтует с 80/443)

## 1. Скачать проект

```bash
git clone <URL_ВАШЕГО_РЕПО>
cd dl.ai
```

Если проект уже скачан:

```bash
git pull
```

## 2. Создать `.env`

Скопируйте шаблон:

```bash
cp .env.example .env
nano .env
```

Заполните в `.env` минимум эти поля:

- `SECRET_KEY`
- `DB_USER`
- `DB_PASSWORD`
- `DB_NAME`
- `HTTP_PROXY` (опционально)
- `HTTPS_PROXY` (опционально)
- `ALL_PROXY` (опционально; некоторые утилиты/библиотеки читают именно его)
- `NO_PROXY` (опционально)
- `PROXY` (опционально)
- `REDIS_URL` (обязательно для production)
- `REDIS_VOLUME_NAME` (опционально; имя Docker volume для Redis)

### Redis

После рефакторинга WebSocket consumer'а и внедрения rate limiting проект использует Redis через Django cache для:

- общей истории диалогов между несколькими Daphne/Gunicorn воркерами,
- корректной работы rate limiting между воркерами.

**Без Redis в multi-worker конфигурации:**

- история диалогов будет теряться при перезапуске процесса,
- разные воркеры увидят разную историю,
- rate limiting не будет работать (счётчики разные в каждом процессе).

**Значение `REDIS_URL`:**

- Внутри Docker Compose (по умолчанию): `redis://redis:6379/1`
- При локальном запуске без Docker: `redis://127.0.0.1:6379/1`

Если `REDIS_URL` не задан, Django использует `LocMemCache`. Это допустимо только для разработки с одним процессом. **Для production `REDIS_URL` обязателен.**

Рекомендуемые для продакшена переменные:

- `DEBUG=0`
- `ALLOWED_HOSTS=dl.gsu.by,dlai.gsu.by`
- `CSRF_TRUSTED_ORIGINS=https://dl.gsu.by,https://dlai.gsu.by`
- `CSRF_COOKIE_NAME=ai_csrftoken`
- `SESSION_COOKIE_NAME=ai_sessionid`
- `CSRF_COOKIE_PATH=/ai/`
- `SESSION_COOKIE_PATH=/ai/`
- `CSRF_USE_SESSIONS=1`
- (опционально) Фоллбек, если остались старые `csrftoken` cookies: их значения будут мигрироваться в сессию автоматически.
- Если используете и `dl.gsu.by`, и `dlai.gsu.by`, дополнительно задайте:
  - `CSRF_COOKIE_DOMAIN=.gsu.by`
  - `SESSION_COOKIE_DOMAIN=.gsu.by`
- `USE_X_FORWARDED_PROTO=1` (если TLS терминируется на nginx/прокси)

Если прокси вам не нужен (или имя прокси не резолвится на сервере), оставьте `HTTP_PROXY/HTTPS_PROXY/PROXY` пустыми или удалите эти строки.

> **Обязательно:** убедитесь, что в `.env` есть строка `REDIS_URL=redis://redis:6379/1`. Без неё в production rate limiting и общая история диалогов работать не будут.

Если AI-токены у вас есть, тоже заполните их.

Для SambaNova можно использовать либо `SC_TOKEN`, либо `SAMBANOVA_API_KEY` (достаточно одной переменной).

Если в логине/пароле прокси есть спецсимволы (`\\`, `@`, `:`, `%`, пробел), указывайте их в URL-encoded виде.
Пример для `\\` в логине: `%5C`.
Пример:

```env
HTTP_PROXY='http://domain%5Cuser:pa%40ss@proxy.host:3128/'
HTTPS_PROXY='http://domain%5Cuser:pa%40ss@proxy.host:3128/'
PROXY='http://domain%5Cuser:pa%40ss@proxy.host:3128/'
```

Если видите `Ошибка API (код 401)`, это обычно означает одну из причин:
- пустой/неверный токен (`SC_TOKEN`/`SAMBANOVA_API_KEY`)
- токен без доступа к выбранной модели
- прокси требует авторизацию и отдает 401/подменяет ответ

## 3. Условия на сервере

Нужно заранее (один раз):

- Установленный Docker Engine
- Доступ пользователя к Docker daemon (обычно группа `docker`)
- Docker Compose v2 (`docker compose`)

Проверка:

```bash
docker version
docker compose version
docker compose ps
```

## 4. Запустить проект

Для стандартного запуска (dev-like, nginx на 8080):

```bash
docker compose --env-file .env up -d --build
docker compose --env-file .env exec -T web python manage.py migrate
docker compose --env-file .env exec -T web python manage.py collectstatic --noinput
docker compose exec -T redis redis-cli ping
docker compose ps
```

Для продакшена (nginx на 127.0.0.1:8081):

```bash
AI_NGINX_BIND=127.0.0.1 AI_NGINX_PORT=8081 docker compose --env-file .env up -d --build
docker compose --env-file .env exec -T web python manage.py migrate
docker compose --env-file .env exec -T web python manage.py collectstatic --noinput
docker compose exec -T redis redis-cli ping
docker compose ps
```

Эквивалентно вручную:

```bash
docker compose --env-file .env up -d --build
docker compose --env-file .env exec -T web python manage.py migrate
docker compose --env-file .env exec -T web python manage.py collectstatic --noinput
docker compose exec -T redis redis-cli ping
docker compose ps
```

Если нужно иначе, перед запуском задайте:

```bash
export AI_NGINX_BIND=127.0.0.1
export AI_NGINX_PORT=8081
```

Команды выше:

- соберут контейнеры
- запустят их
- выполнят миграции
- выполнят `collectstatic`

## 5. Проверить, что всё работает

Локально на сервере (внутри него):

```bash
# Проверить, что Redis отвечает (должен вернуть PONG)
docker compose exec -T redis redis-cli ping

# Проверить, что веб-часть отвечает
curl -I http://127.0.0.1:8081/ai/chat/
```

```bash
docker compose ps
docker compose logs -f web
```

## 6. Обновление из git

Просто снова выполните:

```bash
git pull
docker compose --env-file .env up -d --build
docker compose --env-file .env exec -T web python manage.py migrate
docker compose --env-file .env exec -T web python manage.py collectstatic --noinput
```

Если вы используете прод-порт 8081:

```bash
git pull
AI_NGINX_BIND=127.0.0.1 AI_NGINX_PORT=8081 docker compose --env-file .env up -d --build
docker compose --env-file .env exec -T web python manage.py migrate
docker compose --env-file .env exec -T web python manage.py collectstatic --noinput

```

---

## 7. Прокси на стороне `dl.gsu.by` (важно для логина)

На внешнем nginx (основной сайт) должны быть отдельные локации для:

- статики `/ai/static/`
- websocket `/ai/chat/ws/`
- остальных запросов `/ai/`

Важно: порядок локаций должен быть именно такой (сначала static, потом ws, потом общий `/ai/`).

Готовый сниппет лежит в `nginx/external-dl.gsu.by.example.nginx-snippet`.
Это файл-шаблон для внешнего nginx: не кладите его в `/etc/nginx/conf.d` контейнера этого проекта.

Пример рабочего фрагмента:

```nginx
location ^~ /ai/static/ {
	proxy_pass http://127.0.0.1:8081/ai/static/;
	proxy_http_version 1.1;

	proxy_set_header Host 127.0.0.1;
	proxy_set_header X-Forwarded-Proto https;
	proxy_set_header X-Real-IP $remote_addr;
	proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;

	# Защита от проблем с промежуточным gzip/прокси-кэшем
	proxy_set_header Accept-Encoding "";
	proxy_redirect off;
}

location /ai/chat/ws/ {
	proxy_pass http://127.0.0.1:8081/ai/chat/ws/;
	proxy_http_version 1.1;

	proxy_set_header Upgrade $http_upgrade;
	proxy_set_header Connection "upgrade";

	proxy_set_header Host $host;
	proxy_set_header X-Forwarded-Proto https;
	proxy_set_header X-Real-IP $remote_addr;
	proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}

location /ai/ {
	proxy_pass http://127.0.0.1:8081/ai/;
	proxy_http_version 1.1;

	proxy_set_header Host $host;
	proxy_set_header X-Forwarded-Proto https;
	proxy_set_header X-Real-IP $remote_addr;
	proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}
```

После изменения внешнего nginx:

```bash
nginx -t
sudo systemctl reload nginx
```

Проверка:

```bash
curl -I https://dl.gsu.by/ai/static/admin/css/chat_template.css
curl -I https://dl.gsu.by/ai/static/admin/js/chat_template.js
```

Ожидается `HTTP 200` и корректные MIME-типы (`text/css`, `application/javascript`/`text/javascript`).

---

## Для разработчиков

Полная техническая документация находится в [`doc/Документация для разработчика.md`](doc/Документация%20для%20разработчика.md). Общая инструкция для разработчиков — в [`DOCX.md`](../DOCX.md#инструкция-для-разработчика).

### Локальный запуск без Docker

Требования: Python 3.11+, PostgreSQL 14+.

```bash
cp .env.example .env
# Установите DB_HOST=127.0.0.1 и параметры базы в .env
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver 0.0.0.0:8000
```

Приложение будет доступно на `http://127.0.0.1:8000/ai/...`.

### Локальный запуск в Docker

```bash
cp .env.example .env
# Установите DB_HOST=db и остальные параметры в .env
# REDIS_URL уже прописан как redis://redis:6379/1 в .env.example
docker compose --env-file .env up -d --build
docker compose --env-file .env exec -T web python manage.py migrate
docker compose --env-file .env exec -T web python manage.py collectstatic --noinput
docker compose exec -T redis redis-cli ping
```

### Тестирование и проверки

```bash
python manage.py test ai
python manage.py check
python manage.py makemigrations --check
python manage.py collectstatic --noinput
```

### Git workflow

- Каждый разработчик работает в отдельной ветке, названной по фамилии.
- По готовности открывается Pull Request в `main`.
- Перед PR убедитесь, что тесты проходят и документация обновлена.
