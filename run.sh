#!/usr/bin/env bash
# Локальный запуск. Создаёт venv при первом старте, ставит зависимости, поднимает приложение.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "[run] создаю venv…"
  python3 -m venv .venv
  ./.venv/bin/pip install -q --upgrade pip
  ./.venv/bin/pip install -q -r requirements.txt
fi

export PORT="${PORT:-5060}"
# ANTHROPIC_API_KEY должен быть в окружении для оценки рынка.
echo "[run] http://127.0.0.1:$PORT"
exec ./.venv/bin/python app.py
