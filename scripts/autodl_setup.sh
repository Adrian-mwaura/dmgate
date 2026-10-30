#!/usr/bin/env bash
# Prepare the AutoDL instance in no-GPU mode (docs/DEPLOY.md, "Prepare by day, run by night").
# Everything slow happens here, without a GPU: the Python environment, vLLM, this project, OPA, the
# model weights, the test suite and a dry run of the overnight script. Safe to rerun: each step
# skips what is already done.
#
#   bash /root/autodl-tmp/dmgate/scripts/autodl_setup.sh
#   GET="qwen35-4b" bash .../autodl_setup.sh          # only some weights
#   SOURCE=hf bash .../autodl_setup.sh                # Hugging Face (through hf-mirror.com) instead of ModelScope
set -euo pipefail

DATA="${DATA:-/root/autodl-tmp}"
ROOT="${ROOT:-$DATA/dmgate}"
ENV="${ENV:-$DATA/env}"
MODELS="${MODELS:-$DATA/models}"
GET="${GET:-qwen35-4b qwen35-9b}"
SOURCE="${SOURCE:-modelscope}"
VLLM_VERSION="0.31.0"
OPA_VERSION="1.21.2"

say() { printf '\n== %s\n' "$*"; }

if [[ -f /etc/network_turbo ]]; then
  say "AutoDL academic network acceleration"
  # shellcheck disable=SC1091
  source /etc/network_turbo || true
fi

say "1/7 project: $ROOT"
if [[ -f "$DATA/dmgate.bundle" ]]; then
  if [[ ! -d "$ROOT/.git" ]]; then
    git clone "$DATA/dmgate.bundle" "$ROOT"
  else
    git -C "$ROOT" pull --ff-only "$DATA/dmgate.bundle" main
  fi
fi
[[ -d "$ROOT/.git" ]] || { echo "no project at $ROOT: copy dmgate.bundle first (scripts/autodl.ps1 -Push)"; exit 1; }
git -C "$ROOT" log --oneline -1

say "2/7 Python 3.11 or newer"
pick_python() {
  for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
      command -v "$candidate"; return 0
    fi
  done
  if command -v conda >/dev/null; then
    [[ -x "$DATA/py312/bin/python" ]] || conda create -y -q -p "$DATA/py312" python=3.12 >&2
    echo "$DATA/py312/bin/python"; return 0
  fi
  return 1
}
PY="$(pick_python)" || { echo "no Python >= 3.11 and no conda: choose an AutoDL image with Python 3.11+"; exit 1; }
"$PY" --version

say "3/7 environment $ENV: vLLM $VLLM_VERSION and the project"
[[ -x "$ENV/bin/python" ]] || "$PY" -m venv "$ENV"
"$ENV/bin/python" -m pip install -q --upgrade pip
"$ENV/bin/python" -m pip install -q "vllm==$VLLM_VERSION" modelscope "huggingface_hub[cli]"
"$ENV/bin/python" -m pip install -q -e "$ROOT[dev]"
"$ENV/bin/vllm" --version

say "4/7 OPA $OPA_VERSION for Linux, checked against its published SHA-256"
OPA="$ROOT/tools/opa/opa"
if [[ ! -x "$OPA" ]]; then
  mkdir -p "$ROOT/tools/opa"
  base="https://github.com/open-policy-agent/opa/releases/download/v$OPA_VERSION/opa_linux_amd64_static"
  curl -fsSL -o "$OPA.download" "$base"
  want="$(curl -fsSL "$base.sha256" | awk '{print $1}')"
  got="$(sha256sum "$OPA.download" | awk '{print $1}')"
  [[ -n "$want" && "$want" == "$got" ]] || { echo "OPA checksum mismatch: want $want got $got"; rm -f "$OPA.download"; exit 1; }
  mv "$OPA.download" "$OPA" && chmod +x "$OPA"
fi
"$OPA" version | head -1

say "5/7 model weights in $MODELS ($SOURCE)"
weights() {  # rung -> "model id|directory"
  case "$1" in
    qwen35-4b) echo "Qwen/Qwen3.5-4B|Qwen3.5-4B" ;;
    qwen35-9b) echo "Qwen/Qwen3.5-9B|Qwen3.5-9B" ;;
    openpangu-7b) echo "openpangu/openPangu-Embedded-7B-V1.1|openPangu-Embedded-7B-V1.1" ;;
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

say "7/7 dry run of the overnight script (a stub server stands in for vLLM)"
DRY_RUN=1 SAMPLE=150 LADDER=qwen35-4b PYTHON="$ENV/bin/python" bash "$ROOT/scripts/overnight.sh"

say "ready: when a GPU is free, start the instance with it and run  bash $ROOT/scripts/overnight.sh"
