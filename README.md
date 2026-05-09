# dl.ai

Django + Channels (ASGI, Daphne). UI and API live under `/ai/...`.

## Регламент работы для студентов (обязательно)

1. Студент берёт текущий проект и выполняет своё ТЗ **только в своей отдельной ветке cо своей фамилией**.
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
- [Запуск на сервере](DEPLOY.md)

## Разграничение доступа к препромптам (Prompt ACL)

- Группа `prompt_developer` создаётся автоматически после `migrate`.
- Участник `prompt_developer` видит все промпты в `/ai/admin/ai/prompt/`.
- Редактировать он может только те промпты, где назначен в поле `editors`.
- `topic` и `prompt_name` для `prompt_developer` только для чтения; редактируется только `prompt_text`.
- Назначение разработчиков на конкретный промпт делает администратор/сотрудник.

Команды для сервера (через Docker):

```bash
docker compose --env-file .env exec -T web python manage.py shell -c "from ai.models import Prompt; print(*[f'{p.id}: {p.prompt_name} | editors={[u.username for u in p.editors.all()]}' for p in Prompt.objects.prefetch_related('editors').order_by('id')], sep='\n')"
```

```bash
docker compose --env-file .env exec -T web python manage.py shell -c "from django.contrib.auth.models import User; print(*[f'{u.id}: {u.username}' for u in User.objects.order_by('username')], sep='\n')"
```

## Локальный запуск (без Docker)

Нужны: `Python 3.10+`, `PostgreSQL 14+`, `psql`.

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
- `HTTP_PROXY`, `HTTPS_PROXY`, `PROXY` пустые (или удалены), если прокси не нужен

3. Запустите контейнеры:

```bash
docker compose --env-file .env up -d --build
docker compose --env-file .env exec -T web python manage.py migrate
docker compose --env-file .env exec -T web python manage.py collectstatic --noinput
```

Если нужна подробная сборка без кэша:

```bash
chmod +x build_no_cache.sh
./build_no_cache.sh
```

По умолчанию nginx доступен на `http://localhost:8080/ai/...`.

## Run (production)

See [DEPLOY.md](DEPLOY.md).

## Daily model availability checks

- Model health checks are executed once per day for the 04:00 MSK window.
- The scheduler starts automatically in web server processes (Daphne/Gunicorn/Uvicorn or Django runserver child process).
- To disable the built-in scheduler, set `AI_DISABLE_HEALTH_SCHEDULER=1`.

Manual run:

```bash
python manage.py check_models_health
python manage.py check_models_health --force
```
