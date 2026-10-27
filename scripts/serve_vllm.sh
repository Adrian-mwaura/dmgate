#!/usr/bin/env bash
# Serve the gate's model with vLLM on the GPU machine (see docs/DEPLOY.md).
# Listens on 127.0.0.1 only: reach it through an SSH tunnel, never expose it publicly.
#
#   MODEL=Qwen/Qwen3-8B PORT=8000 bash scripts/serve_vllm.sh
set -euo pipefail

MODEL="${MODEL:?set MODEL to a Hugging Face or ModelScope model id}"
PORT="${PORT:-8000}"
MAX_LEN="${MAX_LEN:-8192}"          # the gate's prompts are short; a smaller context leaves room for batching
GPU_UTIL="${GPU_UTIL:-0.90}"        # share of GPU memory vLLM may take
export HF_HOME="${HF_HOME:-/root/autodl-tmp/hf}"   # keep weights on the data disk

echo "vLLM $(vllm --version 2>/dev/null || echo '?') serving ${MODEL} on 127.0.0.1:${PORT}"
exec vllm serve "${MODEL}" \
  --host 127.0.0.1 \
  --port "${PORT}" \
  --max-model-len "${MAX_LEN}" \
  --gpu-memory-utilization "${GPU_UTIL}" \
  --enable-prefix-caching
