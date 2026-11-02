# Running the gate's model: vLLM on the ZJU GPU server

The gate's LLM layer (M5) talks to any OpenAI-compatible endpoint. This guide serves open models
with vLLM on the ZJU International campus GPU server and brings the answers home to the Windows
machine, where every run replays without a GPU.

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
- **The server** is one Linux machine with eight RTX 3090 cards, shared with other students. There
  is no job scheduler: you start programs yourself, on cards nobody else is using. Its address is
  a private campus one (10.x.x.x), reachable only from the campus network or through the campus VPN.
- **tmux** keeps programs running after you disconnect: `tmux new -s measure` opens a session,
  `Ctrl-b` then `d` detaches from it, `tmux attach -t measure` comes back. Press `Ctrl-b` once and
  let go before `d`: a second `Ctrl-b` sends a literal `^B` to the program instead. At a prompt
  inside tmux, typing `tmux detach` also works.
- **SSH tunnel.** `ssh -L 8000:127.0.0.1:18xxx zju3090` makes port 8000 on the Windows machine
  forward to port 18xxx on the server, through the encrypted SSH connection. vLLM listens only on
  the server's own loopback address, so nothing is exposed to the network.
- **A git bundle** is the whole repository in one file. Copying it over means the private GitHub
  repository is never needed on the server, and no credentials go there.

## The machine

| | |
|---|---|
| Cards | 8 × RTX 3090, 24 GB each (Ampere) |
| Driver | 590.48, CUDA 13.1 (vLLM's own CUDA build runs on it) |
| CPU, memory | 96 cores, 251 GB |
| System disk `/` | 1.8 TB, **99% full**: nothing large in `/tmp`, ever |
| `/home/part1` | 7 TB, 4.1 TB free: **everything goes in `/home/part1/<user>`**, your home folder |
| Python | `python3` on the path is an old 3.5; the setup uses Ubuntu's 3.12 (`/usr/bin/python3.12`) |
| Docker | installed, but users are not in the `docker` group, so no containers here |
| Network | PyPI, ModelScope, GitHub and hf-mirror.com are reachable |

Everything lives in one folder (the scripts call it `DATA`; `scripts/server_env.sh` sets it):

```
/home/part1/<user>/
  dmgate.bundle    the project as one file (from server.ps1 -Push)
  dmgate/          the repository; results/ inside it
  env/             the Python environment: vLLM 0.31.0 and the project
  models/          the weights
  cache/  tmp/     every cache and temporary file: pip, Hugging Face, ModelScope, vLLM, Triton, torch
```

pip, vLLM and the model hubs default to `~/.cache` and `/tmp`; `/tmp` is on the full system disk.
`server_env.sh` points every one of them into `DATA`, so filling them cannot break the machine and
everything of the project stays in one place.

## Sharing the machine

The scripts are built to be good neighbours:

- **Free cards only.** `measure.sh` and `serve_vllm.sh` take the first card that looks free (under
  1 GiB of memory in use and under 10% busy, as `nvidia-smi` shows), one card per gate model, and
  `measure.sh` waits up to two hours when none is. `GPUS=3` picks a card yourself. Card numbers
  match `nvidia-smi` because the scripts set `CUDA_DEVICE_ORDER=PCI_BUS_ID` (CUDA's own order can
  differ).
- **No idle servers.** `measure.sh` stops each model when its rung is done, so the card goes back
  at once. After a run, `nvidia-smi` should list nothing of yours.
- **A port of your own.** Every user's 127.0.0.1 is the same machine, so port 8000 may belong to
  someone else. The scripts use 18000 plus (your user id mod 1000), and refuse a taken port.
- **A key per server.** `measure.sh` gives each vLLM server a fresh random API key, so other
  users cannot send requests to your model. The key is not part of the answer cache, so replay on
  Windows needs none.
- **Ask first** before long runs on several cards (the M6 agent takes four).

## Memory, in one rule

Weights in bf16 take about 2 bytes per parameter, in FP8 about 1; the attention cache needs room
on top. vLLM takes `--gpu-memory-utilization` of the card (0.90 here) and gives what the weights
leave over to the attention cache.

## The model ladder

Experiment E7 climbs a ladder of gate models, one card each. Start at the bottom; climb only if
accuracy needs it:

| Rung | Model | Parameters | Serve as | GPU memory for weights |
|---|---|---|---|---|
| 1 | `Qwen/Qwen3.5-4B` (Apache-2.0) | 4.7B | bf16, `--language-model-only` | ~9.3 GB |
| 2 | `openpangu/openPangu-Embedded-7B-V1.1` (Huawei; OPENPANGU MODEL LICENSE 1.0) | 8.0B | bf16 | ~16 GB |
| 3 | `Qwen/Qwen3.5-9B` (Apache-2.0) | 9.7B | `QUANT=fp8`, `--language-model-only` | ~10 GB |

- Qwen3.5 models are vision-language models; `--language-model-only` skips the vision encoder and
  frees memory for the attention cache. They think out loud by default; the extra body below turns
  that off.
- **FP8 on the 3090.** Ampere cards have no FP8 arithmetic. vLLM still stores the weights in
  8 bits and computes in 16 bits (its Marlin kernels): the same memory saving as on newer cards,
  a little slower. Weights download in bf16 (~19 GB for the 9B) and are converted at load time.
- openPangu's card documents `transformers` and Ascend NPUs, not vLLM on NVIDIA GPUs. Check that it
  loads (`vllm serve` may need `--trust-remote-code`) and read its licence before using it.
- Larger rungs (27B and up) need two cards with tensor parallelism (`TP=2`) or 4-bit weights;
  they come later.

## Prepare, measure, replay

The GPU is needed for one thing only: producing the model's answers. Everything else (installing,
downloading, testing, analysing) runs without it, and every answer is cached, so one measurement
run gives results that replay on Windows with no GPU at all.

**Once, on Windows: a name for the server.** Open the SSH client's settings file
(`notepad $HOME\.ssh\config`; let Notepad create it) and add, with the server's address and your
user name from the registration:

```
Host zju3090
    HostName <the 3090 server's address>
    User <your user name>
```

Now `ssh zju3090` logs in, and the scripts below find the server by that name. Connect to the
campus network (or the campus VPN) before any of these steps.

1. **Windows: send the project.** Commit first: a bundle carries committed history only.

   ```powershell
   .\scripts\server.ps1 -Push
   ```

2. **Server: prepare (no GPU used).** Log in with `ssh zju3090`. The first time, unpack the
   bundle; then run the setup inside tmux, since the downloads take a while:

   ```bash
   git clone /home/part1/$USER/dmgate.bundle /home/part1/$USER/dmgate   # first time only
   tmux new -s setup
   bash /home/part1/$USER/dmgate/scripts/server_setup.sh
   ```

   It installs vLLM 0.31.0 and the project into `env/`, fetches OPA for Linux (checked against a
   SHA-256 pinned in the script; a download from GitHub that breaks partway resumes, and if it
   keeps breaking, `.\scripts\server.ps1 -Push -Opa` sends the binary from Windows instead),
   downloads Qwen3.5-4B and Qwen3.5-9B into `models/`, runs the test suite and finishes with a
   **dry run** of the measurement script against a stub server, so a broken script cannot waste
   GPU time. Rerunning it skips whatever is done; after a new `-Push` it pulls the new bundle first.
   If pip is slow, add the campus mirror: `PIP_INDEX_URL=https://mirrors.zju.edu.cn/pypi/web/simple`
   in front of `bash`.

3. **Server: measure.**

   ```bash
   tmux new -s measure
   bash /home/part1/$USER/dmgate/scripts/measure.sh
   ```

   For each model on the ladder it claims a free card, serves the model, waits until it is ready,
   runs the smoke check, the compiler and sensitivity measurements and the full `rules-llm` gate on
   the FS/DB dev probes, then stops the server and gives the card back. A failing rung is logged
   and the next one still runs. Detach with `Ctrl-b` then `d`; the run continues. Progress is in
   `results/<stamp>-measure/run.log`; the manifest next to it records the commit, the cards, the
   vLLM version and the times.

4. **Windows: bring the results home and replay.**

   ```powershell
   .\scripts\server.ps1 -Pull
   $env:DMGATE_LLM_REPLAY = "1"
   $env:DMGATE_LLM_MODEL = "Qwen/Qwen3.5-4B"
   $env:DMGATE_LLM_EXTRA_BODY = '{"chat_template_kwargs": {"enable_thinking": false}}'
   .\.venv\Scripts\dmgate-eval.exe --dataset fsdb-dev-probes --gate rules-llm
   ```

   Replay answers only from the cache and stops at the first request it has not seen, so a changed
   prompt is never silently answered by nobody.

Options for `measure.sh`: `LADDER="qwen35-4b"` (one rung), `openpangu-7b` (after downloading it
with `GET=openpangu-7b bash scripts/server_setup.sh`), `SAMPLE=300` (a quick subsample, never for
reported numbers), `DRY_RUN=1` (stub server, no GPU), `GPUS=3` (this card), `TP=2` (two cards per
model), `WORKERS=16` (questions in flight at once; vLLM answers them as one batch, so the card
is busy instead of waiting on one request; the default), `GPU_WAIT_MINUTES=30`, `PORT=18123`.
Latency is measured separately with `WORKERS=1`: with several workers each decision's time
includes its wait behind the others.

## The agent (M6, Q7)

The agent that generates M6's trajectories is Qwen3.5-35B-A3B in FP8 (37.5 GB of weights). On
3090s it runs over **four cards with tensor parallelism** (`TP=4`: about 9.4 GB of weights per
card, the rest for the attention cache that long agent sessions need); two cards would leave only
about 5 GB in all. Still to verify at M6: that vLLM's FP8 kernels for mixture-of-experts layers
run on Ampere. If they do not, the bf16 weights (`Qwen/Qwen3.5-35B-A3B`, about 70 GB) also fit
four cards, with less room for the cache.

The download is slow, so get it out of the way early:

```bash
GET="qwen35-35b-a3b-fp8" bash /home/part1/$USER/dmgate/scripts/server_setup.sh
```

Ask the administrator before holding four cards for hours. Serving the agent, the task
environments and the OpenHands runs come with M6.

## By hand: one model and a tunnel

For poking at a model interactively from Windows.

### On the server

```bash
tmux new -s vllm
KEY=$(od -An -tx1 -N16 /dev/urandom | tr -d ' \n'); echo "$KEY"
MODEL=/home/part1/$USER/models/Qwen3.5-4B NAME=Qwen/Qwen3.5-4B API_KEY=$KEY \
  EXTRA_ARGS="--language-model-only" bash /home/part1/$USER/dmgate/scripts/serve_vllm.sh
```

It prints the port and the card it took. It is ready when the log says the server is running.
Stop it with `Ctrl-c` when you are done: an idle model still holds the card.

### On the Windows machine

1. Open the tunnel in its own PowerShell window and leave it open (the port is the one
   `serve_vllm.sh` printed):

   ```powershell
   ssh -N -L 8000:127.0.0.1:<port> zju3090
   ```

2. Point the gate at it, with the key the server printed:

   ```powershell
   $env:DMGATE_LLM_BASE_URL = "http://127.0.0.1:8000/v1"
   $env:DMGATE_LLM_API_KEY = "<key>"
   $env:DMGATE_LLM_MODEL = "Qwen/Qwen3.5-4B"
   $env:DMGATE_LLM_EXTRA_BODY = '{"chat_template_kwargs": {"enable_thinking": false}}'
   ```

3. Smoke check, then the per-fact measurements, then the whole gate:

   ```powershell
   .\.venv\Scripts\dmgate-llm-eval.exe --task smoke
   .\.venv\Scripts\dmgate-llm-eval.exe --task compiler,sensitivity
   .\.venv\Scripts\dmgate-eval.exe --dataset fsdb-dev-probes --gate rules-llm
   ```

Every answer is cached in `results/llm-cache/<model>.jsonl` (not committed), keyed by the exact
request and the request settings, so a rerun replays the same answers and costs no GPU time.
Delete the file to ask again.

To climb a rung, stop the server, start the next model and change `DMGATE_LLM_MODEL`; for
Qwen3.5-9B add `QUANT=fp8`.

## Rules

- AmPermBench is test-only: never put its prompts in exemplars or tune prompts on it. It stays on
  Windows (its tests skip on the server).
- Record the model id, vLLM version, GPU and the date with every reported number.
- Nothing large in `/tmp` on the server; no idle model on a card.
- If the smoke check fails with `response_format`, try vLLM's own field:
  `--structured structured_outputs`, or `$env:DMGATE_LLM_STRUCTURED = "structured_outputs"`. The
  old `guided_json` field was removed in vLLM 0.12 and may be ignored silently, so the client
  refuses it.
