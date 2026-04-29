param(
    [string]$EnvFile = ".env.staging.local",
    [string]$PythonPath = ""
)

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $scriptRoot "Common-Staging.ps1")

Import-EnvFile -Path $EnvFile
$PythonPath = Resolve-StagingPythonPath -RequestedPath $PythonPath

& $PythonPath -m app.staging
