param(
    [string]$PythonVersion = "3.12",
    [switch]$PrintOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-WingetPath {
    $command = Get-Command winget.exe -ErrorAction SilentlyContinue
    if (-not $command) {
        throw "winget.exe nao encontrado. Instale o App Installer da Microsoft Store ou instale Python manualmente."
    }
    return $command.Source
}

function Get-WingetPackageId {
    param(
        [string]$Version
    )

    switch ($Version) {
        "3.11" { return "Python.Python.3.11" }
        "3.12" { return "Python.Python.3.12" }
        default { throw "Versao nao suportada para bootstrap automatico: $Version" }
    }
}

$wingetPath = Get-WingetPath
$packageId = Get-WingetPackageId -Version $PythonVersion
$arguments = @(
    "install",
    "--id", $packageId,
    "--exact",
    "--accept-package-agreements",
    "--accept-source-agreements"
)

if ($PrintOnly) {
    Write-Host "$wingetPath $($arguments -join ' ')"
    exit 0
}

& $wingetPath @arguments
