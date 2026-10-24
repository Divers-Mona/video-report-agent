#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
TOOL_ROOT="$PROJECT_ROOT/.tooling/wsl"
VENV="$PROJECT_ROOT/.venv-wsl"

export UV_PROJECT_ENVIRONMENT="$VENV"
export UV_PYTHON_INSTALL_DIR="$TOOL_ROOT/python"
export UV_CACHE_DIR="$TOOL_ROOT/cache/uv"
export PLAYWRIGHT_BROWSERS_PATH="$TOOL_ROOT/cache/playwright"
export PATH="$VENV/bin:$TOOL_ROOT/node/bin:$TOOL_ROOT/pi/node_modules/.bin:$TOOL_ROOT/bin:$PATH"

cd "$PROJECT_ROOT"

if [[ ! -x "$VENV/bin/video-report" ]]; then
  printf 'WSL environment is not installed. Follow docs/local-wsl.md and run scripts/setup-wsl.sh.\n' >&2
  exit 2
fi

exec "$VENV/bin/video-report" "$@"
