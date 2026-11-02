<#
.SYNOPSIS
  Move the project to the ZJU GPU server and bring measurement results back (docs/DEPLOY.md).

.DESCRIPTION
  -Push   Packs the repository into one file (a git bundle: the whole history of main) and copies
          it to <Data>/dmgate.bundle on the server. The private GitHub repository is not needed there.
  -Pull   Copies the newest results tarball from the server into results\ and unpacks it there,
          including the answer cache, so the runs replay on this machine without a GPU.

  -Server is what you would type after ssh: the "zju3090" entry in ~/.ssh/config (the default; see
  docs/DEPLOY.md), or user@address. -Data defaults to /home/part1/<your user on the server>.
  The server is on the campus network: connect to it (or the campus VPN) first.

.EXAMPLE
  .\scripts\server.ps1 -Push
.EXAMPLE
  .\scripts\server.ps1 -Pull
#>
param(
    [string]$Server = "zju3090",
    [string]$Data = "",
    [switch]$Push,
    [switch]$Pull
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

if (-not ($Push -or $Pull)) { throw "say -Push or -Pull" }
if (-not $Data) {
    # ssh -G prints the settings ssh would use for this server without connecting.
    $user = (ssh -G $Server | Where-Object { $_ -like "user *" } | Select-Object -First 1) -replace "^user ", ""
    $Data = "/home/part1/$user"
}
$hint = "cannot reach ${Server}: are you on the campus network or the campus VPN?"

if ($Push) {
    $bundle = Join-Path $env:TEMP "dmgate.bundle"
    git -C $root bundle create $bundle HEAD main
    if ($LASTEXITCODE -ne 0) { throw "git bundle failed" }
    scp $bundle "${Server}:$Data/dmgate.bundle"
    if ($LASTEXITCODE -ne 0) { throw $hint }
    Write-Host "copied to $Data/dmgate.bundle. First time on the server:"
    Write-Host "  git clone $Data/dmgate.bundle $Data/dmgate"
    Write-Host "  bash $Data/dmgate/scripts/server_setup.sh"
    Write-Host "Later pushes: the setup script pulls the new bundle in step 1."
}

if ($Pull) {
    $latest = ssh $Server "ls -t $Data/dmgate/results/*-measure*.tar.gz 2>/dev/null | head -1"
    if ($LASTEXITCODE -ne 0) { throw $hint }
    if (-not $latest) { throw "no results tarball on the server yet" }
    $results = Join-Path $root "results"
    New-Item -ItemType Directory -Force $results | Out-Null
    $local = Join-Path $results (Split-Path -Leaf $latest)
    scp "${Server}:$latest" $local
    if ($LASTEXITCODE -ne 0) { throw $hint }
    tar -xzf $local -C $results
    Write-Host "unpacked $local into $results"
    Write-Host "replay without a GPU:  `$env:DMGATE_LLM_REPLAY = '1'; `$env:DMGATE_LLM_MODEL = 'Qwen/Qwen3.5-4B'"
}
