# dl.ai

Django + Channels (ASGI, Daphne). UI and API live under `/ai/...`; доступ берётся из сессии основного сайта через middleware и cookie `DLSID`.

## Регламент работы для студентов (обязательно)

1. Студент берёт текущий проект и выполняет своё ТЗ **только в своей отдельной ветке со своей фамилией**.
2. После завершения работы студент открывает Pull Request из своей ветки в `main`.
3. Изменения проверяются через нейросеть и вручную перед подтверждением.
4. Если всё корректно, PR мерджится в `main`, затем изменения выкатываются на боевой `dl`.
5. Если есть ошибки или ТЗ выполнено неверно, отправляется обратная связь на почту.

Перед началом работы студент должен отправить письмо на `vadik2005guryanov@gmail.com` с:
- своим `GitHub nickname` (username);
- своей почтой для связи.

## Обратная связь

`vadik2005guryanov@gmail.com`

## Документация

- [Инструкция для пользователя](DOCX.md#инструкция-для-пользователя)
- [Инструкция для тестера](DOCX.md#инструкция-для-тестера)
- [Инструкция для системного администратора](DOCX.md#инструкция-для-системного-администратора)
- [Инструкция для суперадмина](DOCX.md#инструкция-для-суперадмина)
- [Инструкция для разработчика](DOCX.md#инструкция-для-разработчика) / [подробная техническая документация](doc/Документация%20для%20разработчика.md)
- [Bot pool (Web DeepSeek)](docs/web_deepseek_bot.md)
- [Запуск на сервере](DEPLOY.md)

## Разграничение доступа к препромптам (Prompt ACL)

- Рабочая группа доступа — `prompt_developer`.
- Участник `prompt_developer` видит ARM и все промпты в `/ai/admin/ai/prompt/`.
- Ссылка "Мой препромпт" открывает только свои/закреплённые промпты (`/ai/admin/ai/prompt/?mine=1`).
- Создаваемый разработчиком промпт автоматически закрепляется за ним (`owner`) и добавляет его в `editors`.
- Редактировать разработчик может только свои промпты (`owner`) и ранее назначенные через `editors`; чужие — только просмотр.
- Назначение разработчиков на конкретный промпт и изменение `owner` делает администратор/сотрудник.

Команды для сервера (через Docker):

```bash
docker compose --env-file .env exec -T web python manage.py shell -c "from ai.models import Prompt; print(*[f'{p.id}: {p.prompt_name} | owner={(p.owner.username if p.owner else \"-\")} | editors={[u.username for u in p.editors.all()]}' for p in Prompt.objects.select_related('owner').prefetch_related('editors').order_by('id')], sep='\n')"
```

```bash
docker compose --env-file .env exec -T web python manage.py shell -c "from django.contrib.auth.models import User; print(*[f'{u.id}: {u.username}' for u in User.objects.order_by('username')], sep='\n')"
```

## Локальный запуск (без Docker, WSL-native)

> Полный путь «с нуля» для человека без опыта — в [docs/getting_started.md](docs/getting_started.md) (WSL2, вплоть до кнопок). Ниже — краткий рецепт для уже установленной машины.

Нужны только `Python 3.10+` и существующий `.venv` — Postgres/Redis/DLSID-кука не нужны. `'daphne'` стоит первым в `INSTALLED_APPS`, поэтому `manage.py runserver` обслуживает WebSocket (+static при `DEBUG=1`) наряду с HTTP; prod не затронут (Dockerfile CMD запускает daphne напрямую).

1. Создайте `.env.local` (untracked, в git не попадает). Он накладывается **поверх** `.env`: `settings.py` грузит `.env` через `load_dotenv(..., override=False)`, так что экспортированные отсюда значения побеждают, а сам секретный `.env` в шелл сурсить не нужно.

```bash
DEBUG=1
AI_DEV_AUTH_BYPASS=1                 # авто-логин dev_admin (суперпользователь) без DLSID
DB_ENGINE=django.db.backends.sqlite3
DB_NAME=/mnt/d/GitHub/dlai/local_db.sqlite3
REDIS_URL=                           # без Redis: LocMemCache
AI_CHANNEL_LAYER=inmemory            # без Redis-сервера: слой Channels в памяти
AI_DISABLE_HEALTH_SCHEDULER=1
AI_WEB_DEEPSEEK_AUTORECOVERY=0       # пулов :3000/:3001 локально нет
AI_WEB_KIMI_AUTORECOVERY=0
CSRF_COOKIE_DOMAIN=                  # критично: .env прибивает домен кук к .gsu.by —
SESSION_COOKIE_DOMAIN=               # на localhost браузер их не сохранит (WS → close 4403)
SECURE_SSL_REDIRECT=0
```

2. Загрузите overrides и запустите (`.env` подхватывается кодом сам):

```bash
cd /mnt/d/GitHub/dlai
set -a; source .env.local; set +a
.venv/bin/python manage.py migrate
.venv/bin/python manage.py runserver
```

   Забыли сурснуть `.env.local` → молча стартует прод-конфиг (`DEBUG=0` → редирект на dl.gsu.by и всё).

3. Откройте в браузере `http://localhost:8000/ai/chat/` — именно `localhost` (WSL2-форвардинг + `ALLOWED_HOSTS`), не eth0-IP WSL. Вы автоматически залогинены под `dev_admin`; `/ai/admin/` доступен полностью, set-password не нужен.

Ожидаемые локально деградации (это не баги): фичи, требующие живую сессию dl.gsu.by («Реши задачу» через DL, «Тестирование», ARM batch-solve), сообщат «sessionId обязателен» / «Нет DLSID»; модели `Web_DeepSeek`/`Web_Kimi` офлайн (пулы :3000/:3001 не подняты); API-модели ходят через корп-прокси из `.env`.

### Альтернатива — нативный PostgreSQL

Нужны `PostgreSQL 14+`, `psql`:

1. Создайте БД и пользователя в PostgreSQL:

```sql
CREATE USER dlaibd WITH PASSWORD 'dlaibd';
CREATE DATABASE dl_ai OWNER dlaibd;
```

2. Создайте `.env` из шаблона и для запуска без Docker выставьте `DB_HOST=127.0.0.1`:

```bash
cp .env.example .env
```

3. Установите зависимости и запустите:

```bash
python -m venv .venv
. .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver 0.0.0.0:8000
```

Приложение будет доступно на `http://127.0.0.1:8000/ai/...`.

## Run (local, Docker, без прокси)

1. Создайте `.env`:

```bash
cp .env.example .env
```

2. Убедитесь, что в `.env`:
- `DB_HOST=db`
- `DEBUG=1`
- `HTTP_PROXY`, `HTTPS_PROXY`, `PROXY` пустые (или удалены), если прокси не нужен

3. Для тестирования **без куки DLSID и без dl.gsu.by** добавьте в `.env` флаги
локального режима (все работают только с пустым prod-`.env`, где их нет):

```bash
AI_BOOTSTRAP_ON_START=1          # migrate + collectstatic при старте контейнера
AI_DEV_AUTH_BYPASS=1             # auto-login dev_admin (суперпользователь) без DLSID
AI_POOL_HEALTHCHECK_DISABLED=1   # healthcheck только Daphne (пулы без seed-логина не ready)
AI_DISABLE_HEALTH_SCHEDULER=1    # без 04:00 модельного health-скедулера
```

   затем — одна команда, без ручных exec-шагов:

```bash
docker compose up -d --build
```

4. Откройте `http://localhost:8080/ai/chat/` — вы автоматически залогинены
под `dev_admin` (суперпользователь); `/ai/admin/` доступен полностью.
DL-функции («Реши задачу», отправка решения) требуют реальной сессии dl.gsu.by —
локально вернут понятный 401/«Нет DLSID», это штатно.

Без этих флагов — прежний путь (ручные шаги после `up`):

```bash
docker compose up -d --build
docker compose exec -T web python manage.py migrate
docker compose exec -T web python manage.py collectstatic --noinput
docker compose exec -T web python manage.py sync_update_log
```

Остановка:

```bash
docker compose down
```

По умолчанию nginx доступен на `http://localhost:8080/ai/...`.

## Run (production)

See [DEPLOY.md](DEPLOY.md).

## Git hooks (установка после clone)

После `git clone` выполните один раз:

```bash
bash scripts/setup_hooks.sh
```

Это установит:
- **post-merge** — после `git pull` автоматически добавляет новые коммиты в таблицу `UpdateLog` (раздел «Обновления» в админке).
- **pre-push** — перед `git push` синхронизирует `UpdateLog` с локальными коммитами.

Hooks работают только на хосте (где есть `git`), подключаясь к Docker-контейнеру `web` для записи в БД.

## Журнал обновлений (UpdateLog)

В админке есть раздел **«Обновления»** (`/ai/admin/updates/`) — доступен только супер-админам.
Содержит таблицу всех коммитов: дата, содержание (на русском), автор.
Доступны фильтрация по автору, диапазону дат и текстовый поиск.

Таблица `UpdateLog` синхронизируется с `git log` командой `sync_update_log`.
Начиная с этой версии она **запускается автоматически при старте контейнера**
(см. `CMD` в `Dockerfile`), поэтому новые коммиты подтягиваются сразу после
`docker compose up -d --build`. Явный вызов
(`docker compose exec -T web python manage.py sync_update_log`) нужен только чтобы
подтянуть коммиты без перезапуска контейнера; git-хуки (`post-merge`/`pre-push`)
синхронизируют её и при `git pull`/`git push`.

Дата последнего обновления отображается на главной странице чата
(правый нижний угол, формат `dd.mm.yyyy`) и синхронизируется с последней записью в `UpdateLog`.

## Daily model availability checks

- Model health checks are executed once per day for the 04:00 MSK window.
- The scheduler starts automatically in web server processes (Daphne/Gunicorn/Uvicorn or Django runserver child process).
- To disable the built-in scheduler, set `AI_DISABLE_HEALTH_SCHEDULER=1`.

Manual run:

```bash
python manage.py check_models_health
python manage.py check_models_health --force
```
