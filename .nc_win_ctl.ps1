param(
  [Parameter(Mandatory = $true)][ValidateSet('state', 'restore', 'focus')][string]$Action,
  [long]$Handle = 0,
  [string]$Title = ''
)

$ErrorActionPreference = 'Stop'

Add-Type -TypeDefinition @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public static class NcCapWin {
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
  [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
  [DllImport("user32.dll")] public static extern bool IsZoomed(IntPtr h);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool GetClientRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern int GetWindowLong(IntPtr h, int index);
  [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr h, StringBuilder s, int max);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll")] public static extern bool ShowWindowAsync(IntPtr h, int cmd);
  [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr h);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
  [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint a, uint b, bool attach);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr p);
  public delegate bool EnumProc(IntPtr h, IntPtr p);
  public static string Text(IntPtr h) {
    StringBuilder b = new StringBuilder(512);
    GetWindowText(h, b, b.Capacity);
    return b.ToString();
  }
  public static bool Claim(IntPtr h) {
    ShowWindowAsync(h, 9);
    bool attached = false;
    uint mine = GetCurrentThreadId();
    IntPtr current = GetForegroundWindow();
    if (current != IntPtr.Zero && current != h) {
      uint pid;
      uint theirs = GetWindowThreadProcessId(current, out pid);
      if (theirs != 0 && theirs != mine) { attached = AttachThreadInput(mine, theirs, true); }
    }
    BringWindowToTop(h);
    bool ok = SetForegroundWindow(h);
    if (attached) {
      uint pid2;
      uint theirs2 = GetWindowThreadProcessId(current, out pid2);
      AttachThreadInput(mine, theirs2, false);
    }
    return ok;
  }
  public static IntPtr Find(string needle) {
    IntPtr found = IntPtr.Zero;
    string want = needle.ToLowerInvariant();
    EnumWindows(delegate(IntPtr h, IntPtr p) {
      if (!IsWindowVisible(h)) { return true; }
      if (Text(h).ToLowerInvariant().Contains(want)) { found = h; return false; }
      return true;
    }, IntPtr.Zero);
    return found;
  }
}
"@

function Report([IntPtr]$h) {
  if ($h -eq [IntPtr]::Zero -or -not [NcCapWin]::IsWindow($h)) {
    [pscustomobject]@{ handle = $Handle; valid = $false } | ConvertTo-Json -Compress
    return
  }
  $rect = New-Object NcCapWin+RECT
  [NcCapWin]::GetWindowRect($h, [ref]$rect) | Out-Null
  $client = New-Object NcCapWin+RECT
  [NcCapWin]::GetClientRect($h, [ref]$client) | Out-Null
  $procId = 0
  [NcCapWin]::GetWindowThreadProcessId($h, [ref]$procId) | Out-Null
  $rLeft = [int]$rect.Left; $rTop = [int]$rect.Top; $rRight = [int]$rect.Right; $rBottom = [int]$rect.Bottom
  $fg = [NcCapWin]::GetForegroundWindow()
  [pscustomobject]@{
    handle = $h.ToInt64()
    title = [NcCapWin]::Text($h)
    pid = $procId
    valid = $true
    minimized = [NcCapWin]::IsIconic($h)
    maximized = [NcCapWin]::IsZoomed($h)
    visible = [NcCapWin]::IsWindowVisible($h)
    foreground = ($fg -eq $h)
    bounds = @($rLeft, $rTop, ($rRight - $rLeft), ($rBottom - $rTop))
    client = @([int]$client.Right, [int]$client.Bottom)
    style = ('0x{0:X8}' -f [NcCapWin]::GetWindowLong($h, -16))
  } | ConvertTo-Json -Compress
}

if ($Action -eq 'focus' -and $Title) {
  $target = [NcCapWin]::Find($Title)
  if ($target -eq [IntPtr]::Zero) {
    Write-Output '{"valid":false,"error":"no window matched"}'
    exit 1
  }
  $Handle = $target.ToInt64()
  [NcCapWin]::Claim($target) | Out-Null
}

if ($Action -eq 'restore' -or $Action -eq 'focus') {
  $h = [IntPtr]::new($Handle)
  [NcCapWin]::Claim($h) | Out-Null
  Start-Sleep -Milliseconds 600
  Report $h
  exit 0
}

Report ([IntPtr]::new($Handle))
