"""Синхронизация таблицы UpdateLog с коммитами из git.

Использование:
    python manage.py sync_update_log          # импортировать все новые коммиты
    python manage.py sync_update_log --rebuild  # пересоздать таблицу с нуля

Читает git log из текущей ветки (main), преобразует английские коммит-сообщения
в русские описания и сохраняет в БД.
"""

import subprocess
import sys
from datetime import datetime

from django.core.management.base import BaseCommand
from ai.models import UpdateLog


def _git_log():
    """Возвращает список коммитов: (hash, date, author, message).

    Date конвертируется в Московский часовой пояс (Europe/Moscow).
    """
    import os
    env = dict(os.environ, TZ="Europe/Moscow")
    # В Docker-контейнере репозиторий примонтирован с хоста (volume `.: /app`)
    # и принадлежит другому uid — git блокирует его как «dubious ownership»
    # (exit 128). safe.directory=* отключает эту проверку для текущего вызова,
    # не трогая глобальный git-конфиг контейнера (который бы слетел при
    # пересоздании контейнера). subprocess.run вызывает git списком без shell,
    # поэтому '*' передаётся буквально (без glob-раскрытия).
    try:
        result = subprocess.run(
            ["git", "-c", "safe.directory=*", "log",
             "--pretty=format:%h|%ad|%an|%s", "--date=format:%Y-%m-%d"],
            capture_output=True, text=True, check=True, env=env,
        )
    except subprocess.CalledProcessError as exc:
        # Покажем реальный stderr git'а, а не глухой traceback с одним retcode.
        sys.stderr.write(exc.stderr or "")
        raise
    commits = []
    for line in result.stdout.strip().splitlines():
        parts = line.split("|", 3)
        if len(parts) == 4:
            commits.append(tuple(parts))
    return commits


# Маппинг английских префиксов коммитов на русские описания
_PREFIX_MAP = {
    "feat:": "Нововведение",
    "fix:": "Исправление",
    "chore:": "Обслуживание",
    "docs:": "Документация",
    "test:": "Тесты",
    "refactor:": "Рефакторинг",
    "style:": "Стиль",
    "perf:": "Производительность",
    "ci:": "CI/CD",
    "build:": "Сборка",
    "ux:": "UX улучшение",
}


def _translate_commit_message(msg):
    """Преобразует английское commit-сообщение в русское описание."""
    lower = msg.lower()
    for prefix, russian in _PREFIX_MAP.items():
        if lower.startswith(prefix):
            rest = msg[len(prefix):].strip()
            return f"{russian}: {rest}"
    # Если нет стандартного префикса — возвращаем как есть
    return msg


class Command(BaseCommand):
    help = "Синхронизация таблицы UpdateLog с git-коммитами"

    def add_arguments(self, parser):
        parser.add_argument(
            "--rebuild",
            action="store_true",
            help="Удалить все записи и пересоздать из git",
        )

    def handle(self, *args, **options):
        if options["rebuild"]:
            deleted, _ = UpdateLog.objects.all().delete()
            self.stdout.write(f"Удалено {deleted} старых записей")

        commits = _git_log()
        existing_hashes = set(
            UpdateLog.objects.exclude(commit_hash="").values_list("commit_hash", flat=True)
        )

        created = 0
        # Коммиты идут от новых к старым — сохраняем в том же порядке
        for commit_hash, date_str, author, message in commits:
            if commit_hash in existing_hashes:
                continue

            try:
                commit_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            except ValueError:
                continue

            description = _translate_commit_message(message)

            UpdateLog.objects.create(
                commit_date=commit_date,
                description=description,
                author=author,
                commit_hash=commit_hash,
            )
            created += 1

        self.stdout.write(
            self.style.SUCCESS(f"Синхронизировано {created} новых коммитов (всего в git: {len(commits)})")
        )