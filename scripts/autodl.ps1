<#
.SYNOPSIS
  Move the project to the AutoDL machine and bring overnight results back (docs/DEPLOY.md).

.DESCRIPTION
  -Push   Packs the repository into one file (a git bundle: the whole history of main) and copies
          it to /root/autodl-tmp/dmgate.bundle. The private GitHub repository is not needed there.
  -Pull   Copies the newest results tarball from the machine into results\ and unpacks it there,
          including the answer cache, so the runs replay on this machine without a GPU.

  scp and ssh ask for the instance's password (shown in the AutoDL console) unless you set up a key.

.EXAMPLE
  .\scripts\autodl.ps1 -SshHost <host from the AutoDL console> -Port <its SSH port> -Push
.EXAMPLE
  .\scripts\autodl.ps1 -SshHost <host from the AutoDL console> -Port <its SSH port> -Pull
#>
param(
    [Parameter(Mandatory = $true)][string]$SshHost,
    [Parameter(Mandatory = $true)][int]$Port,
    [switch]$Push,
    [switch]$Pull
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$remote = "root@$SshHost"

if (-not ($Push -or $Pull)) { throw "say -Push or -Pull" }

if ($Push) {
    $bundle = Join-Path $env:TEMP "dmgate.bundle"
    git -C $root bundle create $bundle HEAD main
    if ($LASTEXITCODE -ne 0) { throw "git bundle failed" }
    scp -P $Port $bundle "${remote}:/root/autodl-tmp/dmgate.bundle"
    if ($LASTEXITCODE -ne 0) { throw "scp failed" }
    Write-Host "copied. First time on the machine:"
    Write-Host "  git clone /root/autodl-tmp/dmgate.bundle /root/autodl-tmp/dmgate"
    Write-Host "  bash /root/autodl-tmp/dmgate/scripts/autodl_setup.sh"
    Write-Host "Later pushes: the setup script pulls the new bundle in step 1."
}

if ($Pull) {
    $latest = ssh -p $Port $remote "ls -t /root/autodl-tmp/dmgate/results/*-night*.tar.gz 2>/dev/null | head -1"
    if (-not $latest) { throw "no results tarball on the machine yet" }
    $results = Join-Path $root "results"
    New-Item -ItemType Directory -Force $results | Out-Null
    $local = Join-Path $results (Split-Path -Leaf $latest)
    scp -P $Port "${remote}:$latest" $local
    if ($LASTEXITCODE -ne 0) { throw "scp failed" }
    tar -xzf $local -C $results
    Write-Host "unpacked $local into $results"
    Write-Host "replay without a GPU:  `$env:DMGATE_LLM_REPLAY = '1'; `$env:DMGATE_LLM_MODEL = 'Qwen/Qwen3.5-4B'"
}
