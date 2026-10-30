param(
  [string]$ProjectPath = (Get-Location).Path,
  [string]$Distro = 'Debian',
  [switch]$AutoUpdate,
  [ValidateSet('run','once','status','inspect','launch-codex','launch-gemini','show-gemini','gemini-status','ready','recover-session','show','notify-test','notify-setup','models','model-status','pause','resume','resume-check','unpause','daybreak-continue','daybreak-restored','daybreak-status')][string]$Command = 'run',
  [ValidateSet('claude','codex')][string]$Agent = 'claude',
  [ValidateSet('high','medium')][string]$GeminiModel = 'high',
  [switch]$Retry,
  [switch]$ConfirmComplete,
  [switch]$AllowStaleHandoff
)
$ErrorActionPreference = 'Stop'
$full = [System.IO.Path]::GetFullPath($ProjectPath)
if ($full -notmatch '^([A-Za-z]):\\(.*)$') { throw "Expected a Windows drive path: $full" }
$wslProject = '/mnt/' + $Matches[1].ToLower() + '/' + $Matches[2].Replace('\','/')
if ($Command -eq 'models' -or $Command -eq 'model-status') {
  $pickerAction = if ($Command -eq 'models') { 'choose' } else { 'show' }
  & wsl.exe -d $Distro -- python3 "$wslProject/.agent-watchdog/model_picker.py" $pickerAction
  if ($LASTEXITCODE -ne 0) { throw "Model picker exited $LASTEXITCODE (previous selection preserved)." }
  return
}
$extraArgs = @()
if ($Retry) { $extraArgs += '--retry' }
if ($ConfirmComplete) { $extraArgs += '--confirm-complete' }
if ($AllowStaleHandoff) { $extraArgs += '--allow-stale-handoff' }
if ($Command -eq 'launch-gemini') { $extraArgs += @('--gemini-model', $GeminiModel) }
if ($AutoUpdate) {
  if ($Command -ne 'run') { throw '-AutoUpdate supports -Command run only.' }
  & wsl.exe -d $Distro -- python3 "$wslProject/.agent-watchdog/update_supervisor.py" --project $wslProject
} else {
  & wsl.exe -d $Distro -- python3 "$wslProject/.agent-watchdog/watchdog.py" $Command --agent $Agent @extraArgs
}
if ($LASTEXITCODE -ne 0) { throw "Watchdog exit $LASTEXITCODE." }
