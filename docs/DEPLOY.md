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
  The gate's client checks every answer against the schema again, and `dmgate-llm-eval` runs a
  **smoke check** first: it asks for `"no"` while the schema allows only `"yes"`, so a server that
  silently ignores schemas is caught before any number is measured.
- **AutoDL** rents GPU machines by the hour. You get a Linux box with SSH access; nothing on it is
  reachable from outside except SSH.
- **SSH tunnel.** `ssh -L 8000:127.0.0.1:8000 ...` makes port 8000 on your Windows machine forward
  to port 8000 on the GPU machine, through the encrypted SSH connection. vLLM listens only on the
  GPU machine's own loopback address, so nothing is exposed to the internet.

## Memory, in one rule

Weights in bf16 take about 2 bytes per parameter, in FP8 about 1; the attention cache needs room
on top. vLLM takes `--gpu-memory-utilization` of the card (0.90 here) and gives what the weights
leave over to the attention cache.

## This project's machine: RTX 4090, 24 GB

AutoDL Inner Mongolia zone B: RTX 4090 (24 GB), 16-core Xeon Gold 6430, 120 GB RAM, 30 GB system
disk, 50 GB data disk, driver 595.71 (CUDA up to 13.2; vLLM's own CUDA 12.x build runs on it).

**Model ladder on this card** (experiment E7; start at the bottom, climb only if accuracy needs it):

| Rung | Model | Parameters | Serve as | GPU memory for weights |
|---|---|---|---|---|
| 1 | `Qwen/Qwen3.5-4B` (Apache-2.0) | 4.7B | bf16, `--language-model-only` | ~9.3 GB |
| 2 | `openpangu/openPangu-Embedded-7B-V1.1` (Huawei; OPENPANGU MODEL LICENSE 1.0) | 8.0B | bf16 | ~16 GB |
| 3 | `Qwen/Qwen3.5-9B` (Apache-2.0) | 9.7B | `QUANT=fp8`, `--language-model-only` | ~10 GB |

- Qwen3.5 models are vision-language models; `--language-model-only` skips the vision encoder and
  frees memory for the attention cache. They think out loud by default; the extra body below turns
  that off.
- openPangu's card documents `transformers` and Ascend NPUs, not vLLM on NVIDIA GPUs. Check that it
  loads (`vllm serve` may need `--trust-remote-code`) and read its licence before using it.
- Larger rungs (27B and up) need a second card or 4-bit weights; they come later.

**Disk budget (50 GB data disk):** the vLLM environment ~10 GB, Qwen3.5-4B ~9.3 GB, openPangu-7B
~16 GB, Qwen3.5-9B ~19 GB (FP8 is computed at load time from the bf16 download). Keep at most two
models at a time; delete one (`rm -rf /root/autodl-tmp/modelscope/...`) or expand the disk before
the next.

## When GPUs are scarce: prepare by day, run by night

The GPU is needed for one thing only: producing the model's answers. Everything else (installing,
downloading weights, testing, analysing) runs without it, and every answer is cached, so one night
of GPU time gives results that replay all day on Windows with no GPU at all.

1. **Windows, any time.** Copy the project over. A git bundle is the whole repository in one file,
   so the private GitHub repository is not needed on the machine (commit first: a bundle carries
   committed history only):

   ```powershell
   .\scripts\autodl.ps1 -SshHost <host> -Port <port> -Push
   ```

2. **AutoDL, no-GPU mode (by day).** Start the instance with "无卡模式" (no GPU attached; when it
   is off, only storage is billed). In its terminal:

   ```bash
   git clone /root/autodl-tmp/dmgate.bundle /root/autodl-tmp/dmgate   # first time only
   bash /root/autodl-tmp/dmgate/scripts/autodl_setup.sh
   ```

   It installs Python, vLLM 0.31.0 and the project, fetches OPA for Linux (checksum-verified),
   downloads Qwen3.5-4B and Qwen3.5-9B into `/root/autodl-tmp/models`, runs the test suite, and
   finishes with a **dry run** of the overnight script against a stub server, so a broken script
   cannot waste a night. Rerunning it skips whatever is done; after a new `-Push` it pulls the
   new bundle first. Shut the instance down when it says ready.

3. **AutoDL with a GPU (at night).** Start the instance normally, then:

   ```bash
   tmux new -s night
   SHUTDOWN=1 bash /root/autodl-tmp/dmgate/scripts/overnight.sh
   ```

   For each model on the ladder it serves the model, waits until it is ready, runs the smoke
   check, the compiler and sensitivity measurements and the full `rules-llm` gate on the FS/DB dev
   probes, then stops the server. A failing rung is logged and the next one still runs. With
   `SHUTDOWN=1` it powers the machine off at the end, which stops GPU billing (if that fails, stop
   the instance from the console). Detach with `Ctrl-b` then `d`; the run continues.

4. **Windows, next morning.** Bring the results and the answer cache home, then replay without a
   GPU:

   ```powershell
   .\scripts\autodl.ps1 -SshHost <host> -Port <port> -Pull
   $env:DMGATE_LLM_REPLAY = "1"
   $env:DMGATE_LLM_MODEL = "Qwen/Qwen3.5-4B"
   $env:DMGATE_LLM_EXTRA_BODY = '{"chat_template_kwargs": {"enable_thinking": false}}'
   .\.venv\Scripts\dmgate-eval.exe --dataset fsdb-dev-probes --gate rules-llm
   ```

   Replay answers only from the cache and stops at the first request it has not seen, so a changed
   prompt is never silently answered by nobody.

Options for `overnight.sh`: `LADDER="qwen35-4b"` (one rung), `openpangu-7b` (after downloading it
with `GET=openpangu-7b bash scripts/autodl_setup.sh`), `SAMPLE=300` (a quick subsample, never for
reported numbers), `DRY_RUN=1` (stub server, no GPU).

## Step by step, by hand

### On the AutoDL machine

Open a terminal (AutoDL's JupyterLab terminal, or `ssh -p <port> root@<host>` from PowerShell).

1. Check the card and pick the network route. AutoDL's academic acceleration helps if your region
   offers it; ModelScope hosts the same Qwen weights inside China:

   ```bash
   nvidia-smi
   source /etc/network_turbo   # only if this file exists on your instance
   ```

2. Install vLLM 0.31.0 (pinned: two weeks older than the newest release) on the data disk:

   ```bash
   cd /root/autodl-tmp
   python3 -m venv vllm-env && source vllm-env/bin/activate
   pip install "vllm==0.31.0" modelscope
   vllm --version
   ```

3. Copy the serve script over. From PowerShell on Windows, in the project folder:

   ```powershell
   scp -P <port> scripts/serve_vllm.sh root@<host>:/root/autodl-tmp/
   ```

4. Serve rung 1 inside `tmux`, so it keeps running when you disconnect (`Ctrl-b` then `d` to
   detach, `tmux attach -t vllm` to come back):

   ```bash
   tmux new -s vllm
   source /root/autodl-tmp/vllm-env/bin/activate
   export VLLM_USE_MODELSCOPE=True
   MODEL=Qwen/Qwen3.5-4B EXTRA_ARGS="--language-model-only" bash /root/autodl-tmp/serve_vllm.sh
   ```

   The first start downloads the weights. It is ready when the log says the server is running on
   `http://127.0.0.1:8000`.

### On the Windows machine

1. Open the tunnel in its own PowerShell window and leave it open:

   ```powershell
   ssh -N -L 8000:127.0.0.1:8000 -p <port> root@<host>
   ```

2. In another window, check the endpoint answers:

   ```powershell
   curl.exe http://127.0.0.1:8000/v1/models
   ```

3. Point the gate at it:

   ```powershell
   $env:DMGATE_LLM_BASE_URL = "http://127.0.0.1:8000/v1"
   $env:DMGATE_LLM_MODEL = "Qwen/Qwen3.5-4B"
   $env:DMGATE_LLM_EXTRA_BODY = '{"chat_template_kwargs": {"enable_thinking": false}}'
   ```

4. Smoke check, then the per-fact measurements, then the whole gate:

   ```powershell
   .\.venv\Scripts\dmgate-llm-eval.exe --task smoke
   .\.venv\Scripts\dmgate-llm-eval.exe --task compiler,sensitivity
   .\.venv\Scripts\dmgate-eval.exe --dataset fsdb-dev-probes --gate rules-llm
   ```

Every answer is cached in `results/llm-cache/<model>.jsonl` (not committed), keyed by the exact
request and the request settings, so a rerun replays the same answers and costs no GPU time.
Delete the file to ask again.

To climb a rung, stop the server (`Ctrl-c` in the tmux window), start the next model, and change
`DMGATE_LLM_MODEL`; for Qwen3.5-9B add `QUANT=fp8`.

## Rules

- AmPermBench is test-only: never put its prompts in exemplars or tune prompts on it.
- Record the model id, vLLM version, GPU and the date with every reported number.
- If the smoke check fails with `response_format`, try vLLM's own field:
  `--structured structured_outputs`, or `$env:DMGATE_LLM_STRUCTURED = "structured_outputs"`. The
  old `guided_json` field was removed in vLLM 0.12 and may be ignored silently, so the client
  refuses it.
