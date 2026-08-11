param(
  [Parameter(Mandatory=$true)][string]$Title,
  [Parameter(Mandatory=$true)][string]$Message
)
# Windows GUI alert is separate from the watchdog process. The watchdog can
# continue polling while this short-lived popup helper displays a notification.
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$notify = New-Object System.Windows.Forms.NotifyIcon
try {
  $notify.Icon = [System.Drawing.SystemIcons]::Information
  $notify.Text = 'RelayWatch'
  $notify.Visible = $true
  $notify.BalloonTipTitle = $Title.Substring(0,[Math]::Min(60,$Title.Length))
  $notify.BalloonTipText = $Message.Substring(0,[Math]::Min(250,$Message.Length))
  $notify.BalloonTipIcon = [System.Windows.Forms.ToolTipIcon]::Warning
  $notify.ShowBalloonTip(12000)
  Start-Sleep -Seconds 13
} finally {
  $notify.Visible = $false
  $notify.Dispose()
}
