#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

if [[ ! -f .env ]]; then
  echo "Ошибка: не найден файл .env"
  echo "Сначала создайте его: cp .env.example .env"
  exit 1
fi

export DOCKER_BUILDKIT=1

if [[ "${1:-}" == "--no-cache" ]]; then
  echo "==> Сборка web без кэша"
  docker compose --env-file .env build --no-cache --pull --progress=plain web
else
  echo "==> Сборка web с кэшем"
  docker compose --env-file .env build --pull --progress=plain web
fi

echo "==> Перезапуск web"
docker compose --env-file .env up -d --no-deps --force-recreate web

echo "==> Статус контейнеров"
docker compose --env-file .env ps
