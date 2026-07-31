#!/usr/bin/env bash
# Ensure ffmpeg/ffprobe + Python deps (venv) and start Zaliver API.
# Run from any directory: bash scripts/api/run.sh
#
# Env (optional): ZALIVER_API_HOST, ZALIVER_API_PORT, ZALIVER_API_TOKEN, …

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$ROOT_DIR"

apt_install() {
  if [[ "$(id -u)" -eq 0 ]]; then
    apt-get update -y
    apt-get install -y "$@"
  else
    sudo apt-get update -y
    sudo apt-get install -y "$@"
  fi
}

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
  apt_install ffmpeg

  if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
    echo "После установки ffmpeg/ffprobe всё ещё не в PATH." >&2
    exit 1
  fi
  echo "ffmpeg/ffprobe установлены."
}

ensure_python_deps() {
  if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 не найден." >&2
    exit 1
  fi

  local venv_dir="$ROOT_DIR/.venv"
  if [[ ! -x "$venv_dir/bin/python" ]]; then
    echo "Создание venv: $venv_dir"
    if ! python3 -m venv "$venv_dir" 2>/dev/null; then
      if command -v apt-get >/dev/null 2>&1; then
        echo "Установка python3-venv…"
        # python3-venv pulls the matching version package on Debian/Ubuntu.
        apt_install python3-venv python3-pip
        rm -rf "$venv_dir"
        python3 -m venv "$venv_dir"
      else
        echo "Не удалось создать venv (нужен python3-venv / ensurepip)." >&2
        exit 1
      fi
    fi
  fi

  # shellcheck source=/dev/null
  source "$venv_dir/bin/activate"
  PYTHON="$venv_dir/bin/python"

  echo "Установка Python-зависимостей (zaliver[api])…"
  "$PYTHON" -m pip install -U pip
  "$PYTHON" -m pip install -e '.[api]'
  echo "Python-зависимости установлены."
}

ensure_ffmpeg
ensure_python_deps

export PYTHONPATH="${ROOT_DIR}/src${PYTHONPATH:+:$PYTHONPATH}"

echo "Starting Zaliver API from $ROOT_DIR …"
exec "$PYTHON" -m zaliver.api
