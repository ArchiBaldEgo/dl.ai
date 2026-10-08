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
- [Путь новичка: запуск с нуля (WSL2 + Docker)](docs/getting_started.md)
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

## Локальный запуск (Docker, один комплект)

> Полный путь «с нуля» для человека без опыта — в [docs/getting_started.md](docs/getting_started.md) (WSL2, вплоть до кнопок). Ниже — краткий рецепт.

Один комплект Docker обслуживает и локальный запуск, и прод: отдельных
override-файлов и локальных флагов не нужно. Режим определяется автоматически
по единому правилу (`ai/env_mode.py`): **есть прокси в `.env` → prod; прокси
пуст/отсутствует → локальный запуск**.

1. Установите Docker и войдите в группу:

```bash
sudo apt update && sudo apt install -y docker.io docker-compose
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER" && newgrp docker
```

2. Создайте минимальный `.env` (секрет Django + учётка локальной БД — всё прочее приложение подставит само):

```bash
printf 'SECRET_KEY=django-insecure-%s\nDB_USER=localai\nDB_PASSWORD=local-ai-db-pass-01\n' "$(python3 -c 'import secrets; print(secrets.token_urlsafe(40))')" > .env
```

   Ключи моделей (`OLLAMA_API_KEY`/`OPENROUTER_API_KEY`) допишите в `.env` через редактор (запросите у владельца проекта). Строки `HTTP_PROXY=`/`HTTPS_PROXY=` НЕ заполняйте — именно пустые прокси означают «локально». Остальные переменные, если понадобятся, — с подписями в `.env.example`.

3. Запуск одной командой (первая сборка 10–20 минут, последующие быстрее):

```bash
docker compose up -d --build
```

4. Откройте `http://localhost:8000/ai/chat/` — вы автоматически залогинены под
`dev_admin` (суперпользователь); `/ai/admin/` доступен полностью. Через nginx
тот же сайт: `http://localhost:8080/ai/chat/`.

Остановить / посмотреть логи:

```bash
docker compose down
docker compose logs -f web
```

Ожидаемые локально деградации (это не баги): фичи, требующие живой сессии
dl.gsu.by («Реши задачу» через DL, «Тестирование», ARM batch-solve), сообщат
«sessionId обязателен» / «Нет DLSID»; модели `Web_DeepSeek`/`Web_Kimi` офлайн
(пулы требуют seed-логина, см. [docs/web_deepseek_bot.md](docs/web_deepseek_bot.md)).

**Prod** — тот же `docker compose up -d --build` на сервере, где в `.env`
прописан корпоративный прокси: миграции делаются по [DEPLOY.md](DEPLOY.md).

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
