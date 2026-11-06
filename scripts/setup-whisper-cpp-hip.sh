#!/usr/bin/env bash
set -Eeuo pipefail

# whisper.cpp is MIT licensed: https://github.com/ggml-org/whisper.cpp
PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
SOURCE="$PROJECT_ROOT/.tooling/wsl/whisper.cpp"
REVISION=d1be6fde11ac6e0407606b4e42fe72d34add8037 # v1.9.5
ROCM_ROOT="${ROCM_PATH:-/opt/rocm}"
GPU_TARGET="${AMDGPU_TARGETS:-gfx1100}"
MODEL="$PROJECT_ROOT/models/whisper-cpp/ggml-large-v3-turbo.bin"
MODEL_REVISION=5359861c739e955e79d9a303bcbc70fb988958b1

for tool in git cmake make curl; do
  command -v "$tool" >/dev/null || { printf '%s is required.\n' "$tool" >&2; exit 2; }
done
if [[ ! -x "$ROCM_ROOT/llvm/bin/clang++" ]]; then
  printf 'ROCm HIP compiler is missing; set ROCM_PATH to the installed ROCm root.\n' >&2
  exit 2
fi
if [[ ! -e "$SOURCE" ]]; then
  git clone --branch v1.9.5 --depth 1 https://github.com/ggml-org/whisper.cpp.git "$SOURCE"
fi
if [[ "$(git -C "$SOURCE" rev-parse HEAD)" != "$REVISION" ]] ||
   [[ -n "$(git -C "$SOURCE" status --porcelain --untracked-files=no)" ]]; then
  printf 'Existing whisper.cpp must be an unmodified v1.9.5 checkout; it was left untouched.\n' >&2
  exit 2
fi
cmake -S "$SOURCE" -B "$SOURCE/build" -DCMAKE_BUILD_TYPE=Release \
  -DGGML_HIP=ON -DAMDGPU_TARGETS="$GPU_TARGET" \
  -DCMAKE_HIP_COMPILER="$ROCM_ROOT/llvm/bin/clang++" \
  -DGGML_HIP_NO_VMM=ON -DWHISPER_BUILD_TESTS=OFF
cmake --build "$SOURCE/build" --target whisper-cli -j "${BUILD_JOBS:-4}"
mkdir -p "$(dirname -- "$MODEL")"
if [[ ! -s "$MODEL" ]]; then
  curl -fL --retry 2 --connect-timeout 20 \
    "https://huggingface.co/ggerganov/whisper.cpp/resolve/$MODEL_REVISION/ggml-large-v3-turbo.bin" \
    -o "$MODEL.partial"
  mv -- "$MODEL.partial" "$MODEL"
fi
printf 'HIP CLI and large-v3-turbo weights are ready. Run transcribe with --asr-backend whisper-cpp --asr-model large-v3-turbo.\n'
