#!/bin/bash

PROJECT_DIR="/home/vlad/v0.9"

while true; do
    # Вычисляем время до следующего 3:55 UTC
    current_epoch=$(date +%s)
    target_epoch=$(date -d "today 03:55" +%s 2>/dev/null || date -d "03:55" +%s)

    # Если сегодняшнее 3:55 уже прошло, берём завтрашнее
    if [ $current_epoch -ge $target_epoch ]; then
        target_epoch=$(date -d "tomorrow 03:55" +%s)
    fi

    sleep_seconds=$((target_epoch - current_epoch))

    echo "$(date): Ждём $((sleep_seconds / 3600)) часов и $(((sleep_seconds % 3600) / 60)) минут до следующего запуска в 3:55."

    sleep $sleep_seconds

    # Теперь настало 3:55 – выполняем перезапуск
    echo "$(date): Запускаю перезапуск контейнеров..."
    cd "$PROJECT_DIR" && docker compose down && docker compose up -d --build
    # Ждём пока web поднимется
    sleep 30
    # Накатываем миграции и статику
    cd "$PROJECT_DIR" && docker compose exec -T web python manage.py migrate
    cd "$PROJECT_DIR" && docker compose exec -T web python manage.py collectstatic --noinput
    echo "$(date): Перезапуск завершён."
done