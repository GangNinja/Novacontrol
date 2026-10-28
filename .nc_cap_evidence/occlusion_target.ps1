# A DISPOSABLE animated window for the capture-occlusion probe.
# It never activates itself, so another window can cover it while it keeps
# painting. No game, no input, no network.
param([int]$Seconds = 120)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$form = New-Object System.Windows.Forms.Form
$form.Text = 'NC CAPTURE OCCLUSION TARGET'
$form.Width = 620
$form.Height = 420
$form.StartPosition = 'Manual'
$form.Left = 120
$form.Top = 120
$form.TopMost = $false

$script:tick = 0
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 120
$timer.Add_Tick({
  $script:tick += 1
  $form.Invalidate()
})

$form.Add_Paint({
  param($sender, $e)
  $g = $e.Graphics
  $g.Clear([System.Drawing.Color]::FromArgb(12, 18, 30))
  $x = 20 + (($script:tick * 7) % 480)
  $brush = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(220, 120, 40))
  $g.FillEllipse($brush, $x, 120, 90, 90)
  $font = New-Object System.Drawing.Font('Segoe UI', 16)
  $g.DrawString("tick $($script:tick)", $font, [System.Drawing.Brushes]::White, 20, 20)
})

$closer = New-Object System.Windows.Forms.Timer
$closer.Interval = $Seconds * 1000
$closer.Add_Tick({ $form.Close() })

$timer.Start()
$closer.Start()
[System.Windows.Forms.Application]::Run($form)
