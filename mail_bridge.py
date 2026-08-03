#!/usr/bin/env python3
"""
mail_bridge.py — Мост: email → команды → выполнение + ответ по почте + Telegram.

Читает непрочитанные письма с IMAP-ящика (приёмника), ищет команды
в теме письма, выполняет их, отправляет ответ отправителю (SMTP)
и дублирует результат в Telegram.

Ящик-приёмник настраивается в .env (MAIL_BRIDGE_IMAP_* / MAIL_BRIDGE_SMTP_*).
Отправитель (dolinsky@gsu.by) указывается в MAIL_BRIDGE_ALLOWED_SENDERS.

Команды (в теме письма):
  restart  — перезапуск проекта
  backup   — сделать бэкап БД
  health   — проверить состояние
  status   — статус контейнеров
  make     — тело письма пересылается в Telegram как задача/хотелка
  <любой текст> — переслать в Telegram

Запуск: nohup python3 /home/archi/v0.9/mail_bridge.py &>/dev/null &
"""

import imaplib
import smtplib
import email
import email.header
import email.mime.text
import email.utils
import os
import re
import subprocess
import time
import urllib.request
import urllib.parse
from pathlib import Path

# === Конфиг ===
PROJECT_DIR = Path("/home/archi/v0.9")
ENV_FILE = PROJECT_DIR / ".env"

# IMAP (чтение)
IMAP_HOST = os.getenv("MAIL_BRIDGE_IMAP_HOST", "")
IMAP_PORT = int(os.getenv("MAIL_BRIDGE_IMAP_PORT", "993"))
IMAP_USER = os.getenv("MAIL_BRIDGE_IMAP_USER", "")
IMAP_PASS = os.getenv("MAIL_BRIDGE_IMAP_PASS", "")

# SMTP (ответ)
SMTP_HOST = os.getenv("MAIL_BRIDGE_SMTP_HOST", "")
SMTP_PORT = int(os.getenv("MAIL_BRIDGE_SMTP_PORT", "587"))
SMTP_USER = os.getenv("MAIL_BRIDGE_SMTP_USER", "")
SMTP_PASS = os.getenv("MAIL_BRIDGE_SMTP_PASS", "")

ALLOWED_SENDERS = [s.strip().lower() for s in os.getenv("MAIL_BRIDGE_ALLOWED_SENDERS", "").split(",") if s.strip()]

# База данных (для команды backup) — берём из .env проекта, чтобы pg_dump
# использовал ту же роль, что и Django (DB_USER), а не захардкоженное имя.
DB_CONTAINER = "dl_ai_db"
DB_NAME = os.getenv("DB_NAME", "dl_ai")
DB_USER = os.getenv("DB_USER", "vlad")

# Telegram
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "834440…Myec")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "690979160")

POLL_INTERVAL = int(os.getenv("MAIL_BRIDGE_POLL", "60"))
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
            if k not in os.environ:
                os.environ[k] = v
    IMAP_HOST = os.getenv("MAIL_BRIDGE_IMAP_HOST", IMAP_HOST)
    IMAP_USER = os.getenv("MAIL_BRIDGE_IMAP_USER", IMAP_USER)
    IMAP_PASS = os.getenv("MAIL_BRIDGE_IMAP_PASS", IMAP_PASS)
    SMTP_HOST = os.getenv("MAIL_BRIDGE_SMTP_HOST", SMTP_HOST)
    SMTP_USER = os.getenv("MAIL_BRIDGE_SMTP_USER", SMTP_USER)
    SMTP_PASS = os.getenv("MAIL_BRIDGE_SMTP_PASS", SMTP_PASS)
    ALLOWED_SENDERS = [s.strip().lower() for s in os.getenv("MAIL_BRIDGE_ALLOWED_SENDERS", "").split(",") if s.strip()]
    DB_NAME = os.getenv("DB_NAME", DB_NAME)
    DB_USER = os.getenv("DB_USER", DB_USER)
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


def send_email_reply(to_addr: str, original_subject: str, body: str):
    """Отправка ответа на письмо через SMTP."""
    if not SMTP_HOST or not SMTP_USER:
        log("SMTP not configured — cannot send email reply")
        return
    try:
        msg = email.mime.text.MIMEText(body, "plain", "utf-8")
        msg["Subject"] = f"Re: {original_subject}"
        msg["From"] = SMTP_USER
        msg["To"] = to_addr
        msg["Date"] = email.utils.formatdate(localtime=False)
        msg["Message-ID"] = email.utils.make_msgid(domain=SMTP_USER.split("@")[-1] if "@" in SMTP_USER else "gsu.by")

        # 465 — неявный SSL (SMTP_SSL), 587/25 — STARTTLS после EHLO.
        if SMTP_PORT == 465:
            smtp_ctx = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30)
        else:
            smtp_ctx = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
        with smtp_ctx as srv:
            srv.ehlo()
            if SMTP_PORT != 465:
                srv.starttls()
                srv.ehlo()
            srv.login(SMTP_USER, SMTP_PASS)
            srv.sendmail(SMTP_USER, [to_addr], msg.as_string())

        log(f"Email reply sent to {to_addr}")
    except Exception as e:
        log(f"SMTP reply failed: {e}")


def decode_header(value):
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
    raw = decode_header(msg.get("From", ""))
    m = re.search(r"<([^>]+)>", raw)
    if m:
        return m.group(1).strip().lower()
    return raw.strip().lower()


def get_subject(msg) -> str:
    return decode_header(msg.get("Subject", "")).strip()


def get_body(msg) -> str:
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
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=300, cwd=str(PROJECT_DIR),
        )
        output = result.stdout.strip()
        if result.stderr.strip():
            output += "\n" + result.stderr.strip()
        return output or "(no output)"
    except subprocess.TimeoutExpired:
        return "Command timed out (300s)"
    except Exception as e:
        return f"Error: {e}"


def process_command(subject: str, body: str, sender: str) -> str:
    """Обработка команды из письма. Возвращает текст результата для ответа.
    Ответы написаны простым языком для обычных пользователей.
    """
    subject_lower = subject.lower().strip()
    log(f"Processing command: '{subject_lower}' from {sender}")

    if subject_lower == "restart":
        send_tg(f"📧 Получена команда RESTART от {sender}\n🔄 Перезапускаю проект...")
        run_command(
            "docker compose down && docker compose up -d --build && sleep 30 && "
            "docker compose exec -T web python manage.py migrate && "
            "docker compose exec -T web python manage.py collectstatic --noinput"
        )
        health = run_command("curl -sf http://localhost:8000/health 2>&1 || echo FAIL")
        if "ok" in health and "FAIL" not in health:
            result = "Проект перезапущен и работает нормально."
        else:
            result = "Проект перезапущен, но пока не отвечает. Возможно, нужно подождать пару минут или обратиться к разработчику."
        send_tg(f"✅ Перезапуск от {sender}: {result}")
        return result

    if subject_lower == "backup":
        send_tg(f"📧 Получена команда BACKUP от {sender}\n📦 Делаю бэкап...")
        ts = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
        bf = f"/home/archi/v0.9/backups/dl_ai_db_{ts}.sql.gz"
        out = run_command(f"docker exec {DB_CONTAINER} pg_dump -U {DB_USER} -d {DB_NAME} --no-owner --no-acl 2>/dev/null | gzip > {bf} && echo OK || echo FAIL")
        if "OK" in out:
            sz = run_command(f"du -h {bf} | cut -f1").strip()
            result = f"Бэкап создан успешно.\nФайл: {os.path.basename(bf)}\nРазмер: {sz}"
        else:
            result = "Не удалось создать бэкап. Возможно, база данных недоступна."
        send_tg(f"📦 Бэкап от {sender}: {result}")
        return result

    if subject_lower == "health":
        out = run_command("curl -sf http://localhost:8000/health 2>&1 || echo FAIL")
        if "ok" in out and "FAIL" not in out:
            result = "Проект работает. Всё в порядке."
        else:
            result = "Проект сейчас не отвечает. Возможно, требуется перезапуск — отправьте письмо с темой restart."
        send_tg(f"📧 Health check от {sender}: {result}")
        return result

    if subject_lower == "status":
        out = run_command('docker ps --format "{{.Names}}\t{{.Status}}" 2>&1')
        services = {
            "dl_ai_web": "Веб-сайт",
            "dl_ai_db": "База данных",
            "dl_ai_redis": "Кэш",
            "dl_ai_nginx": "Прокси",
        }
        parts = []
        all_ok = True
        for line in out.strip().splitlines():
            if "\t" not in line:
                continue
            name, status = line.split("\t", 1)
            label = services.get(name.strip(), name.strip())
            if "Up" in status and "healthy" in status.lower():
                parts.append(f"✅ {label} — работает")
            elif "Up" in status:
                parts.append(f"⚠️ {label} — запущен, но ещё проверяется")
                all_ok = False
            else:
                parts.append(f"❌ {label} — не работает")
                all_ok = False
        if all_ok and parts:
            result = "Всё работает:\n\n" + "\n".join(parts)
        elif parts:
            result = "Статус сервисов:\n\n" + "\n".join(parts) + "\n\nЧасть сервисов не работает. Можно отправить письмо с темой restart."
        else:
            result = "Не удалось получить статус сервисов. Возможно, система не запущена."
        send_tg(f"📧 Status от {sender}:\n{result}")
        return result

    if subject_lower == "make":
        task_text = body.strip() if body.strip() else "(пустое тело письма)"
        send_tg(f"📧 Задача от {sender}\n\n{task_text[:4000]}")
        return "Задача принята и передана разработчику. Спасибо!"

    # Произвольный текст — пересылаем в Telegram
    text = f"📧 Письмо от {sender}\nТема: {subject}\n\n{body[:3000]}"
    send_tg(text)
    return "Письмо переслано разработчику."


def check_mail():
    if not IMAP_HOST or not IMAP_USER or not IMAP_PASS:
        return

    try:
        mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        mail.login(IMAP_USER, IMAP_PASS)
        mail.select("INBOX")

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

            # Сохраняем Reply-To / Message-ID для ответа
            reply_to = msg.get("Reply-To") or msg.get("From") or sender
            reply_to_addr = get_sender_addr(msg) or sender
            original_msg_id = msg.get("Message-ID", "")

            # Фильтр по отправителю
            if ALLOWED_SENDERS and sender not in ALLOWED_SENDERS:
                log(f"Sender {sender} not in allowed list — skipping")
                mail.store(eid, "+FLAGS", "\\Seen")
                continue

            # Обработка команды
            try:
                result_text = process_command(subject, body, sender)
                # Ответ по почте
                send_email_reply(reply_to_addr, subject, result_text)
            except Exception as e:
                log(f"Command processing error: {e}")
                send_tg(f"📧 Ошибка обработки письма от {sender}: {e}")
                send_email_reply(reply_to_addr, subject, f"Ошибка: {e}")

            mail.store(eid, "+FLAGS", "\\Seen")

        mail.logout()
    except Exception as e:
        log(f"IMAP error: {e}")


def main():
    log(f"mail_bridge started — polling {IMAP_HOST} every {POLL_INTERVAL}s")
    if not IMAP_HOST:
        log("WARNING: MAIL_BRIDGE_IMAP_HOST not set — configure in .env")
        log("Required: MAIL_BRIDGE_IMAP_HOST, MAIL_BRIDGE_IMAP_USER, MAIL_BRIDGE_IMAP_PASS")
        log("Required: MAIL_BRIDGE_SMTP_HOST, MAIL_BRIDGE_SMTP_USER, MAIL_BRIDGE_SMTP_PASS")
        log("Optional: MAIL_BRIDGE_ALLOWED_SENDERS (comma-separated emails)")

    while True:
        try:
            check_mail()
        except Exception as e:
            log(f"Unexpected error: {e}")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()