#!/usr/bin/env bash
# Serve one model with vLLM on the ZJU GPU server, by hand (see docs/DEPLOY.md; measure.sh does this
# for every rung). Listens on 127.0.0.1 only: reach it from Windows through an SSH tunnel. Other
# users of the server share that address, so the server takes the first free card, a port of
# your own and, with API_KEY, a key every request must carry.
#
#   MODEL=$MODELS/Qwen3.5-4B NAME=Qwen/Qwen3.5-4B EXTRA_ARGS="--language-model-only" bash serve_vllm.sh
#   MODEL=$MODELS/Qwen3.5-9B NAME=Qwen/Qwen3.5-9B QUANT=fp8 EXTRA_ARGS="--language-model-only" bash serve_vllm.sh
set -euo pipefail

# shellcheck source=server_env.sh
source "$(dirname "${BASH_SOURCE[0]}")/server_env.sh"
MODEL="${MODEL:?set MODEL to a weights folder (or a model id to download)}"
NAME="${NAME:-$MODEL}"          # the model id clients ask for; the answer cache is keyed by it
MAX_LEN="${MAX_LEN:-16384}"     # the gate's prompts are a few thousand tokens; a short context leaves room to batch
GPU_UTIL="${GPU_UTIL:-0.90}"    # share of each card's 24 GB vLLM may take (weights + attention cache)
QUANT="${QUANT:-}"              # fp8: 8-bit weights; the 3090 (Ampere) has no FP8 units, so vLLM computes in 16 bits
TP="${TP:-1}"                   # cards per model (tensor parallelism)
API_KEY="${API_KEY:-}"          # set it here and as DMGATE_LLM_API_KEY on the client
EXTRA_ARGS="${EXTRA_ARGS:-}"    # e.g. --language-model-only for Qwen3.5 (skips its vision encoder)
VLLM="$ENV/bin/vllm"

if [[ -z "${CUDA_VISIBLE_DEVICES:-}" ]]; then
  CUDA_VISIBLE_DEVICES="$(pick_gpus "$TP")" || { echo "fewer than $TP free card(s): wait, or set CUDA_VISIBLE_DEVICES"; exit 1; }
  export CUDA_VISIBLE_DEVICES
fi
if port_in_use "$PORT"; then
  echo "port $PORT is taken: choose another with PORT=..."; exit 1
fi

args=(--served-model-name "${NAME}" --host 127.0.0.1 --port "${PORT}" --max-model-len "${MAX_LEN}"
      --gpu-memory-utilization "${GPU_UTIL}")
[[ -n "${QUANT}" ]] && args+=(--quantization "${QUANT}")
((TP > 1)) && args+=(--tensor-parallel-size "${TP}")
[[ -n "${API_KEY}" ]] && args+=(--api-key "${API_KEY}")

echo "vLLM $("$VLLM" --version 2>/dev/null || echo '?') serving ${NAME} on 127.0.0.1:${PORT}, cards ${CUDA_VISIBLE_DEVICES}"
# shellcheck disable=SC2086  # EXTRA_ARGS is a list of flags on purpose
exec "$VLLM" serve "${MODEL}" "${args[@]}" ${EXTRA_ARGS}
