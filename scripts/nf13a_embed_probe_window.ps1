# NF-13A disposable embedding probe — a real native window, NOT a game.
#
# It exists so the embedded-game host can be exercised against a windowed,
# third-party-style HWND without launching (or crashing) a game. It deliberately
# does NOT steal focus, inject input, or touch a game: the pixels change on a
# timer so a live frame can be distinguished from a stale grab.
#
# Title:  NC NF13A EMBED PROBE
# Size:   900x500 windowed (fixed frame), so WS_CAPTION/WS_THICKFRAME are present
#         and the host's fullscreen refusal does not apply.

param(
  [int]$Seconds = 180,
  [string]$Title = "NC NF13A EMBED PROBE",
  [string]$LogPath = ""
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$form = New-Object System.Windows.Forms.Form
$form.Text = $Title
$form.Width = 900
$form.Height = 500
$form.StartPosition = 'CenterScreen'
$form.FormBorderStyle = 'FixedSingle'
$form.MaximizeBox = $false
$form.MinimizeBox = $true

# Draw the WHOLE client area from the real paint clip. A docked child label
# kept its pre-reparent bounds in the cross-process host, which made a correct
# embed look clipped; painting the clip rectangle removes that fixture artifact.
$font = New-Object System.Drawing.Font('Consolas', 20)
$script:frame = 0
$form.Add_Paint({
  param($sender, $eventArgs)
  # A paint that fails must not take the fixture down with it. PrintWindow on a
  # CROSS-PROCESS hosted window hands this process a DC it may refuse; an
  # escaping GDI+ exception would exit the process and look like the host (or
  # the capture attempt) having killed the window.
  try {
    $g = $eventArgs.Graphics
    $r = $eventArgs.ClipRectangle
    $shade = if (($script:frame % 2) -eq 0) { 40 } else { 54 }
    $g.Clear([System.Drawing.Color]::FromArgb(8, $shade, 88))
    $brush = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(0, 240, 255))
    $text = "NF-13A EMBED PROBE`r`n`r`nframe $($script:frame) - real native window`r`nreparented into NovaControl`r`nnot a screenshot and not a game"
    $format = New-Object System.Drawing.StringFormat
    $format.Alignment = [System.Drawing.StringAlignment]::Center
    $format.LineAlignment = [System.Drawing.StringAlignment]::Center
    $g.DrawString($text, $font, $brush, [System.Drawing.RectangleF]$r, $format)
    $brush.Dispose()
  } catch {
    # The next timer tick repaints anyway. Record it, because a paint failure is
    # exactly what a failed capture of a hosted window looks like from outside.
    $script:paintFailures = [int]$script:paintFailures + 1
    if ($LogPath) {
      $line = "paint_failure $($script:paintFailures) frame=$($script:frame) :: $($_.Exception.GetType().Name): $($_.Exception.Message)"
      try { Add-Content -LiteralPath $LogPath -Value $line -ErrorAction Stop } catch { }
    }
  }
})
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 120
$timer.Add_Tick({
  $script:frame = $script:frame + 1
  $form.Invalidate()
})
$timer.Start()

# The lifetime is TIME-bounded, not $form.Visible-bounded. WinForms reads
# Visible through the parent chain, so the moment this window is parented to the
# NovaControl surface and the dashboard is hidden (a tab switch) Visible goes
# false and a "while ($form.Visible)" loop ends the PROCESS. That looked exactly
# like "the host destroyed the fixture" in three probe runs: exit code 0, empty
# stderr, no paint failures, window gone. The fixture must outlive the host's
# hidden/shown cycles and only close when asked or when its time is up.
[void]$form.Show()
$deadline = [DateTime]::UtcNow.AddSeconds([Math]::Max(5, $Seconds))
while (-not $form.IsDisposed -and [DateTime]::UtcNow -lt $deadline) {
  [System.Windows.Forms.Application]::DoEvents()
  Start-Sleep -Milliseconds 30
}
if (-not $form.IsDisposed) { $form.Close() }
