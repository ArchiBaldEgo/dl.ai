#!/usr/bin/env python3
"""
mail_bridge.py — Мост: email → команды → Telegram + OpenClaw.

Читает непрочитанные письма с IMAP-ящика, ищет команды в теме или теле,
выполняет их и/или пересылает в Telegram.

Поддерживаемые команды (в теме письма):
  restart         — перезапуск проекта
  backup          — сделать бэкап БД
  health          — проверить состояние
  status          — статус контейнеров
  <произвольный текст> — переслать в Telegram как сообщение мне

Настройка в .env или в конфиге ниже.
Запуск: nohup python3 /home/vlad/v0.9/mail_bridge.py &>/dev/null &
"""

import imaplib
import email
import email.header
import os
import re
import subprocess
import sys
import time
import json
import urllib.request
import urllib.parse
from pathlib import Path

# === Конфиг (можно переопределить через .env) ===
PROJECT_DIR = Path("/home/vlad/v0.9")
ENV_FILE = PROJECT_DIR / ".env"

# IMAP
IMAP_HOST = os.getenv("MAIL_BRIDGE_IMAP_HOST", "")
IMAP_PORT = int(os.getenv("MAIL_BRIDGE_IMAP_PORT", "993"))
IMAP_USER = os.getenv("MAIL_BRIDGE_IMAP_USER", "")
IMAP_PASS = os.getenv("MAIL_BRIDGE_IMAP_PASS", "")
ALLOWED_SENDERS = [s.strip().lower() for s in os.getenv("MAIL_BRIDGE_ALLOWED_SENDERS", "").split(",") if s.strip()]

# Telegram
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "834440…Myec")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "690979160")

POLL_INTERVAL = int(os.getenv("MAIL_BRIDGE_POLL", "60"))  # секунд
LOG_FILE = PROJECT_DIR / "backups" / "mail_bridge.log"

# Загружаем .env
if ENV_FILE.exists():
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            # Не перезаписываем уже заданные env-переменные
            if k not in os.environ:
                os.environ[k] = v
    # Перечитаем после .env
    IMAP_HOST = os.getenv("MAIL_BRIDGE_IMAP_HOST", IMAP_HOST)
    IMAP_USER = os.getenv("MAIL_BRIDGE_IMAP_USER", IMAP_USER)
    IMAP_PASS = os.getenv("MAIL_BRIDGE_IMAP_PASS", IMAP_PASS)
    ALLOWED_SENDERS = [s.strip().lower() for s in os.getenv("MAIL_BRIDGE_ALLOWED_SENDERS", "").split(",") if s.strip()]
    TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", TG_BOT_TOKEN)
    TG_CHAT_ID = os.getenv("TG_CHAT_ID", TG_CHAT_ID)


def log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    line = f"[{ts}] {msg}"
    print(line)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def send_tg(text: str):
    """Отправка сообщения в Telegram."""
    try:
        url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": TG_CHAT_ID, "text": text}).encode()
        req = urllib.request.Request(url, data=data)
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log(f"TG send failed: {e}")


def decode_header(value):
    """Декодирование заголовка email."""
    if not value:
        return ""
    parts = email.header.decode_header(value)
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(str(part))
    return "".join(decoded)


def get_sender_addr(msg) -> str:
    """Извлечение email-адреса отправителя."""
    raw = decode_header(msg.get("From", ""))
    m = re.search(r"<([^>]+)>", raw)
    if m:
        return m.group(1).strip().lower()
    return raw.strip().lower()


def get_subject(msg) -> str:
    return decode_header(msg.get("Subject", "")).strip()


def get_body(msg) -> str:
    """Извлечение текстового тела письма."""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain":
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace").strip()
        return ""
    else:
        payload = msg.get_payload(decode=True)
        charset = msg.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace").strip()


def run_command(cmd: str) -> str:
    """Выполнение команды на сервере, возврат результата."""
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(PROJECT_DIR),
        )
        output = result.stdout.strip()
        if result.stderr.strip():
            output += "\n" + result.stderr.strip()
        return output or "(no output)"
    except subprocess.TimeoutExpired:
        return "Command timed out (300s)"
    except Exception as e:
        return f"Error: {e}"


def process_command(subject: str, body: str, sender: str):
    """Обработка команды из письма."""
    subject_lower = subject.lower().strip()
    log(f"Processing command: '{subject_lower}' from {sender}")

    if subject_lower == "restart":
        send_tg(f"📧 Получена команда RESTART от {sender}\n🔄 Перезапускаю проект...")
        output = run_command("docker compose down && docker compose up -d --build && sleep 30 && docker compose exec -T web python manage.py migrate && docker compose exec -T web python manage.py collectstatic --noinput")
        send_tg(f"✅ Перезапуск выполнен:\n```\n{output[:2000]}\n```")
        return

    if subject_lower == "backup":
        send_tg(f"📧 Получена команда BACKUP от {sender}\n📦 Делаю бэкап...")
        output = run_command("bash /home/vlad/v0.9/backup_runner.sh --now 2>&1 || true")
        send_tg(f"✅ Бэкап выполнен:\n```\n{output[:2000]}\n```")
        return

    if subject_lower == "health":
        output = run_command("curl -sf http://localhost:8000/health 2>&1 || echo 'HEALTH FAILED'")
        send_tg(f"📧 Health check от {sender}:\n```\n{output}\n```")
        return

    if subject_lower == "status":
        output = run_command('docker ps --format "table {{.Names}}\\t{{.Status}}\\t{{.Ports}}" 2>&1')
        send_tg(f"📧 Статус контейнеров:\n```\n{output}\n```")
        return

    # Произвольный текст — пересылаем в Telegram
    text = f"📧 Письмо от {sender}\nТема: {subject}\n\n{body[:3000]}"
    send_tg(text)


def check_mail():
    """Проверка почты через IMAP — чтение непрочитанных писем."""
    if not IMAP_HOST or not IMAP_USER or not IMAP_PASS:
        log("IMAP not configured — skipping mail check")
        return

    try:
        mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        mail.login(IMAP_USER, IMAP_PASS)
        mail.select("INBOX")

        # Ищем непрочитанные
        status, data = mail.search(None, "UNSEEN")
        if status != "OK":
            return

        ids = data[0].split()
        if not ids:
            return

        log(f"Found {len(ids)} unread email(s)")

        for eid in ids:
            status, msg_data = mail.fetch(eid, "(RFC822)")
            if status != "OK":
                continue

            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)

            sender = get_sender_addr(msg)
            subject = get_subject(msg)
            body = get_body(msg)

            log(f"Email from: {sender}, subject: {subject}")

            # Фильтр по отправителю
            if ALLOWED_SENDERS and sender not in ALLOWED_SENDERS:
                log(f"Sender {sender} not in allowed list — skipping")
                # Помечаем как прочитанное чтобы не висело
                mail.store(eid, "+FLAGS", "\\Seen")
                continue

            # Обработка команды
            try:
                process_command(subject, body, sender)
            except Exception as e:
                log(f"Command processing error: {e}")
                send_tg(f"📧 Ошибка обработки письма от {sender}: {e}")

            # Помечаем как прочитанное
            mail.store(eid, "+FLAGS", "\\Seen")

        mail.logout()
    except Exception as e:
        log(f"IMAP error: {e}")


def main():
    log(f"mail_bridge started — polling {IMAP_HOST} every {POLL_INTERVAL}s")
    if not IMAP_HOST:
        log("WARNING: MAIL_BRIDGE_IMAP_HOST not set — configure in .env")
        log("Required env vars: MAIL_BRIDGE_IMAP_HOST, MAIL_BRIDGE_IMAP_USER, MAIL_BRIDGE_IMAP_PASS")
        log("Optional: MAIL_BRIDGE_ALLOWED_SENDERS (comma-separated emails)")
        log("Optional: MAIL_BRIDGE_POLL (seconds, default 60)")

    while True:
        try:
            check_mail()
        except Exception as e:
            log(f"Unexpected error: {e}")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()