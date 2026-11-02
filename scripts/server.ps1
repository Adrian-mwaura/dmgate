<#
.SYNOPSIS
  Move the project to a ZJU GPU server and bring measurement results back (docs/DEPLOY.md).

.DESCRIPTION
  -Push   Packs the repository into one file (a git bundle: the whole history of main) and copies
          it to <Data>/dmgate.bundle on the server. The private GitHub repository is not needed there.
  -Opa    With -Push: also sends OPA's Linux binary (tools\opa\opa_linux_amd64_static, downloaded
          from GitHub first if missing), for when the server's own download from GitHub breaks. The
          setup script checks it against the SHA-256 pinned there before using it.
  -Cache  With -Push: also sends this machine's answer caches (results\llm-cache) and merges them
          into the server's by request key (the server must be set up). A new server then answers
          the questions other machines already asked the same way, and asks only new ones.
  -Pull   Copies every results tarball the server has and this machine does not (dry runs aside)
          into results\, unpacks the runs there and merges each tarball's answer cache into
          results\llm-cache by request key, so runs from several servers replay here without a GPU.

  -Server is what you would type after ssh: an entry in ~/.ssh/config (see docs/DEPLOY.md):
  zju3090 (the default), zju4090 or zju4090b; or user@address. -Data is the server folder that holds
  dmgate/; by default your home folder there (/home/part1/<you> on the 3090, /data/home/<you> on the
  4090s). The servers are on the campus network: connect to it (or the campus VPN) first.

.EXAMPLE
  .\scripts\server.ps1 -Push
.EXAMPLE
  .\scripts\server.ps1 -Server zju4090 -Push -Cache
.EXAMPLE
  .\scripts\server.ps1 -Server zju4090 -Pull
#>
param(
    [string]$Server = "zju3090",
    [string]$Data = "",
    [switch]$Push,
    [switch]$Pull,
    [switch]$Opa,
    [switch]$Cache
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$results = Join-Path $root "results"

if (-not ($Push -or $Pull)) { throw "say -Push or -Pull" }
# scp and ssh start relative paths in the home folder, so without -Data every path is relative.
$prefix = if ($Data) { "$($Data.TrimEnd('/'))/" } else { "" }
$shown = if ($Data) { $Data.TrimEnd('/') } else { "~" }
$hint = "cannot reach ${Server}. Type the passphrase within two minutes of the prompt (the server " +
        "closes the connection after that), and be on the campus network or the campus VPN."

if ($Push) {
    $bundle = Join-Path $env:TEMP "dmgate.bundle"
    git -C $root bundle create $bundle HEAD main
    if ($LASTEXITCODE -ne 0) { throw "git bundle failed" }
    $files = @($bundle)
    if ($Opa) {
        $opaLinux = Join-Path $root "tools\opa\opa_linux_amd64_static"
        if (-not (Test-Path $opaLinux)) {
            New-Item -ItemType Directory -Force (Split-Path $opaLinux) | Out-Null
            curl.exe -fsSL -o $opaLinux "https://github.com/open-policy-agent/opa/releases/download/v1.21.2/opa_linux_amd64_static"
            if ($LASTEXITCODE -ne 0) { Remove-Item -Force -ErrorAction SilentlyContinue $opaLinux; throw "OPA download failed" }
        }
        $files += $opaLinux
    }
    scp @files "${Server}:$prefix"  # one connection, so one passphrase
    if ($LASTEXITCODE -ne 0) { throw $hint }
    Write-Host "copied to ${shown}: $(($files | ForEach-Object { Split-Path -Leaf $_ }) -join ', '). First time on the server:"
    Write-Host "  git clone $shown/dmgate.bundle $shown/dmgate"
    Write-Host "  bash $shown/dmgate/scripts/server_setup.sh"
    Write-Host "Later pushes: the setup script pulls the new bundle in step 1."

    if ($Cache) {
        $caches = @(Get-ChildItem (Join-Path $results "llm-cache") -Filter *.jsonl |
                    Where-Object { $_.Name -notlike "dry-run*" -and $_.Name -notlike "par-check*" })
        if (-not $caches) { throw "no answer caches in results\llm-cache" }
        ssh $Server "mkdir -p ${prefix}dmgate/results/llm-cache-incoming"
        if ($LASTEXITCODE -ne 0) { throw $hint }
        scp @($caches.FullName) "${Server}:${prefix}dmgate/results/llm-cache-incoming/"
        if ($LASTEXITCODE -ne 0) { throw $hint }
        ssh $Server ("cd ${prefix}dmgate && ../env/bin/python -m dmgate.llm.merge_cache results/llm-cache-incoming " +
                     "--into results/llm-cache && rm -r results/llm-cache-incoming")
        if ($LASTEXITCODE -ne 0) { throw "merging the caches on $Server failed: is the server set up (env/)?" }
    }
}

if ($Pull) {
    $listing = ssh $Server "ls -t ${prefix}dmgate/results/*-measure*.tar.gz 2>/dev/null; true"
    if ($LASTEXITCODE -ne 0) { throw $hint }
    New-Item -ItemType Directory -Force $results | Out-Null
    $new = @($listing | Where-Object {
        $_ -and $_ -notlike "*-dry.tar.gz" -and -not (Test-Path (Join-Path $results (Split-Path -Leaf $_)))
    })
    if (-not $new) { Write-Host "nothing new on $Server"; return }
    scp @($new | ForEach-Object { "${Server}:$_" }) $results
    if ($LASTEXITCODE -ne 0) { throw $hint }
    foreach ($remote in $new) {
        $tarball = Join-Path $results (Split-Path -Leaf $remote)
        $run = (Split-Path -Leaf $remote) -replace '\.tar\.gz$', ''
        $staging = Join-Path $env:TEMP "dmgate-pull-$run"
        New-Item -ItemType Directory -Force $staging | Out-Null
        tar -xzf $tarball -C $staging
        if (-not (Test-Path (Join-Path $results $run))) { Move-Item (Join-Path $staging $run) $results }
        if (Test-Path (Join-Path $staging "llm-cache")) {
            & $python -m dmgate.llm.merge_cache (Join-Path $staging "llm-cache") --into (Join-Path $results "llm-cache")
        }
        Remove-Item -Recurse -Force $staging
        Write-Host "unpacked $run into $results"
    }
    Write-Host "replay without a GPU:  `$env:DMGATE_LLM_REPLAY = '1'; `$env:DMGATE_LLM_MODEL = 'Qwen/Qwen3.5-4B'"
}
