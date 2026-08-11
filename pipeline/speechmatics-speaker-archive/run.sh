#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
  echo "Ошибка: установите ffmpeg и ffprobe." >&2
  echo "Ubuntu/Debian: sudo apt install ffmpeg" >&2
  exit 1
fi

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi

. .venv/bin/activate
python -m pip install -r requirements.txt
exec uvicorn app.main:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}"
