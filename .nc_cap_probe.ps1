param(
  [Parameter(Mandatory = $true)][long]$Handle,
  [Parameter(Mandatory = $true)][string]$Out,
  [int]$Flags = 2
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing

Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class NcPrintProbe {
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr hdc, uint flags);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool GetClientRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
}
"@

$h = [IntPtr]::new($Handle)
$r = New-Object NcPrintProbe+RECT
[NcPrintProbe]::GetWindowRect($h, [ref]$r) | Out-Null
$c = New-Object NcPrintProbe+RECT
[NcPrintProbe]::GetClientRect($h, [ref]$c) | Out-Null
$w = [Math]::Max(1, $r.Right - $r.Left)
$ht = [Math]::Max(1, $r.Bottom - $r.Top)

$bmp = New-Object System.Drawing.Bitmap($w, $ht, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.Clear([System.Drawing.Color]::Magenta)
$hdc = $g.GetHdc()
$ok = $false
try {
  $ok = [NcPrintProbe]::PrintWindow($h, $hdc, [uint32]$Flags)
} finally {
  $g.ReleaseHdc($hdc)
}
$g.Dispose()
$bmp.Save($Out, [System.Drawing.Imaging.ImageFormat]::Png)

# Sample a grid; a black/blank surface is the failure mode PrintWindow has with
# DirectX games, so measure it instead of assuming the bytes are the game.
$nonBlack = 0
$nonMagenta = 0
$total = 0
$step = 16
for ($y = 0; $y -lt $ht; $y += $step) {
  for ($x = 0; $x -lt $w; $x += $step) {
    $p = $bmp.GetPixel($x, $y)
    $total++
    if ($p.R -gt 8 -or $p.G -gt 8 -or $p.B -gt 8) { $nonBlack++ }
    if (-not ($p.R -gt 250 -and $p.B -gt 250 -and $p.G -lt 10)) { $nonMagenta++ }
  }
}
$bmp.Dispose()
[pscustomobject]@{
  handle = $Handle
  printwindow = $ok
  flags = $Flags
  iconic = [NcPrintProbe]::IsIconic($h)
  visible = [NcPrintProbe]::IsWindowVisible($h)
  foreground = ([NcPrintProbe]::GetForegroundWindow() -eq $h)
  width = $w
  height = $ht
  client = "$($c.Right)x$($c.Bottom)"
  non_black_ratio = if ($total -gt 0) { [Math]::Round($nonBlack / $total, 4) } else { 0 }
  painted_ratio = if ($total -gt 0) { [Math]::Round($nonMagenta / $total, 4) } else { 0 }
  path = $Out
} | ConvertTo-Json -Compress
