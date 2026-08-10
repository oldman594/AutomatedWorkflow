#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"

if ! "$PYTHON" -c 'import PyInstaller' >/dev/null 2>&1; then
  echo "PyInstaller is required: $PYTHON -m pip install 'pyinstaller>=6,<7'" >&2
  exit 1
fi
if ! command -v npm >/dev/null 2>&1; then
  echo "Node.js 22 and npm are required" >&2
  exit 1
fi

"$PYTHON" -m PyInstaller --noconfirm --clean \
  --workpath "$ROOT/desktop/build" \
  --distpath "$ROOT/desktop/dist" \
  "$ROOT/desktop/runner.spec"

mkdir -p "$ROOT/desktop/src-tauri/resources/bin"
if [[ "${OS:-}" == "Windows_NT" ]]; then
  cp "$ROOT/desktop/dist/autoflow-runner.exe" \
    "$ROOT/desktop/src-tauri/resources/bin/autoflow-runner.exe"
else
  install -m 0755 "$ROOT/desktop/dist/autoflow-runner" \
    "$ROOT/desktop/src-tauri/resources/bin/autoflow-runner"
fi

cd "$ROOT/desktop"
npm ci
npm run build
