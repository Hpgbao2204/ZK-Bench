$ErrorActionPreference = "Stop"

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$localRoot = Join-Path $repoRoot ".local"

$env:CARGO_HOME = Join-Path $localRoot "cargo-home"
$env:CARGO_TARGET_DIR = Join-Path $localRoot "cargo-target"
$env:PIP_CACHE_DIR = Join-Path $localRoot "pip-cache"
$env:PYTHONPYCACHEPREFIX = Join-Path $localRoot "python-cache"
$env:PYTHONPATH = Join-Path $repoRoot "src"
$env:GIT_CONFIG_GLOBAL = Join-Path $localRoot "git-home\gitconfig"

New-Item -ItemType Directory -Force -Path @(
    $env:CARGO_HOME,
    $env:CARGO_TARGET_DIR,
    $env:PIP_CACHE_DIR,
    $env:PYTHONPYCACHEPREFIX
) | Out-Null

Write-Output "Project-local environment configured under $localRoot"
