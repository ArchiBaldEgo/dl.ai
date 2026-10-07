# Запуск dl.ai с нуля (для новичка)

Всё, что нужно человеку, который впервые открыл этот проект. Путь проверен: Windows 10/11 + WSL2 Ubuntu → приложение работает **без Docker, без PostgreSQL, без Redis, без куки dl.gsu.by**.

Время: первый раз ~20–30 минут (большая часть — скачивание пакетов).

---

## Шаг 1. WSL2 (Windows → Linux-подсистема)

1. Откройте **PowerShell от администратора** (Пуск → наберите «PowerShell» → правая кнопка → «Запуск от имени администратора»).
2. Выполните:

```powershell
wsl --install
```

3. Перезагрузите Windows (Пуск → «Завершение работы» → «Перезагрузка»).
4. После перезагрузки откроется окно Ubuntu: придумайте имя пользователя и пароль (это пароль Linux-подсистемы, не Windows). Если окно не открылось — Пуск → наберите «Ubuntu».
5. Обновите пакеты (появится окно терминала — это и есть ваш рабочий терминал):

```bash
sudo apt update && sudo apt -y upgrade
```

Пароль, который вы вводите, не отображается — это нормально, просто набирайте и жмите Enter.

## Шаг 2. Код проекта

В терминале Ubuntu:

```bash
sudo apt install -y git
git config --global user.name "Ваше Имя Фамилия"
git config --global user.email "ваш.email@example.com"
git clone https://github.com/ArchiBaldEgo/dl.ai.git
cd dl.ai
```

Репозиторий публичный — доступы не нужны.

**Своя ветка** (ваш рабочий вариант кода, «привязанный» к main):

```bash
git switch -c my-feature        # создаст ветку с вашим именем
```

Так вы работаете в своей ветке и НЕ меняете main. Чтобы подтянуть чужие изменения из main к себе:

```bash
git fetch origin
git rebase origin/main
```

> Храните проект в домашней папке WSL (`~/dl.ai`), а не на `/mnt/c/...` / `/mnt/d/...` — доступ к дискам Windows из Linux заметно медленнее.

## Шаг 3. Python и файлы настроек

```bash
sudo apt install -y python3-venv python3-pip
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt     # 3–5 минут
```

Теперь два файла настроек (оба создаются одной командой «копипаст», целиком):

**1) `.env`** — общий файл-шаблон (нужен хотя бы SECRET_KEY):

```bash
cp .env.example .env
sed -i "s/^SECRET_KEY=$/SECRET_KEY=$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')/" .env
```

**2) `.env.local`** — локальные overrides поверх шаблона (Docker/БД/Redis/куки не нужны):

```bash
cat > .env.local <<'EOF'
# Локальный запуск: всё в памяти и sqlite.
DEBUG=1
AI_DEV_AUTH_BYPASS=1

DB_ENGINE=django.db.backends.sqlite3
DB_NAME=local_db.sqlite3

REDIS_URL=
AI_CHANNEL_LAYER=inmemory

AI_DISABLE_HEALTH_SCHEDULER=1
AI_WEB_DEEPSEEK_AUTORECOVERY=0
AI_WEB_KIMI_AUTORECOVERY=0

CSRF_COOKIE_DOMAIN=
SESSION_COOKIE_DOMAIN=

SECURE_SSL_REDIRECT=0
HTTP_PROXY=
HTTPS_PROXY=
ALL_PROXY=
PROXY=
EOF
```

> `.env` грузится кодом сам, его никуда не нужно «подставлять» руками при запуске. А вот `.env.local` нужно один раз объявить в сеансе (см. Шаг 4).

## Шаг 4. Запуск

```bash
set -a; source .env.local; set +a
.venv/bin/python manage.py migrate
.venv/bin/python manage.py runserver
```

`runserver` не завершается — это и есть работающий сервер (Ctrl+C — остановить).

Откройте в Windows-браузере: **http://localhost:8000/ai/chat/**

Вы автоматически войдёте под пользователем `dev_admin` (суперпользователь, без кук и dl.gsu.by). Админка: http://localhost:8000/ai/admin/

Повторный запуск в будущем — те же три строки из Шага 4 (один раз на открытие терминала, `migrate` можно не повторять).

## Шаг 5. Проверьте себя

- `http://localhost:8000/health` — должен вернуть `{"ok": true}`.
- Полный автотест-сьют (несколько минут):

```bash
.venv/bin/python manage.py test ai --settings=DjangoTest.test_settings
```

Ожидаемый итог: `OK` (примерно 480 тестов). `skipped=1` — нормально.

## Что локально НЕ работает (это не поломка)

| Фича | Почему | Что видите |
|---|---|---|
| «Реши задачу» через DL / «Тестирование» | нужна живая сессия dl.gsu.by | «sessionId обязателен» |
| ARM-пакетное решение | нужен DLSID | «Нет DLSID — требуется авторизация на dl.gsu.by» |
| Модели `Web_DeepSeek`, `Web_Kimi` | пулы :3000/:3001 не запущены | «Ошибка подключения…» |
| Модели Ollama/OpenRouter | нужны ключи из `.env` | в селекторе «нет доступных моделей» (запросите ключи у владельца проекта) |

Пустой список моделей после первого открытия страницы — подождите 1–2 минуты и обновите: страница сама запускает проверку моделей.

## Если что-то не так (FAQ)

| Симптом | Причина / решение |
|---|---|
| Постоянный редирект на dl.gsu.by | Забыли `source .env.local` в этом сеансе — повторите Шаг 4 |
| Чат мгновенно отваливается, `4403` | Куки не сохранились: проверьте, что в `.env.local` есть пустые `CSRF_COOKIE_DOMAIN=` и `SESSION_COOKIE_DOMAIN=` |
| `Port 8000 is already in use` | Уже запущен второй сервер: закройте его (Ctrl+C) или запустите на другом порту: `runserver 0.0.0.0:8001` |
| `database is locked` | sqlite на Windows-диске медленный: в `.env.local` поменяйте `DB_NAME=~/dlai_local.sqlite3` и повторите `migrate` |
| В чате «Ошибка: сессия истекла» | Обновите страницу и повторите |
| Список моделей пуст долго (>5 мин) | Нет ключей моделей в `.env` — см. таблицу выше |

## Git-хуки (необязательно)

```bash
bash scripts/setup_hooks.sh
```

Добавляют автообновление таблицы обновлений при pull/push. На машине без Docker хуки просто ничего не делают — вреда нет.