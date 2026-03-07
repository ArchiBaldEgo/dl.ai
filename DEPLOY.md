# Простой запуск проекта на сервере

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
- `NO_PROXY`
- `PROXY`

Если AI-токены у вас есть, тоже заполните их.

## 3. Запустить проект

```bash
bash server-up.sh
```

Скрипт сам:

- соберёт контейнеры
- запустит их
- выполнит миграции
- выполнит `collectstatic`

## 4. Проверить, что всё работает

```bash
sudo docker compose ps
sudo docker logs -f dl_ai_web
```

## 5. Если вы обновили код из git

Просто снова выполните:

```bash
git pull
bash server-up.sh
```
