# Sourced by the server scripts (server_setup.sh, measure.sh, serve_vllm.sh): where everything
# lives on the ZJU GPU server, and helpers for sharing the machine politely (docs/DEPLOY.md).
#
# DATA holds the repository (dmgate/), the environment (env/), the weights (models/), the caches
# (cache/) and temporary files (tmp/). It defaults to the folder that holds the repository,
# /home/part1/<user> on the big disk.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA="${DATA:-$(dirname "$ROOT")}"
ENV="${ENV:-$DATA/env}"
MODELS="${MODELS:-$DATA/models}"
[[ -w "$DATA" ]] || { echo "cannot write to $DATA: set DATA to a folder of yours on a big disk" >&2; exit 1; }

# Every cache and temporary file goes under DATA, always. pip, Hugging Face, ModelScope, vLLM,
# Triton and torch default to ~/.cache and /tmp, which on a shared server can sit on a system disk
# that is nearly full (the server's is, 99%), and filling it breaks the machine for everyone.
export XDG_CACHE_HOME="$DATA/cache"
export PIP_CACHE_DIR="$XDG_CACHE_HOME/pip"
export HF_HOME="$XDG_CACHE_HOME/huggingface"
export MODELSCOPE_CACHE="$XDG_CACHE_HOME/modelscope"
export VLLM_CACHE_ROOT="$XDG_CACHE_HOME/vllm"
export TRITON_CACHE_DIR="$XDG_CACHE_HOME/triton"
export TORCHINDUCTOR_CACHE_DIR="$XDG_CACHE_HOME/torchinductor"
export TMPDIR="$DATA/tmp"
mkdir -p "$XDG_CACHE_HOME" "$TMPDIR"

export CUDA_DEVICE_ORDER=PCI_BUS_ID                                     # card numbers mean what nvidia-smi shows
export VLLM_NO_USAGE_STATS=1 DO_NOT_TRACK=1 HF_HUB_DISABLE_TELEMETRY=1  # no usage reports leave the machine

# FlashInfer has no prebuilt token-sampling kernel for the 3090 (sm_86) and compiles one on first use
# with NVIDIA's compiler, nvcc, which the server lacks; vLLM's own sampler (PyTorch) needs none.
export VLLM_USE_FLASHINFER_SAMPLER=0

# A port of our own: on a shared machine every user's 127.0.0.1 is the same, so 8000 may be taken.
PORT="${PORT:-$((18000 + $(id -u) % 1000))}"

# Cards that look free: under 1 GiB of memory in use and under 10% busy, in nvidia-smi's numbering.
idle_gpus() {
  nvidia-smi --query-gpu=index,memory.used,utilization.gpu --format=csv,noheader,nounits 2>/dev/null |
    awk -F', *' '$2 + 0 < 1024 && $3 + 0 < 10 { print $1 }'
}

# pick_gpus N: the first N free cards as a list such as 2,3 (for CUDA_VISIBLE_DEVICES); fails when
# fewer than N are free.
pick_gpus() {
  local cards
  cards="$(idle_gpus | head -n "$1" | paste -sd, -)"
  [[ -n "$cards" && "$(tr ',' '\n' <<<"$cards" | wc -l)" -eq "$1" ]] || return 1
  echo "$cards"
}

# port_in_use PORT: true when something on this machine already listens there.
port_in_use() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }

# A fresh key for one server's lifetime: other users of the machine cannot use our model server.
new_api_key() { od -An -tx1 -N16 /dev/urandom | tr -d ' \n'; }
