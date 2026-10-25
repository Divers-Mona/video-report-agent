#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
TOOL_ROOT="$PROJECT_ROOT/.tooling/wsl"
DENO="$TOOL_ROOT/bin/deno"
PYTHON="$PROJECT_ROOT/.venv-wsl/bin/python"
DENO_VERSION="2.9.8"
export DENO_NO_UPDATE_CHECK=1

if [[ -x "$DENO" ]] && installed_version="$("$DENO" --version)" && \
   [[ "${installed_version%%$'\n'*}" == "deno $DENO_VERSION "* ]]; then
  printf 'Deno %s is already installed at %s.\n' "$DENO_VERSION" "$DENO"
  exit 0
fi

case "$(uname -m)" in
  x86_64) target="x86_64-unknown-linux-gnu" ;;
  aarch64) target="aarch64-unknown-linux-gnu" ;;
  *) printf 'Unsupported Deno architecture: %s\n' "$(uname -m)" >&2; exit 2 ;;
esac
for tool in curl sha256sum; do
  if ! command -v "$tool" >/dev/null; then
    printf '%s is required to install Deno.\n' "$tool" >&2
    exit 2
  fi
done
if [[ ! -x "$PYTHON" ]]; then
  printf 'Project WSL Python is required; run scripts/setup-wsl.sh.\n' >&2
  exit 2
fi

mkdir -p "$TOOL_ROOT/bin"
temporary="$(mktemp -d "$TOOL_ROOT/.deno-install.XXXXXX")"
trap 'rm -rf -- "$temporary"' EXIT
archive="deno-$target.zip"
release="https://github.com/denoland/deno/releases/download/v$DENO_VERSION"
curl --fail --location --silent --show-error "$release/$archive" -o "$temporary/$archive"
curl --fail --location --silent --show-error "$release/$archive.sha256sum" \
  -o "$temporary/$archive.sha256sum"
read -r checksum filename < "$temporary/$archive.sha256sum"
if [[ ! "$checksum" =~ ^[[:xdigit:]]{64}$ || "$filename" != "$archive" ]]; then
  printf 'Invalid Deno release checksum entry.\n' >&2
  exit 2
fi
(
  cd "$temporary"
  printf '%s  %s\n' "$checksum" "$archive" | sha256sum --check --strict
)
"$PYTHON" - "$temporary/$archive" "$temporary/deno" <<'PY'
import shutil
import sys
import zipfile

with zipfile.ZipFile(sys.argv[1]) as archive:
    with archive.open("deno") as source, open(sys.argv[2], "wb") as destination:
        shutil.copyfileobj(source, destination)
PY
chmod 755 "$temporary/deno"
installed_version="$("$temporary/deno" --version)"
if [[ "${installed_version%%$'\n'*}" != "deno $DENO_VERSION "* ]]; then
  printf 'Downloaded Deno has an unexpected version.\n' >&2
  exit 2
fi
mv -- "$temporary/deno" "$DENO"
printf 'Installed Deno %s at %s (official release SHA-256 verified).\n' "$DENO_VERSION" "$DENO"
