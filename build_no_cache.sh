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

echo "==> Сборка web без кэша"
docker compose --env-file .env build --no-cache --pull --progress=plain web

echo "==> Перезапуск контейнеров"
docker compose --env-file .env up -d --force-recreate --remove-orphans

echo "==> Статус контейнеров"
docker compose --env-file .env ps

echo "==> Последние логи"
docker compose --env-file .env logs --no-color --tail=100 web db nginx
