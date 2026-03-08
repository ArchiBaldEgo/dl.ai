#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

ENV_FILE="${ENV_FILE:-.env}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Файл $ENV_FILE не найден. Скопируйте .env.example в $ENV_FILE и заполните значения."
  exit 1
fi

compose() {
  if docker compose version >/dev/null 2>&1; then
    docker compose --env-file "$ENV_FILE" "$@"
    return
  fi

  if command -v docker-compose >/dev/null 2>&1; then
    echo "WARNING: используется legacy docker-compose (v1). Лучше установить Docker Compose v2." >&2
    docker-compose "$@"
    return
  fi

  echo "Docker Compose не найден. Установите docker compose (v2 plugin) или docker-compose." >&2
  exit 1
}

COMPOSE_FILES=("-f" "docker-compose.yml")
if [[ "${COMPOSE_PROD:-0}" == "1" ]]; then
  COMPOSE_FILES+=("-f" "docker-compose.prod.yml")
fi

compose "${COMPOSE_FILES[@]}" up -d --build

for attempt in {1..10}; do
  if compose "${COMPOSE_FILES[@]}" exec -T web python manage.py migrate; then
    break
  fi

  if [[ "$attempt" == "10" ]]; then
    echo "Не удалось выполнить миграции после 10 попыток."
    exit 1
  fi

  echo "База данных ещё не готова. Ждём 5 секунд и пробуем снова..."
  sleep 5
done

compose "${COMPOSE_FILES[@]}" exec -T web python manage.py collectstatic --noinput
compose "${COMPOSE_FILES[@]}" ps

echo "Готово. Проект запущен."
