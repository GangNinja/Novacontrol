# NF-23 lifecycle probe window — DISPOSABLE, never a game.
#
# Two solid colour regions with very different channel magnitudes, so a frame
# read through the wrong channel order is unmistakable:
#
#   left  (majority): RGB(220, 30, 20)   red-dominant
#   right (160 px):   RGB( 20, 60, 220)  blue-dominant
#
# It never activates itself and never goes TopMost (unless -Cover is given), so
# it cannot steal focus from whatever the operator is doing. -Cover draws the
# same shape in a third colour as a TOPMOST window at the same screen position,
# which is how the "occluded target" case is produced without touching any
# other application.
param(
  [int]$Seconds = 90,
  [int]$X = 80,
  [int]$Y = 80,
  [switch]$Cover
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$form = New-Object System.Windows.Forms.Form
$form.Text = if ($Cover) { 'NC NF23 COVER LAYER' } else { 'NC NF23 LIFECYCLE PROBE' }
$form.Width = 420
$form.Height = 300
$form.StartPosition = 'Manual'
$form.Location = New-Object System.Drawing.Point($X, $Y)
$form.BackColor = if ($Cover) {
  [System.Drawing.Color]::FromArgb(15, 200, 40)
} else {
  [System.Drawing.Color]::FromArgb(220, 30, 20)
}
$form.ShowInTaskbar = $false
if ($Cover) { $form.TopMost = $true }

$panel = New-Object System.Windows.Forms.Panel
$panel.Dock = 'Right'
$panel.Width = 160
$panel.BackColor = if ($Cover) {
  [System.Drawing.Color]::FromArgb(200, 15, 200)
} else {
  [System.Drawing.Color]::FromArgb(20, 60, 220)
}
$form.Controls.Add($panel)

$closeTimer = New-Object System.Windows.Forms.Timer
$closeTimer.Interval = [Math]::Max(1000, $Seconds * 1000)
$closeTimer.Add_Tick({ $closeTimer.Stop(); $form.Close() })
$closeTimer.Start()

[void]$form.ShowDialog()
