#!/bin/bash
#
# health_check.sh — Мониторинг dl.ai проекта с авторекавери.
#
# Проверяет /health каждые 60 секунд. Если 3 раза подряд неудача —
# перезапускает контейнеры и отправляет уведомление в Telegram.
#
# Запуск: nohup /home/vlad/v0.9/health_check.sh &>/dev/null &

set -euo pipefail

PROJECT_DIR="/home/vlad/v0.9"
HEALTH_URL="http://localhost:8000/health"
FAIL_THRESHOLD=3
POLL_INTERVAL=60

TG_BOT_TOKEN="834440…Myec"
TG_CHAT_ID="690979160"
LOG_FILE="$PROJECT_DIR/backups/health_check.log"

mkdir -p "$(dirname "$LOG_FILE")"

log() {
    echo "[$(date -u '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG_FILE"
}

send_tg() {
    curl -s -X POST "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" \
        -d "chat_id=${TG_CHAT_ID}" \
        -d "text=$1" >> "$LOG_FILE" 2>&1
}

do_restart() {
    log "RESTART: Starting emergency restart..."
    send_tg "🚨 dl.ai не отвечает на health-check. Перезапускаю проект..."

    cd "$PROJECT_DIR"
    docker compose down >> "$LOG_FILE" 2>&1
    docker compose up -d --build >> "$LOG_FILE" 2>&1

    # Ждём поднятия
    sleep 30

    # Миграции и статика
    docker compose exec -T web python manage.py migrate >> "$LOG_FILE" 2>&1 || true
    docker compose exec -T web python manage.py collectstatic --noinput >> "$LOG_FILE" 2>&1 || true

    # Проверяем здоровье
    sleep 15
    if curl -sf "$HEALTH_URL" >> "$LOG_FILE" 2>&1; then
        log "RESTART: Success — project is healthy"
        send_tg "✅ dl.ai перезапущен и работает нормально."
    else
        log "RESTART: FAILED — still unhealthy after restart"
        send_tg "❌ dl.ai НЕ поднялся после перезапуска. Нужен ручной заход."
    fi
}

log "health_check started — monitoring $HEALTH_URL every ${POLL_INTERVAL}s"

fail_count=0

while true; do
    if curl -sf --max-time 10 "$HEALTH_URL" >> "$LOG_FILE" 2>&1; then
        if [ "$fail_count" -gt 0 ]; then
            log "Recovered after $fail_count failures"
        fi
        fail_count=0
    else
        fail_count=$((fail_count + 1))
        log "Health check FAILED ($fail_count/$FAIL_THRESHOLD)"

        if [ "$fail_count" -ge "$FAIL_THRESHOLD" ]; then
            log "Threshold reached — triggering restart"
            do_restart
            fail_count=0
        fi
    fi

    sleep "$POLL_INTERVAL"
done