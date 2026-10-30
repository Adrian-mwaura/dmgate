#!/usr/bin/env bash
# Serve the gate's model with vLLM on the GPU machine (see docs/DEPLOY.md).
# Listens on 127.0.0.1 only: reach it through an SSH tunnel, never expose it publicly.
#
#   MODEL=Qwen/Qwen3.5-4B EXTRA_ARGS="--language-model-only" bash serve_vllm.sh
#   MODEL=Qwen/Qwen3.5-9B QUANT=fp8 EXTRA_ARGS="--language-model-only" bash serve_vllm.sh
set -euo pipefail

MODEL="${MODEL:?set MODEL to a Hugging Face or ModelScope model id}"
PORT="${PORT:-8000}"
MAX_LEN="${MAX_LEN:-16384}"     # the gate's prompts are a few thousand tokens; a short context leaves room to batch
GPU_UTIL="${GPU_UTIL:-0.90}"    # share of the 24 GB vLLM may take (weights + attention cache)
QUANT="${QUANT:-}"              # fp8: quantize bf16 weights at load time (Ada GPUs such as the 4090 run FP8 natively)
EXTRA_ARGS="${EXTRA_ARGS:-}"    # e.g. --language-model-only for Qwen3.5 (skips its vision encoder)
export HF_HOME="${HF_HOME:-/root/autodl-tmp/hf}"            # keep weights on the data disk
export MODELSCOPE_CACHE="${MODELSCOPE_CACHE:-/root/autodl-tmp/modelscope}"

args=(--host 127.0.0.1 --port "${PORT}" --max-model-len "${MAX_LEN}" --gpu-memory-utilization "${GPU_UTIL}")
if [[ -n "${QUANT}" ]]; then
  args+=(--quantization "${QUANT}")
fi

echo "vLLM $(vllm --version 2>/dev/null || echo '?') serving ${MODEL} on 127.0.0.1:${PORT}"
# shellcheck disable=SC2086  # EXTRA_ARGS is a list of flags on purpose
exec vllm serve "${MODEL}" "${args[@]}" ${EXTRA_ARGS}
