#!/usr/bin/env bash
# The measurement run on the ZJU GPU server (docs/DEPLOY.md). For each rung of the model ladder:
# claim free cards, serve the model with vLLM, wait until ready, measure the LLM layer
# (dmgate-llm-eval: compiler, sensitivity) and the whole gate (rules-llm on the FS/DB dev probes),
# stop the server and give the cards back. One rung failing does not stop the next. Results, logs
# and the answer cache stay under the repository; a tarball collects them for copying home.
#
#   bash scripts/measure.sh                          # the default ladder
#   LADDER="qwen35-4b" bash scripts/measure.sh       # one rung
#   GPUS=3 bash scripts/measure.sh                   # this card, instead of the first free one
#   DRY_RUN=1 SAMPLE=150 bash scripts/measure.sh     # no GPU: a stub server stands in for vLLM
set -uo pipefail

# shellcheck source=server_env.sh
source "$(dirname "${BASH_SOURCE[0]}")/server_env.sh"
PYTHON="${PYTHON:-$ENV/bin/python}"
VLLM="${VLLM:-$(dirname "$PYTHON")/vllm}"
LADDER="${LADDER:-qwen35-4b qwen35-9b-fp8}"
SAMPLE="${SAMPLE:-}"              # about this many probes (every k-th); empty = all. Never for reported numbers.
DRY_RUN="${DRY_RUN:-0}"
GPUS="${GPUS:-auto}"              # auto = the first free cards; or a list such as 0,1
TP="${TP:-1}"                     # cards per model (tensor parallelism)
GPU_WAIT_MINUTES="${GPU_WAIT_MINUTES:-120}"
READY_MINUTES="${READY_MINUTES:-20}"
MAX_LEN="${MAX_LEN:-16384}"
GPU_UTIL="${GPU_UTIL:-0.90}"

rung() {  # name -> "served model id|weights directory|quantization|extra vLLM flags"
  case "$1" in
    qwen35-4b) echo "Qwen/Qwen3.5-4B|$MODELS/Qwen3.5-4B||--language-model-only" ;;
    qwen35-9b-fp8) echo "Qwen/Qwen3.5-9B|$MODELS/Qwen3.5-9B|fp8|--language-model-only" ;;
    openpangu-7b) echo "openpangu/openPangu-Embedded-7B-V1.1|$MODELS/openPangu-Embedded-7B-V1.1||--trust-remote-code" ;;
    *) return 1 ;;
  esac
}

stamp="$(date +%Y%m%d-%H%M)"
RUN="$stamp-measure$([[ "$DRY_RUN" == 1 ]] && echo -dry)"
OUT="$ROOT/results/$RUN"
mkdir -p "$OUT"
cd "$ROOT" || exit 1
log() { printf '%s  %s\n' "$(date +%H:%M:%S)" "$*" | tee -a "$OUT/run.log"; }

{
  echo "run: $RUN"
  echo "started: $(date -Iseconds)"
  echo "host: $(hostname)"
  echo "commit: $(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
  echo "gpus: $(nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader 2>/dev/null |
                sort | uniq -c | sed 's/^ *//' | paste -sd';' - || echo none)"
  echo "vllm: $([[ "$DRY_RUN" == 1 ]] && echo 'stub (dry run)' || "$VLLM" --version 2>/dev/null || echo unknown)"
  echo "ladder: $LADDER"
  echo "sample: ${SAMPLE:-all}"
  echo "tensor parallel: $TP"
} >"$OUT/manifest.txt"
cat "$OUT/manifest.txt"

# Claim cards for one rung: the given list, or the first free ones, waiting for others to finish.
claim_cards() {
  [[ "$GPUS" == auto ]] || { export CUDA_VISIBLE_DEVICES="$GPUS"; return 0; }
  local cards waited=0
  until cards="$(pick_gpus "$TP")"; do
    ((waited == 0)) && log "waiting for $TP free card(s), up to $GPU_WAIT_MINUTES min"
    ((waited >= GPU_WAIT_MINUTES)) && return 1
    sleep 60
    waited=$((waited + 1))
  done
  export CUDA_VISIBLE_DEVICES="$cards"
}

SERVER_PID=""
KEY=""
start_server() {  # model dir quant extra logfile
  local model="$1" dir="$2" quant="$3" extra="$4" logfile="$5"
  KEY="$(new_api_key)"
  if [[ "$DRY_RUN" == 1 ]]; then
    "$PYTHON" -m dmgate.llm.stub_server --port "$PORT" --model "$model" >"$logfile" 2>&1 &
  else
    local args=(serve "$dir" --served-model-name "$model" --host 127.0.0.1 --port "$PORT" --api-key "$KEY"
                --max-model-len "$MAX_LEN" --gpu-memory-utilization "$GPU_UTIL")
    [[ -n "$quant" ]] && args+=(--quantization "$quant")
    ((TP > 1)) && args+=(--tensor-parallel-size "$TP")
    # shellcheck disable=SC2086  # extra is a list of flags on purpose
    "$VLLM" "${args[@]}" $extra >"$logfile" 2>&1 &
  fi
  SERVER_PID=$!
}

wait_ready() {
  local tries=$((READY_MINUTES * 12))
  for ((i = 0; i < tries; i++)); do
    kill -0 "$SERVER_PID" 2>/dev/null || return 1
    curl -sf -H "Authorization: Bearer $KEY" "http://127.0.0.1:$PORT/v1/models" >/dev/null && return 0
    sleep 5
  done
  return 1
}

stop_server() {
  [[ -n "$SERVER_PID" ]] || return 0
  kill "$SERVER_PID" 2>/dev/null
  wait "$SERVER_PID" 2>/dev/null
  SERVER_PID=""
  [[ "$DRY_RUN" == 1 ]] || sleep 15  # let the GPU memory go before the next rung
}
trap stop_server EXIT

for name in $LADDER; do
  spec="$(rung "$name")" || { log "$name: unknown rung, skipped"; continue; }
  IFS='|' read -r model dir quant extra <<<"$spec"
  dest="$OUT/$name"
  mkdir -p "$dest"
  if [[ "$DRY_RUN" != 1 && ! -f "$dir/config.json" ]]; then
    log "$name: no weights in $dir (run scripts/server_setup.sh), skipped"
    continue
  fi
  if port_in_use "$PORT"; then
    log "$name: port $PORT is taken by another program (choose one with PORT=...), skipped"
    continue
  fi
  if [[ "$DRY_RUN" != 1 ]]; then
    claim_cards || { log "$name: no $TP free card(s) within $GPU_WAIT_MINUTES min, skipped"; continue; }
    log "$name: cards $CUDA_VISIBLE_DEVICES"
    echo "$name: cards $CUDA_VISIBLE_DEVICES" >>"$OUT/manifest.txt"
  fi

  log "$name: starting $model on port $PORT"
  start_server "$model" "$dir" "$quant" "$extra" "$dest/server.log"
  if ! wait_ready; then
    log "$name: the server did not come up within $READY_MINUTES min (see $dest/server.log)"
    stop_server
    continue
  fi
  log "$name: ready"

  export DMGATE_LLM_BASE_URL="http://127.0.0.1:$PORT/v1"
  export DMGATE_LLM_API_KEY="$KEY"  # not part of the cache key, so replay needs no key
  export DMGATE_LLM_MODEL="$model"
  [[ "$DRY_RUN" == 1 ]] && export DMGATE_LLM_MODEL="dry-run/$model"  # keeps stub answers out of the real cache
  export DMGATE_LLM_EXTRA_BODY='{"chat_template_kwargs": {"enable_thinking": false}}'

  if "$PYTHON" -m dmgate.llm.evaluate --task compiler,sensitivity --out "$dest/llm-eval" >"$dest/llm-eval.log" 2>&1; then
    log "$name: llm-eval done"
  else
    log "$name: llm-eval FAILED (see $dest/llm-eval.log)"
  fi
  if "$PYTHON" -m dmgate.harness --dataset fsdb-dev-probes --gate rules-llm --n-boot 1000 --out "$dest/rules-llm" \
      ${SAMPLE:+--sample "$SAMPLE"} >"$dest/rules-llm.log" 2>&1; then
    log "$name: rules-llm done"
  else
    log "$name: rules-llm FAILED (see $dest/rules-llm.log)"
  fi
  stop_server
done

echo "finished: $(date -Iseconds)" >>"$OUT/manifest.txt"
tar -czf "$ROOT/results/$RUN.tar.gz" -C "$ROOT/results" "$RUN" $([[ -d "$ROOT/results/llm-cache" ]] && echo llm-cache)
log "results: $ROOT/results/$RUN.tar.gz"
