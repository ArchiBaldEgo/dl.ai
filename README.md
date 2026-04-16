# dl.ai

Django + Channels (ASGI, Daphne). UI and API live under `/ai/...`.

## Run (local)

```bash
cp .env.example .env
docker compose --env-file .env up -d --build
```

Default mapping exposes nginx on `http://localhost:8080/ai/...`.

## Run (production, no sudo)

See [DEPLOY.md](DEPLOY.md). In short:

```bash
cp .env.example .env
COMPOSE_PROD=1 ENV_FILE=.env bash server-up.sh
```

Prod override binds nginx to `127.0.0.1:8081` by default so it can be reverse-proxied from `https://dl.gsu.by/ai/`.

## Daily model availability checks

- Model health checks are executed once per day for the 04:00 MSK window.
- The scheduler starts automatically in web server processes (Daphne/Gunicorn/Uvicorn or Django runserver child process).
- To disable the built-in scheduler, set `AI_DISABLE_HEALTH_SCHEDULER=1`.

Manual run:

```bash
python manage.py check_models_health
python manage.py check_models_health --force
```
