#!/bin/bash
# Установка git hooks для проекта dl.ai.
# Копирует post-merge (sync UpdateLog после git pull) и pre-push
# (sync UpdateLog перед git push) в .git/hooks/.
#
# Использование: bash scripts/setup_hooks.sh

set -e

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOOKS_DIR="$REPO_ROOT/.git/hooks"

if [ ! -d "$HOOKS_DIR" ]; then
    echo "Ошибка: $HOOKS_DIR не найден — не git-репозиторий?"
    exit 1
fi

for hook in post-merge pre-push; do
    SRC="$REPO_ROOT/scripts/hooks/$hook"
    DST="$HOOKS_DIR/$hook"
    if [ -f "$SRC" ]; then
        cp "$SRC" "$DST"
        chmod +x "$DST"
        echo "✅ Установлен $hook → $DST"
    else
        echo "⚠️ Пропущен $hook — исходный файл не найден: $SRC"
    fi
done

echo "Готово. Hooks активны."