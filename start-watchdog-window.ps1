param(
  [string]$ProjectPath = (Get-Location).Path,
  [string]$Distro = 'Debian',
  [switch]$UseSavedModels
)
$ErrorActionPreference = 'Stop'
$script = Join-Path $PSScriptRoot 'watchdog-windows.ps1'
if (-not $UseSavedModels) {
  Write-Host 'Select models and effort for future Claude/Codex launches.'
  & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $script -ProjectPath $ProjectPath -Distro $Distro -Command models
  if ($LASTEXITCODE -ne 0) {
    Write-Host 'Model selection cancelled/failed. No new watcher launched.'
    exit 2
  }
}
$arguments = "-NoProfile -ExecutionPolicy Bypass -NoExit -File `"$script`" -ProjectPath `"$ProjectPath`" -Distro `"$Distro`""
Start-Process -FilePath 'powershell.exe' -ArgumentList $arguments
Write-Host 'Launched usage watchdog in a separate PowerShell window.'
