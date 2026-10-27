# Running the gate's model: vLLM on AutoDL

The gate's LLM layer (M5) talks to any OpenAI-compatible endpoint. This guide serves an open model
with vLLM on an AutoDL GPU and reaches it from the Windows machine through an SSH tunnel.

## What the pieces are

- **vLLM** loads a model's weights onto the GPU and serves them over HTTP, with the same API shape
  as OpenAI's (`POST /v1/chat/completions`). It is fast because it batches many requests at once
  and stores the attention cache in small pages (PagedAttention), so the GPU stays busy.
- **Structured outputs.** When a request carries `response_format: {"type": "json_schema", ...}`,
  vLLM restricts which tokens the model may produce at each step, so the answer is always JSON
  that fits the schema. The gate relies on this: every model answer is an enum or a quoted span.
- **AutoDL** rents GPU machines by the hour. You get a Linux box with SSH access; nothing on it is
  reachable from outside except SSH.
- **SSH tunnel.** `ssh -L 8000:127.0.0.1:8000 ...` makes port 8000 on your Windows machine forward
  to port 8000 on the GPU machine, through the encrypted SSH connection. vLLM listens only on the
  GPU machine's own loopback address, so nothing is exposed to the internet.

## Choosing a card and a model

Weights in bf16 take about 2 bytes per parameter; the attention cache needs room on top.

| Model size | Weights (bf16) | Fits on |
|---|---|---|
| ~4B | ~8 GB | any 24 GB card (RTX 3090/4090) |
| ~8B | ~16 GB | a 24 GB card, with a modest context length |
| ~14B | ~28 GB | 40-48 GB cards, or a 24 GB card with 4-bit AWQ/GPTQ weights |
| ~32B | ~64 GB | 80 GB cards (A100/H800), or 4-bit weights on a 40-48 GB card |

The challenge asks for the smallest model that works, and experiment E7 measures a size ladder.
Start with a small instruct model that vLLM supports with structured outputs (for example a
4B-8B Qwen3 instruct model, or openPangu-Embedded-7B), then climb. Check each model card for
its exact id, licence and vLLM support before downloading.

## On the AutoDL machine

1. Create an instance with a PyTorch/CUDA image. The console shows the SSH host, port and password.
   Put models on the data disk (`/root/autodl-tmp`), not the small system disk.
2. If downloads from Hugging Face are slow, turn on AutoDL's academic network acceleration
   (`source /etc/network_turbo`) or use a mirror (`export HF_ENDPOINT=https://hf-mirror.com`), or
   load from ModelScope (`export VLLM_USE_MODELSCOPE=True`).
3. Install vLLM in its own environment and write the version down with the results:

   ```bash
   python -m venv /root/autodl-tmp/vllm-env && source /root/autodl-tmp/vllm-env/bin/activate
   pip install vllm
   vllm --version
   ```

4. Serve the model (the repository's `scripts/serve_vllm.sh` wraps this):

   ```bash
   MODEL=Qwen/Qwen3-8B PORT=8000 bash scripts/serve_vllm.sh
   ```

   It listens on 127.0.0.1 only. Leave it running (inside `tmux` or `screen`, so it survives the
   SSH session).

## On the Windows machine

1. Open the tunnel and leave the window open (Windows 10/11 ship an OpenSSH client):

   ```powershell
   ssh -N -L 8000:127.0.0.1:8000 -p <port> root@<autodl-host>
   ```

2. Check the endpoint answers:

   ```powershell
   curl.exe http://127.0.0.1:8000/v1/models
   ```

3. Point the gate at it. Qwen3 models think out loud by default; the extra body turns that off,
   which the gate's short structured answers do not need:

   ```powershell
   $env:DMGATE_LLM_BASE_URL = "http://127.0.0.1:8000/v1"
   $env:DMGATE_LLM_MODEL = "Qwen/Qwen3-8B"
   $env:DMGATE_LLM_EXTRA_BODY = '{"chat_template_kwargs": {"enable_thinking": false}}'
   ```

4. Measure the LLM layer on the FS/DB dev benchmark, then the whole gate:

   ```powershell
   .\.venv\Scripts\dmgate-llm-eval.exe --task compiler,sensitivity
   .\.venv\Scripts\dmgate-eval.exe --dataset fsdb-dev-probes --gate rules-llm
   ```

Every answer is cached in `results/llm-cache/<model>.jsonl` (not committed), keyed by the exact
request, so a rerun replays the same answers and costs no GPU time. Delete the file to ask again.

## Rules

- AmPermBench is test-only: never put its prompts in exemplars or tune prompts on it.
- Record the model id, vLLM version, GPU and the date with every reported number.
- If the server rejects `response_format`, older vLLM versions take `guided_json` instead:
  `--structured guided_json` for `dmgate-llm-eval`, or `$env:DMGATE_LLM_STRUCTURED = "guided_json"`.
