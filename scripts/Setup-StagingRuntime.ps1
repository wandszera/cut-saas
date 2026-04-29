param(
    [string]$PythonVersion = "",
    [string]$VenvPath = ".\.venv",
    [switch]$PrintOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-PythonLauncherEntries {
    $output = & py -0p 2>$null
    if (-not $output) {
        return @()
    }

    $entries = @()
    foreach ($line in $output) {
        if ($line -match "-V:(?<version>\d+\.\d+)\s+\*?\s*(?<path>.+)$") {
            $entries += [PSCustomObject]@{
                Version = $Matches["version"]
                Path = $Matches["path"].Trim()
            }
        }
    }
    return $entries
}

function Resolve-PreferredPython {
    param(
        [string]$RequestedVersion
    )

    $entries = Get-PythonLauncherEntries
    if (-not $entries.Count) {
        throw "Nenhum interpretador Python encontrado via 'py -0p'."
    }

    if ($RequestedVersion) {
        $requested = $entries | Where-Object { $_.Version -eq $RequestedVersion } | Select-Object -First 1
        if (-not $requested) {
            throw "Python $RequestedVersion nao encontrado. Instalados: $($entries.Version -join ', ')"
        }
        return $requested
    }

    $preferred = $entries | Where-Object { $_.Version -in @("3.12", "3.11") } | Select-Object -First 1
    if ($preferred) {
        return $preferred
    }

    $installed = $entries.Version -join ", "
    throw "Nenhum Python compativel com faster-whisper encontrado. Instale Python 3.11 ou 3.12. Instalados: $installed"
}

$python = Resolve-PreferredPython -RequestedVersion $PythonVersion
$venvPythonPath = Join-Path $VenvPath "Scripts\python.exe"

$commands = @(
    "py -$($python.Version) -m venv $VenvPath",
    "$venvPythonPath -m pip install --upgrade pip",
    "$venvPythonPath -m pip install -r requirements.txt"
)

Write-Host "Python selecionado para staging: $($python.Version) ($($python.Path))"

if ($PrintOnly) {
    foreach ($command in $commands) {
        Write-Host $command
    }
    exit 0
}

& py -$($python.Version) -m venv $VenvPath
& $venvPythonPath -m pip install --upgrade pip
& $venvPythonPath -m pip install -r requirements.txt

