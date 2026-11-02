#!/usr/bin/env bash
# Prepare the ZJU GPU server for the gate's measurements (docs/DEPLOY.md). Uses no GPU: the Python
# environment, vLLM, this project, OPA, the model weights, the test suite and a dry run of the
# measurement script. Safe to rerun: each step skips what is already done. Everything goes under
# DATA, the folder that holds the repository (server_env.sh).
#
#   bash /home/part1/<user>/dmgate/scripts/server_setup.sh
#   GET="qwen35-4b" bash .../server_setup.sh          # only some weights
#   SOURCE=hf bash .../server_setup.sh                # Hugging Face (through hf-mirror.com) instead of ModelScope
#   PIP_INDEX_URL=https://mirrors.zju.edu.cn/pypi/web/simple bash .../server_setup.sh   # a pip mirror
set -euo pipefail

# shellcheck source=server_env.sh
source "$(dirname "${BASH_SOURCE[0]}")/server_env.sh"
GET="${GET:-qwen35-4b qwen35-9b}"
SOURCE="${SOURCE:-modelscope}"
VLLM_VERSION="0.31.0"
OPA_VERSION="1.21.2"

say() { printf '\n== %s\n' "$*"; }

say "1/7 project: $ROOT (data in $DATA)"
df -h "$DATA" | tail -1
[[ -d "$ROOT/.git" ]] || { echo "$ROOT is not a git checkout: clone dmgate.bundle first (scripts/server.ps1 -Push)"; exit 1; }
if [[ -f "$DATA/dmgate.bundle" ]]; then
  before="$(git -C "$ROOT" rev-parse HEAD)"
  git -C "$ROOT" pull --ff-only "$DATA/dmgate.bundle" main
  # A newer bundle may change this script: start again with the new version.
  [[ "$(git -C "$ROOT" rev-parse HEAD)" == "$before" ]] || exec bash "$ROOT/scripts/server_setup.sh"
fi
git -C "$ROOT" log --oneline -1

say "2/7 Python 3.11 or newer"
pick_python() {  # the first python3 on PATH is an old 3.5; Ubuntu's own 3.12 is in /usr/bin
  for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
      command -v "$candidate"; return 0
    fi
  done
  return 1
}
PY="$(pick_python)" || { echo "no Python >= 3.11 on this machine: ask the administrator"; exit 1; }
"$PY" --version

say "3/7 environment $ENV: vLLM $VLLM_VERSION and the project"
[[ -x "$ENV/bin/python" ]] || "$PY" -m venv "$ENV"
"$ENV/bin/python" -m pip install -q --upgrade pip
"$ENV/bin/python" -m pip install -q "vllm==$VLLM_VERSION" modelscope huggingface_hub  # hub 1.x includes the hf command
"$ENV/bin/python" -m pip install -q -e "$ROOT[dev]"
"$ENV/bin/vllm" --version

say "4/7 OPA $OPA_VERSION for Linux, checked against its published SHA-256"
OPA="$ROOT/tools/opa/opa"
OPA_SHA256="830ab11f61ffec760b5e19db0d7ad02bf037e13f5ef4d665faeaf17632177527"  # GitHub's .sha256 for opa_linux_amd64_static
sha() { sha256sum "$1" 2>/dev/null | awk '{print $1}'; }
if [[ "$(sha "$OPA")" != "$OPA_SHA256" ]]; then
  mkdir -p "$ROOT/tools/opa"
  if [[ -f "$DATA/opa_linux_amd64_static" ]]; then  # sent from Windows by scripts/server.ps1 -Push -Opa
    cp "$DATA/opa_linux_amd64_static" "$OPA.download"
  else
    # Downloads from GitHub can break partway from here: each try resumes where the last one stopped.
    url="https://github.com/open-policy-agent/opa/releases/download/v$OPA_VERSION/opa_linux_amd64_static"
    for try in 1 2 3 4 5 6 7 8; do
      [[ "$(sha "$OPA.download")" == "$OPA_SHA256" ]] && break
      curl -fsSL -C - -o "$OPA.download" "$url" || { echo "download broke (try $try), resuming"; sleep 5; }
    done
  fi
  [[ "$(sha "$OPA.download")" == "$OPA_SHA256" ]] || {
    echo "OPA download failed or does not match its checksum. Delete $OPA.download, then rerun, or send"
    echo "the binary from Windows:  .\\scripts\\server.ps1 -Push -Opa"
    exit 1
  }
  mv "$OPA.download" "$OPA" && chmod +x "$OPA"
fi
"$OPA" version | head -1

say "5/7 model weights in $MODELS ($SOURCE)"
weights() {  # rung -> "model id|directory"
  case "$1" in
    qwen35-4b) echo "Qwen/Qwen3.5-4B|Qwen3.5-4B" ;;
    qwen35-9b) echo "Qwen/Qwen3.5-9B|Qwen3.5-9B" ;;
    openpangu-7b) echo "openpangu/openPangu-Embedded-7B-V1.1|openPangu-Embedded-7B-V1.1" ;;
    qwen38-27b-fp8) echo "Qwen/Qwen3.8-27B-FP8|Qwen3.8-27B-FP8" ;;  # gate rung 3: 31 GB, two cards
    qwen35-35b-a3b-fp8) echo "Qwen/Qwen3.5-35B-A3B-FP8|Qwen3.5-35B-A3B-FP8" ;;  # the M6 agent (Q7): four cards
    *) return 1 ;;
  esac
}
mkdir -p "$MODELS"
for rung in $GET; do
  spec="$(weights "$rung")" || { echo "unknown rung $rung"; continue; }
  IFS='|' read -r model dir <<<"$spec"
  if [[ -f "$MODELS/$dir/config.json" ]]; then
    echo "$model: already in $MODELS/$dir"; continue
  fi
  df -h "$DATA" | tail -1
  if [[ "$SOURCE" == "hf" ]]; then
    HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}" "$ENV/bin/hf" download "$model" --local-dir "$MODELS/$dir"
  else
    "$ENV/bin/modelscope" download --model "$model" --local_dir "$MODELS/$dir"
  fi
done
du -sh "$MODELS"/* 2>/dev/null || true

say "6/7 test suite (AmPermBench tests skip here: it is test-only data and stays on Windows)"
(cd "$ROOT" && "$ENV/bin/python" -m pytest -q)

say "7/7 dry run of the measurement script (a stub server stands in for vLLM)"
DRY_RUN=1 SAMPLE=150 LADDER=qwen35-4b PYTHON="$ENV/bin/python" bash "$ROOT/scripts/measure.sh"

say "ready: run  bash $ROOT/scripts/measure.sh  inside tmux (it waits for a free card)"
