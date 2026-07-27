#!/bin/bash
#
# backup_runner.sh — Фоновый демон:每周一 04:00 UTC делает бэкап БД и отправляет в Telegram.
#
# Логика:
#   1. Ждёт до следующего понедельника 04:00 UTC
#   2. Делает pg_dump через docker exec → .sql.gz в backups/
#   3. Если файлов больше 3 — удаляет самые старые
#   4. Отправляет новый файл в Telegram через Bot API
#   5. Повторяет
#
# Запуск: nohup /home/vlad/v0.9/backup_runner.sh &>/dev/null &
# или через restart_docker.sh (добавить строку запуска)
#

set -euo pipefail

PROJECT_DIR="/home/vlad/v0.9"
BACKUP_DIR="$PROJECT_DIR/backups"
DB_CONTAINER="dl_ai_db"
DB_NAME="dl_ai"
DB_USER="vlad"
MAX_BACKUPS=3

# Telegram
TG_BOT_TOKEN="8344403193:AAEsizOuLe4uh6RRUFrbIVRyIGs14drMyec"
TG_CHAT_ID="690979160"

LOG_FILE="$BACKUP_DIR/backup.log"
mkdir -p "$BACKUP_DIR"

log() {
    echo "[$(date -u '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG_FILE"
}

do_backup() {
    local TIMESTAMP=$(date -u +"%Y%m%d_%H%M%S")
    local BACKUP_FILE="$BACKUP_DIR/dl_ai_db_${TIMESTAMP}.sql.gz"

    # === 1. Дамп БД ===
    log "Starting DB dump → $BACKUP_FILE"
    docker exec "$DB_CONTAINER" pg_dump -U "$DB_USER" -d "$DB_NAME" --no-owner --no-acl 2>>"$LOG_FILE" | gzip > "$BACKUP_FILE"

    if [ ! -s "$BACKUP_FILE" ]; then
        log "ERROR: Backup file is empty — pg_dump failed"
        rm -f "$BACKUP_FILE"
        return 1
    fi

    local SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
    log "Backup created: $BACKUP_FILE ($SIZE)"

    # === 2. Ротация — оставляем только MAX_BACKUPS файлов ===
    local BACKUP_COUNT=$(ls -1 "$BACKUP_DIR"/dl_ai_db_*.sql.gz 2>/dev/null | wc -l)
    if [ "$BACKUP_COUNT" -gt "$MAX_BACKUPS" ]; then
        local DELETE_COUNT=$((BACKUP_COUNT - MAX_BACKUPS))
        log "Rotation: $BACKUP_COUNT backups found, deleting $DELETE_COUNT oldest"
        ls -1t "$BACKUP_DIR"/dl_ai_db_*.sql.gz | tail -n "$DELETE_COUNT" | while read -r old_file; do
            log "Deleting: $old_file"
            rm -f "$old_file"
        done
    fi

    # === 3. Отправка в Telegram ===
    log "Sending backup to Telegram chat $TG_CHAT_ID"

    # Текстовое сообщение
    local REMAINING=$(ls -1 "$BACKUP_DIR"/dl_ai_db_*.sql.gz 2>/dev/null | wc -l)
    curl -s -X POST "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" \
        -d "chat_id=${TG_CHAT_ID}" \
        -d "text=📦 Бэкап БД dl_ai

🗓 $(date -u '+%Y-%m-%d %H:%M:%S') UTC
📁 Файл: $(basename "$BACKUP_FILE")
📊 Размер: ${SIZE}
✅ Бэкапов в ротации: ${REMAINING}" \
        -d "parse_mode=HTML" >>"$LOG_FILE" 2>&1

    # Файл
    curl -s -X POST "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendDocument" \
        -F "chat_id=${TG_CHAT_ID}" \
        -F "document=@${BACKUP_FILE}" \
        -F "caption=dl_ai_db_${TIMESTAMP}.sql.gz" >>"$LOG_FILE" 2>&1

    log "Done. Backup sent to Telegram."
}

# === Главный цикл — ждём до каждого понедельника 04:00 UTC ===
log "backup_runner started — waiting for Mon 04:00 UTC"

while true; do
    current_epoch=$(date +%s)

    # Вычисляем следующий понедельник 04:00 UTC
    current_day=$(date -u +%u)  # 1=Mon ... 7=Sun
    target_epoch=$(date -u -d "today 04:00" +%s 2>/dev/null)

    # Если сегодня не понедельник или уже после 04:00 — берём следующий понедельник
    if [ "$current_day" -ne 1 ] || [ "$current_epoch" -ge "$target_epoch" ]; then
        # Сколько дней до следующего понедельника
        days_until_mon=$(( (8 - current_day) % 7 ))
        if [ "$days_until_mon" -eq 0 ]; then
            days_until_mon=7
        fi
        target_epoch=$(date -u -d "+${days_until_mon} days 04:00" +%s 2>/dev/null)
    fi

    sleep_seconds=$((target_epoch - current_epoch))
    hours=$((sleep_seconds / 3600))
    mins=$(((sleep_seconds % 3600) / 60))
    log "Next backup: Mon 04:00 UTC — waiting ${hours}h ${mins}m"

    sleep "$sleep_seconds"

    # Делаем бэкап
    do_backup || log "Backup failed — will retry next week"

    # Спим 5 минут чтобы не зацепить тот же понедельник дважды
    sleep 300
done