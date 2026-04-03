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
- `HTTP_PROXY`
- `HTTPS_PROXY`
- `ALL_PROXY` (опционально; некоторые утилиты/библиотеки читают именно его)
- `NO_PROXY`
- `PROXY`

Рекомендуемые для продакшена переменные:

- `DEBUG=0`
- `ALLOWED_HOSTS=dl.gsu.by,dlai.gsu.by`
- `CSRF_TRUSTED_ORIGINS=https://dl.gsu.by,https://dlai.gsu.by`
- `USE_X_FORWARDED_PROTO=1` (если TLS терминируется на nginx/прокси)

Если прокси вам не нужен (или имя прокси не резолвится на сервере), оставьте `HTTP_PROXY/HTTPS_PROXY/PROXY` пустыми или удалите эти строки.

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
ENV_FILE=.env bash server-up.sh
```

Для продакшена (рекомендуется: без bind-mount исходников, nginx на 127.0.0.1:8081):

```bash
COMPOSE_PROD=1 ENV_FILE=.env bash server-up.sh
```

Эквивалентно вручную:

```bash
docker compose --env-file .env -f docker-compose.yml -f docker-compose.prod.yml up -d --build
docker compose --env-file .env exec -T web python manage.py migrate
docker compose --env-file .env exec -T web python manage.py collectstatic --noinput
docker compose ps
```

По умолчанию `docker-compose.prod.yml` публикует nginx как `127.0.0.1:8081->80`.
Если нужно иначе, перед запуском задайте:

```bash
export AI_NGINX_BIND=127.0.0.1
export AI_NGINX_PORT=8081
```

Скрипт сам:

- соберёт контейнеры
- запустит их
- выполнит миграции
- выполнит `collectstatic`

## 5. Проверить, что всё работает

Локально на сервере (внутри него):

```bash
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
ENV_FILE=.env bash server-up.sh
```

Если вы используете прод-override:

```bash
git pull
docker compose --env-file .env -f docker-compose.yml -f docker-compose.prod.yml up -d --build
docker compose --env-file .env exec -T web python manage.py migrate
docker compose --env-file .env exec -T web python manage.py collectstatic --noinput

```

---

## 7. Прокси на стороне `dl.gsu.by` (важно для логина)

На `dl.gsu.by` должен быть настроен reverse-proxy, который:

- пропускает в AI только уже авторизованных пользователей (как именно — зависит от того, как у вас устроен логин на основном сайте)
- проксирует HTTP и WebSocket на `http://127.0.0.1:8081`

Пример фрагмента nginx-конфига (на стороне основного сайта):

```nginx
location /ai/ {
	proxy_pass http://127.0.0.1:8081;
	proxy_http_version 1.1;

	proxy_set_header Host $host;
	proxy_set_header X-Real-IP $remote_addr;
	proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
	proxy_set_header X-Forwarded-Proto https;

	# WebSocket для /ai/chat/ws/...
	proxy_set_header Upgrade $http_upgrade;
	proxy_set_header Connection "upgrade";
}
```

Если у вас есть отдельная локация под статику на основном nginx, можно сделать отдельный `location /ai/static/` и добавить кэширование — но это необязательно: внутри стека уже есть nginx, который отдаёт `/ai/static/`.
