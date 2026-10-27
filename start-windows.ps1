# Start the WSL-backed monitor in a separate PowerShell window.
param(
    [string]$ProjectPath = (Get-Location).Path,
    [string]$Distro = 'Debian',
    [switch]$UseSavedModels
)
$ErrorActionPreference = 'Stop'
$launcher = Join-Path $PSScriptRoot 'start-watchdog-window.ps1'
& $launcher -ProjectPath $ProjectPath -Distro $Distro -UseSavedModels:$UseSavedModels
