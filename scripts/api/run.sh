#!/usr/bin/env bash
# Ensure ffmpeg/ffprobe (Ubuntu/Debian) and start Zaliver API.
# Run from any directory: bash scripts/api/run.sh
#
# Env (optional): ZALIVER_API_HOST, ZALIVER_API_PORT, ZALIVER_API_TOKEN, …

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$ROOT_DIR"

ensure_ffmpeg() {
  if command -v ffmpeg >/dev/null 2>&1 && command -v ffprobe >/dev/null 2>&1; then
    echo "ffmpeg/ffprobe OK:"
    echo "  $(command -v ffmpeg)"
    echo "  $(command -v ffprobe)"
    return 0
  fi

  if ! command -v apt-get >/dev/null 2>&1; then
    echo "Нужны ffmpeg и ffprobe в PATH. apt-get не найден — установите вручную." >&2
    exit 1
  fi

  echo "Установка ffmpeg (включает ffprobe)…"
  if [[ "$(id -u)" -eq 0 ]]; then
    apt-get update -y
    apt-get install -y ffmpeg
  else
    sudo apt-get update -y
    sudo apt-get install -y ffmpeg
  fi

  if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
    echo "После установки ffmpeg/ffprobe всё ещё не в PATH." >&2
    exit 1
  fi
  echo "ffmpeg/ffprobe установлены."
}

if [[ -d "$ROOT_DIR/.venv" ]]; then
  # shellcheck source=/dev/null
  source "$ROOT_DIR/.venv/bin/activate"
fi

if command -v python3 >/dev/null 2>&1; then
  PYTHON=python3
elif command -v python >/dev/null 2>&1; then
  PYTHON=python
else
  echo "Python не найден." >&2
  exit 1
fi

ensure_ffmpeg

export PYTHONPATH="${ROOT_DIR}/src${PYTHONPATH:+:$PYTHONPATH}"

echo "Starting Zaliver API from $ROOT_DIR …"
exec "$PYTHON" -m zaliver.api
