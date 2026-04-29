param(
    [string]$EnvFile = ".env.staging.local",
    [string]$PythonPath = "",
    [string]$BindHost = "0.0.0.0",
    [int]$Port = 8000,
    [switch]$PrintOnly
)

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $scriptRoot "Common-Staging.ps1")

Import-EnvFile -Path $EnvFile
$PythonPath = Resolve-StagingPythonPath -RequestedPath $PythonPath

$arguments = @(
    "-m",
    "uvicorn",
    "app.main:app",
    "--host",
    $BindHost,
    "--port",
    $Port
)

if ($PrintOnly) {
    Write-Host "$PythonPath $($arguments -join ' ')"
    exit 0
}

& $PythonPath @arguments
