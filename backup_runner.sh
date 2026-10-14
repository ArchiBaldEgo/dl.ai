#!/bin/bash
#
# backup_runner.sh — Фоновый демон: каждый понедельник 04:00 UTC делает бэкап БД и отправляет в Telegram.
#
# Логика:
#   1. Ждёт до следующего понедельника 04:00 UTC
#   2. Делает pg_dump через docker exec → .sql.gz в backups/
#   3. Если файлов больше 3 — удаляет самые старые
#   4. Отправляет новый файл в Telegram через Bot API
#   5. Повторяет
#
# Запуск (рекомендуется): systemd user-юнит dlai-backup.service:
#   systemctl --user enable --now dlai-backup.service   (логи: journalctl --user -u dlai-backup)
# Разовый бэкап без ожидания понедельника:
#   /home/archi/dlai/backup_runner.sh --now   (разово поднять ротацию: MAX_BACKUPS=99 ... --now)
#
# Секреты Telegram — в /home/archi/dlai/backup.env (chmod 600), в скрипте их нет.
#

set -euo pipefail

PROJECT_DIR="/home/archi/dlai/"
BACKUP_DIR="$PROJECT_DIR/backups"
DB_CONTAINER="dl_ai_db"
DB_NAME="dl_ai"
DB_USER="vlad"
# Сколько бэкапов держать в ротации; можно переопределить разово окружением:
#   MAX_BACKUPS=99 /home/archi/dlai/backup_runner.sh --now
MAX_BACKUPS="${MAX_BACKUPS:-3}"

# Telegram: секретов в скрипте больше нет. Юнит берёт их из /home/archi/dlai/backup.env
# (EnvironmentFile); при ручном запуске не из окружения — читаем тот же файл,
# затем /etc/environment (на случай переезда туда через sudo).
ENV_FILE="${PROJECT_DIR}backup.env"
TG_BOT_TOKEN="${TG_BOT_TOKEN:-$(sed -n 's/^TG_BOT_TOKEN=//p' "$ENV_FILE" 2>/dev/null | head -n1 | tr -d '"')}"
TG_BOT_TOKEN="${TG_BOT_TOKEN:-$(sed -n 's/^TG_BOT_TOKEN=//p' /etc/environment 2>/dev/null | head -n1 | tr -d '"')}"
TG_CHAT_ID="${TG_CHAT_ID:-$(sed -n 's/^TG_CHAT_ID=//p' "$ENV_FILE" 2>/dev/null | head -n1 | tr -d '"')}"
TG_CHAT_ID="${TG_CHAT_ID:-$(sed -n 's/^TG_CHAT_ID=//p' /etc/environment 2>/dev/null | head -n1 | tr -d '"')}"
[ -n "$TG_BOT_TOKEN" ] && [ -n "$TG_CHAT_ID" ] || { echo "FATAL: TG_BOT_TOKEN/TG_CHAT_ID не найдены (см. $ENV_FILE)" >&2; exit 1; }

LOG_FILE="$BACKUP_DIR/backup.log"
mkdir -p "$BACKUP_DIR"

log() {
    echo "[$(date -u '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG_FILE"
}

# tg_api <endpoint> [curl-args...] — POST к Telegram Bot API.
# В лог пишем короткий итог; полный ответ — только при ошибке,
# чтобы backup.log не зарастал raw-JSON каждого успешного вызова.
tg_api() {
    local endpoint="$1"; shift
    local resp rc
    resp=$(curl -sS -X POST "https://api.telegram.org/bot${TG_BOT_TOKEN}/${endpoint}" "$@" 2>&1) && rc=0 || rc=$?
    case "$resp" in
        *'"ok":true'*)
            log "Telegram ${endpoint}: OK"
            return 0
            ;;
        *)
            log "Telegram ${endpoint}: FAILED (curl rc=${rc}) — ${resp}"
            return 1
            ;;
    esac
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

    # Целостность: pg_dump мог умереть посередине — обрезанный архив не шлём.
    if ! gzip -t "$BACKUP_FILE" 2>>"$LOG_FILE"; then
        log "ERROR: gzip -t failed — дамп обрезан, файл удалён: $BACKUP_FILE"
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

    local TG_FAILED=0
    local REMAINING=$(ls -1 "$BACKUP_DIR"/dl_ai_db_*.sql.gz 2>/dev/null | wc -l)

    # Текстовое сообщение
    tg_api sendMessage \
        -d "chat_id=${TG_CHAT_ID}" \
        -d "text=📦 Бэкап БД dl_ai

🗓 $(date -u '+%Y-%m-%d %H:%M:%S') UTC
📁 Файл: $(basename "$BACKUP_FILE")
📊 Размер: ${SIZE}
✅ Бэкапов в ротации: ${REMAINING}" \
        -d "parse_mode=HTML" || TG_FAILED=1

    # Файл
    tg_api sendDocument \
        -F "chat_id=${TG_CHAT_ID}" \
        -F "document=@${BACKUP_FILE}" \
        -F "caption=dl_ai_db_${TIMESTAMP}.sql.gz" || TG_FAILED=1

    if [ "$TG_FAILED" = 1 ]; then
        log "ERROR: Telegram delivery failed — файл остался локально: $BACKUP_FILE"
        return 1
    fi

    log "Done. Backup sent to Telegram."
}

# === Главный цикл — ждём до каждого понедельника 04:00 UTC ===
log "backup_runner started — waiting for Mon 04:00 UTC"

# Разовый запуск бэкапа (для теста/ручного восстановления): после do_backup — выход.
if [ "${1:-}" = "--now" ]; then
    do_backup && log "--- one-off backup finished (--now) ---"
    exit $?
fi

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

    # Страховка: при дрифте/скачке часов sleep_seconds мог стать ≤0 — тогда
    # sleep упал бы (set -e убил бы демона). Ждём минуту и пересчитываем.
    if [ "$sleep_seconds" -le 0 ]; then
        sleep_seconds=60
    fi
    sleep "$sleep_seconds"

    # Делаем бэкап
    do_backup || log "Backup failed — will retry next week"

    # Спим 5 минут чтобы не зацепить тот же понедельник дважды
    sleep 300
done
