#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

if [[ ! -f .env ]]; then
  echo "Файл .env не найден. Скопируйте .env.example в .env и заполните значения."
  exit 1
fi

set -a
source .env
set +a

sudo --preserve-env=HTTP_PROXY,HTTPS_PROXY,NO_PROXY,PROXY,http_proxy,https_proxy,no_proxy docker compose up -d --build

for attempt in {1..10}; do
  if sudo docker compose exec -T web python manage.py migrate; then
    break
  fi

  if [[ "$attempt" == "10" ]]; then
    echo "Не удалось выполнить миграции после 10 попыток."
    exit 1
  fi

  echo "База данных ещё не готова. Ждём 5 секунд и пробуем снова..."
  sleep 5
done

sudo docker compose exec -T web python manage.py collectstatic --noinput
sudo docker compose ps

echo "Готово. Проект запущен."
