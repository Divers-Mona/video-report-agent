#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
TOOL_ROOT="$PROJECT_ROOT/.tooling/wsl"
VENV="$PROJECT_ROOT/.venv-wsl"
UV="$TOOL_ROOT/bin/uv"
NODE="$TOOL_ROOT/node/bin/node"
NPM="$TOOL_ROOT/node/bin/npm"

export UV_PROJECT_ENVIRONMENT="$VENV"
export UV_PYTHON_INSTALL_DIR="$TOOL_ROOT/python"
export UV_CACHE_DIR="$TOOL_ROOT/cache/uv"
export PLAYWRIGHT_BROWSERS_PATH="$TOOL_ROOT/cache/playwright"
export DENO_NO_UPDATE_CHECK=1
export PATH="$VENV/bin:$TOOL_ROOT/node/bin:$TOOL_ROOT/pi/node_modules/.bin:$TOOL_ROOT/bin:$PATH"

cd "$PROJECT_ROOT"

if [[ ! -x "$UV" || ! -x "$NODE" || ! -x "$NPM" ]]; then
  printf 'Project-local uv and Node.js are required. See the tool installation steps in docs/local-wsl.md.\n' >&2
  exit 2
fi
if ! command -v ffmpeg >/dev/null || ! command -v ffprobe >/dev/null; then
  printf 'ffmpeg and ffprobe are required on the WSL PATH. See docs/local-wsl.md.\n' >&2
  exit 2
fi

mkdir -p "$TOOL_ROOT/cache/npm" "$TOOL_ROOT/cache/playwright"
EXTRAS=(--extra local-asr)
if [[ "${VIDEO_REPORT_WITH_ENHANCEMENT:-0}" == "1" ]]; then
  EXTRAS+=(--extra enhancement)
fi
"$UV" sync --project "$PROJECT_ROOT" --locked --inexact "${EXTRAS[@]}"
bash "$PROJECT_ROOT/scripts/install-deno.sh"
npm install --prefix "$TOOL_ROOT/pi" --no-save --no-package-lock --no-audit --no-fund \
  --cache "$TOOL_ROOT/cache/npm" '@earendil-works/pi-coding-agent@0.85.0'
"$UV" run --project "$PROJECT_ROOT" --locked "${EXTRAS[@]}" playwright install chromium
"$VENV/bin/python" "$PROJECT_ROOT/scripts/cache_faster_whisper_model.py"

printf 'WSL local environment is ready. Run video-report.cmd from Windows or scripts/run-wsl.sh from WSL.\n'
