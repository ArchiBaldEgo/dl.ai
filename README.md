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