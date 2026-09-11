param(
    [ValidateSet("groth16", "plonk", "stark", "bulletproofs")]
    [string[]]$System,
    [ValidateRange(3, 100)]
    [int]$Repetitions = 10,
    [ValidateRange(1, 20)]
    [int]$Primers = 2,
    [switch]$AllowDirty,
    [switch]$EstimateOnly,
    [switch]$SummarizeOnly
)

$ErrorActionPreference = "Stop"
$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$wslRepo = (& wsl.exe -- wslpath -a $repoRoot).Trim()
if ($LASTEXITCODE -ne 0 -or -not $wslRepo) {
    throw "Could not resolve the repository path inside WSL2"
}

function ConvertTo-BashSingleQuoted([string]$Value) {
    if ($Value.Contains("'")) {
        throw "Apostrophes are not supported in WSL command arguments"
    }
    return "'$Value'"
}

$auditArguments = @(
    "--repetitions", $Repetitions.ToString(),
    "--primers", $Primers.ToString()
)
foreach ($name in $System) {
    $auditArguments += @("--system", $name)
}
if ($AllowDirty) {
    $auditArguments += "--allow-dirty"
}
if ($EstimateOnly) {
    $auditArguments += "--estimate-only"
}
if ($SummarizeOnly) {
    $auditArguments += "--summarize-only"
}

$quotedArguments = ($auditArguments | ForEach-Object {
    ConvertTo-BashSingleQuoted $_
}) -join " "
$quotedRepo = ConvertTo-BashSingleQuoted $wslRepo
$bashCommand = "cd $quotedRepo && PYTHONPATH=src python3 scripts/run_swap_audit.py $quotedArguments"

& wsl.exe -- bash -lc $bashCommand
exit $LASTEXITCODE
