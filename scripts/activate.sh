#!/usr/bin/env bash
# source ./scripts/activate.sh
set -euo pipefail

# Определяем, где лежит сам скрипт, даже при source
# SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# cd "$PROJECT_ROOT"     # переходим в корень проекта
# pwd
alias app="./scripts/app/main.sh"
