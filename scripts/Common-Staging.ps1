Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Import-EnvFile {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Arquivo de ambiente nao encontrado: $Path"
    }

    Get-Content -LiteralPath $Path | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith("#")) {
            return
        }

        $separatorIndex = $line.IndexOf("=")
        if ($separatorIndex -lt 1) {
            return
        }

        $name = $line.Substring(0, $separatorIndex).Trim()
        $value = $line.Substring($separatorIndex + 1).Trim()
        [System.Environment]::SetEnvironmentVariable($name, $value, "Process")
    }
}

function Resolve-StagingPythonPath {
    param(
        [string]$RequestedPath
    )

    if ($RequestedPath -and (Test-Path -LiteralPath $RequestedPath)) {
        return $RequestedPath
    }

    $candidates = @(
        ".\.venv312\Scripts\python.exe",
        ".\.venv\Scripts\python.exe"
    )

    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) {
            return $candidate
        }
    }

    if ($RequestedPath) {
        throw "Python do virtualenv nao encontrado: $RequestedPath"
    }

    throw "Nenhum Python de staging encontrado. Esperado em .\.venv312\Scripts\python.exe ou .\.venv\Scripts\python.exe"
}
