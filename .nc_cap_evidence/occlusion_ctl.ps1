param(
  [Parameter(Mandatory = $true)][ValidateSet('minimize', 'restore', 'close', 'state')][string]$Action,
  [Parameter(Mandatory = $true)][string]$Title
)

$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public static class NcOcclusion {
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern bool ShowWindowAsync(IntPtr h, int cmd);
  [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h, uint msg, IntPtr w, IntPtr l);
  [DllImport("user32.dll")] public static extern bool GetWindowText(IntPtr h, StringBuilder s, int max);
  public static string Text(IntPtr h) { StringBuilder b = new StringBuilder(512); GetWindowText(h, b, b.Capacity); return b.ToString(); }
}
"@

$needle = $Title.ToLowerInvariant()
$handles = @()
Get-Process | Where-Object { $_.MainWindowHandle -ne 0 } | ForEach-Object {
  $text = [NcOcclusion]::Text($_.MainWindowHandle)
  if ($text.ToLowerInvariant().Contains($needle)) { $handles += $_.MainWindowHandle }
}
if ($handles.Count -eq 0) { throw "no window matches '$Title'" }
$handle = [IntPtr]::new([long]$handles[0])

switch ($Action) {
  'minimize' { [NcOcclusion]::ShowWindowAsync($handle, 6) | Out-Null }
  'restore' { [NcOcclusion]::ShowWindowAsync($handle, 9) | Out-Null }
  'close' { [NcOcclusion]::PostMessage($handle, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero) | Out-Null }
  'state' { }
}
Start-Sleep -Milliseconds 250
[pscustomobject]@{
  handle = [long]$handle
  title = [NcOcclusion]::Text($handle)
  minimized = [NcOcclusion]::IsIconic($handle)
  visible = [NcOcclusion]::IsWindowVisible($handle)
} | ConvertTo-Json -Compress
