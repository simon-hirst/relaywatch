param(
  [string]$ProjectPath = (Get-Location).Path,
  [string]$Distro = 'Debian',
  [switch]$EnableAutoLaunch,
  [switch]$Unrestricted
)
$ErrorActionPreference = 'Stop'
function Convert-WslPath([string]$p) {
  $full = [System.IO.Path]::GetFullPath($p)
  if ($full -notmatch '^([A-Za-z]):\\(.*)$') {
    throw "Expected a Windows drive path (for example C:\Users\...); received: $full"
  }
  return ('/mnt/' + $Matches[1].ToLower() + '/' + $Matches[2].Replace('\','/'))
}
$tool = Convert-WslPath $PSScriptRoot
$project = Convert-WslPath $ProjectPath
Write-Host "Installing into $ProjectPath (WSL: $project)"
$extra = @()
if ($EnableAutoLaunch) { $extra += '--enable-auto-launch' }
if ($Unrestricted) { $extra += '--unrestricted' }
& wsl.exe -d $Distro -- python3 "$tool/install.py" --project "$project" @extra
if ($LASTEXITCODE -ne 0) { throw "Installer failed (exit $LASTEXITCODE)." }
Write-Host "`nInstalled. Start with: powershell -NoProfile -ExecutionPolicy Bypass -File `"$PSScriptRoot\watchdog-windows.ps1`" -ProjectPath `"$ProjectPath`""
