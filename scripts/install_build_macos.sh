#!/usr/bin/env bash
# Install dependencies and build a macOS .app with py2app.
# Run from any directory: bash scripts/install_build_macos.sh

set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This script is intended for macOS (Darwin). Got: $(uname -s)" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

if [[ ! -f pyproject.toml ]] || [[ ! -f requirements.txt ]]; then
  echo "pyproject.toml and requirements.txt must exist in $ROOT_DIR" >&2
  exit 1
fi

if command -v python3 >/dev/null 2>&1; then
  PYTHON=python3
elif command -v python >/dev/null 2>&1; then
  PYTHON=python
else
  echo "Python not found. Install Python 3.11+ and ensure python3 or python is on PATH." >&2
  exit 1
fi

"$PYTHON" -c "import sys; v=sys.version_info; assert v >= (3, 11), f'Need Python >= 3.11, got {v.major}.{v.minor}'"

VENV_DIR="$ROOT_DIR/.venv"
if [[ ! -d "$VENV_DIR" ]]; then
  "$PYTHON" -m venv "$VENV_DIR"
fi
# shellcheck source=/dev/null
source "$VENV_DIR/bin/activate"

python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -e .
python -m pip install "py2app>=0.28"

export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-offscreen}"
python -c "from zaliver.ui.main_window import MainWindow; print('import ok')"

( cd "$ROOT_DIR/macos_app" && python setup.py py2app )

echo "Build finished. Output: $ROOT_DIR/macos_app/dist/Zaliver.app"
