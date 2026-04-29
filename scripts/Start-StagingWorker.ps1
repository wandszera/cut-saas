param(
    [string]$EnvFile = ".env.staging.local",
    [string]$PythonPath = "",
    [float]$PollInterval = 5,
    [Nullable[int]]$MaxJobs = $null,
    [switch]$Once,
    [switch]$PrintOnly
)

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $scriptRoot "Common-Staging.ps1")

Import-EnvFile -Path $EnvFile
$PythonPath = Resolve-StagingPythonPath -RequestedPath $PythonPath

$arguments = @(
    "-m",
    "app.worker",
    "--poll-interval",
    $PollInterval
)

if ($Once) {
    $arguments += "--once"
}

if ($null -ne $MaxJobs) {
    $arguments += @("--max-jobs", $MaxJobs)
}

if ($PrintOnly) {
    Write-Host "$PythonPath $($arguments -join ' ')"
    exit 0
}

& $PythonPath @arguments
