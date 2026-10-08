"""Синхронизация таблицы UpdateLog с коммитами из git.

Использование:
    python manage.py sync_update_log          # импортировать все новые коммиты
    python manage.py sync_update_log --rebuild  # пересоздать таблицу с нуля
    python manage.py sync_update_log --interactive  # консольный отбор коммитов

Читает git log из текущей ветки (main) без merge-коммитов (--no-merges),
преобразует английские коммит-сообщения в русские описания и сохраняет в БД.
Авторы пишутся сырыми никами (%an); ФИО/группа отображаются на странице
«Обновления» через справочник AuthorAlias.

В режиме --interactive (хуки post-merge/pre-push) новые коммиты выводятся
списком и запрашивается выбор: добавить все, перечислить номера или ничего.
НЕвыбранные коммиты пишутся строками с hidden=True (не видны на «Обновлениях»,
но дедуп по хэшу работает и их можно опубликовать позже через UpdateLogAdmin).
Без TTY интерактивный режим ничего не импортирует — выбор сохраняется до
следующего запуска в терминале.
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
            ["git", "-c", "safe.directory=*", "log", "--no-merges",
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


def _parse_selection(answer, total):
    """Разбирает ответ пользователя на выбор добавляемых коммитов.

    "" → пустое множество (ничего); "a"/"A"/"все" → все номера;
    "1,3-5" → {1, 3, 4, 5}. Любой мусор/вне диапазона → None (повторный запрос).
    """
    answer = (answer or "").strip().lower()
    if not answer:
        return set()
    if answer in ("a", "все"):
        return set(range(1, total + 1))
    selected = set()
    for token in answer.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            bounds = token.split("-", 1)
            try:
                start, end = int(bounds[0]), int(bounds[1])
            except ValueError:
                return None
            if start < 1 or end > total or start > end:
                return None
            selected.update(range(start, end + 1))
            continue
        try:
            number = int(token)
        except ValueError:
            return None
        if number < 1 or number > total:
            return None
        selected.add(number)
    return selected


def _new_commits(commits):
    """Фильтрует список git-коммитов до ещё не сохранённых."""
    existing_hashes = set(
        UpdateLog.objects.exclude(commit_hash="").values_list("commit_hash", flat=True)
    )
    return [c for c in commits if c[0] not in existing_hashes]


def _create_rows(new_commits, selected_numbers):
    """Пишет коммиты: выбранные — видимо, остальные — скрыто.

    selected_numbers — множество номеров коммитов, которые делают видимыми;
    None значит «ничего не выбрано» (все — hidden=True). Не выбранные коммиты
    сохраняются скрытыми (дедуп по хэшу; публикация — через UpdateLogAdmin).
    """
    if selected_numbers is None:
        selected_numbers = set()
    added = hidden = 0
    # Коммиты идут от новых к старым — сохраняем в том же порядке
    for index, (commit_hash, date_str, author, message) in enumerate(new_commits, 1):
        try:
            commit_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            # Дату не разобрали — коммит пропускаем целиком (как раньше).
            continue

        if index in selected_numbers:
            is_hidden = False
            added += 1
        else:
            is_hidden = True
            hidden += 1

        description = _translate_commit_message(message)
        UpdateLog.objects.create(
            commit_date=commit_date,
            description=description,
            author=author,
            commit_hash=commit_hash,
            hidden=is_hidden,
        )
    return added, hidden


class Command(BaseCommand):
    help = "Синхронизация таблицы UpdateLog с git-коммитами"

    def add_arguments(self, parser):
        parser.add_argument(
            "--rebuild",
            action="store_true",
            help="Удалить все записи и пересоздать из git",
        )
        parser.add_argument(
            "--interactive",
            action="store_true",
            help="Выбрать новые коммиты в консоли; невыбранные сохраняются скрытыми",
        )

    def handle(self, *args, **options):
        if options["rebuild"]:
            deleted, _ = UpdateLog.objects.all().delete()
            self.stdout.write(f"Удалено {deleted} старых записей")

        commits = _git_log()
        new_commits = _new_commits(commits)

        if not new_commits:
            self.stdout.write("Новых коммитов нет (всего в git: %d)" % len(commits))
            return

        if options.get("interactive"):
            self._handle_interactive(new_commits, len(commits))
            return

        # Авто-режим (CMD контейнера): импортируем всё.
        added, _hidden = _create_rows(new_commits, set(range(1, len(new_commits) + 1)))
        self.stdout.write(
            self.style.SUCCESS(f"Синхронизировано {added} новых коммитов (всего в git: {len(commits)})")
        )

    def _handle_interactive(self, new_commits, total_commits):
        """Консольный отбор: показываем новые коммиты, спрашиваем что добавить."""
        # Без терминала выбор невозможен — и ничего не импортируем: коммиты
        # останутся «новыми» и будут предложены при следующем интерактивном запуске.
        if not sys.stdin.isatty():
            self.stdout.write(
                f"Нет TTY — импорт отложен: {len(new_commits)} коммитов будут "
                "предложены при следующем интерактивном запуске"
            )
            return

        self.stdout.write(f"Новых коммитов: {len(new_commits)} (всего в git: {total_commits})")
        for index, (commit_hash, date_str, author, message) in enumerate(new_commits, 1):
            self.stdout.write(f" {index:>2}) {date_str}  {author}: {message}")

        total = len(new_commits)
        selected = None
        while selected is None:
            answer = input(
                "Добавить в «Обновления»? [a = все / номера через запятую (1,3-5) / "
                "Enter = ничего]: "
            )
            selected = _parse_selection(answer, total)
            if selected is None:
                self.stdout.write("Не разобрал ответ — повторите, например: 1,3-5")

        added, hidden = _create_rows(new_commits, selected)
        self.stdout.write(
            self.style.SUCCESS(
                f"Добавлено {added}, скрыто {hidden} (опубликовать/скрыть записи — UpdateLogAdmin)"
            )
        )