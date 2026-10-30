# Runs in Windows PowerShell, invoked by a WSL watchdog or manually from Windows.
# Shows an EXISTING tmux session: never starts a second Claude/Codex CLI.
param(
  [Parameter(Mandatory=$true)][ValidatePattern('^agent-(?:handoff|gemini)-[a-f0-9]{10}[string]$Session,
  [ValidateSet('claude','codex','gemini')][string]$Agent = 'codex',
  [string]$Distro = 'Debian'
)
$ErrorActionPreference = 'Stop'
if (-not (Get-Command wt.exe -ErrorAction SilentlyContinue)) {
  throw 'Windows Terminal (wt.exe) is not available. Install Windows Terminal, or attach manually using wsl -d Debian -- tmux attach -t <session>.'
}
if ($Distro -notmatch '^[A-Za-z0-9_.-]+$') { throw 'Invalid WSL distro name.' }
$title = 'RelayWatch - ' + $Agent.ToUpperInvariant()
# Start-Process joins ArgumentList into a command line, so explicitly quote
# the title; everything else is validated and contains no spaces.
$commandLine = '-w new new-tab --title "' + $title + '" wsl.exe -d ' + $Distro + ' -- tmux attach-session -t =' + $Session
Start-Process -FilePath 'wt.exe' -ArgumentList $commandLine -ErrorAction Stop | Out-Null
Write-Host "Opened Windows Terminal for $Agent (tmux session $Session)."
)][string]$Session,
  [ValidateSet('claude','codex')][string]$Agent = 'codex',
  [string]$Distro = 'Debian'
)
$ErrorActionPreference = 'Stop'
if (-not (Get-Command wt.exe -ErrorAction SilentlyContinue)) {
  throw 'Windows Terminal (wt.exe) is not available. Install Windows Terminal, or attach manually using wsl -d Debian -- tmux attach -t <session>.'
}
if ($Distro -notmatch '^[A-Za-z0-9_.-]+$') { throw 'Invalid WSL distro name.' }
$title = 'RelayWatch - ' + $Agent.ToUpperInvariant()
# Start-Process joins ArgumentList into a command line, so explicitly quote
# the title; everything else is validated and contains no spaces.
$commandLine = '-w new new-tab --title "' + $title + '" wsl.exe -d ' + $Distro + ' -- tmux attach-session -t =' + $Session
Start-Process -FilePath 'wt.exe' -ArgumentList $commandLine -ErrorAction Stop | Out-Null
Write-Host "Opened Windows Terminal for $Agent (tmux session $Session)."
