
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -TypeDefinition @'

using System;
using System.Runtime.InteropServices;
using System.Text;

public static class NCDesktop
{
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
    [StructLayout(LayoutKind.Sequential)] public struct POINT { public int X; public int Y; }

    [DllImport("user32.dll")] static extern bool SetCursorPos(int X, int Y);
    [DllImport("user32.dll")] static extern bool GetCursorPos(out POINT lpPoint);
    [DllImport("user32.dll")] static extern void mouse_event(uint dwFlags, uint dx, uint dy, uint dwData, int dwExtraInfo);
    [DllImport("user32.dll")] static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, int dwExtraInfo);
    [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);
    [DllImport("user32.dll")] static extern bool GetClientRect(IntPtr hWnd, out RECT lpRect);
    [DllImport("user32.dll")] static extern bool ClientToScreen(IntPtr hWnd, ref POINT lpPoint);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint lpdwProcessId);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int GetWindowText(IntPtr hWnd, StringBuilder lpString, int nMaxCount);
    [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll")] static extern bool IsIconic(IntPtr hWnd);
    [DllImport("user32.dll")] static extern uint GetDpiForWindow(IntPtr hWnd);
    [DllImport("user32.dll")] static extern IntPtr WindowFromPoint(POINT point);
    [DllImport("user32.dll")] static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
    [DllImport("user32.dll")] static extern bool PrintWindow(IntPtr hWnd, IntPtr hdc, uint flags);
    delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

    // Window-surface capture. PW_RENDERFULLCONTENT (2) asks the window to render
    // its own content rather than whatever the screen shows; it is what makes a
    // DirectX window capturable while another window covers it. It is NOT a
    // guarantee: some renderers answer black, so the caller must validate the
    // pixels before treating them as a frame of the target.
    public static bool PrintTo(IntPtr hWnd, IntPtr hdc)
    {
        if (hWnd == IntPtr.Zero || IsIconic(hWnd)) return false;
        return PrintWindow(hWnd, hdc, 2);
    }

    public static bool IsMinimized(IntPtr hWnd) { return IsIconic(hWnd); }

    public static string BoundsOf(IntPtr hWnd)
    {
        RECT r;
        if (!GetWindowRect(hWnd, out r)) return "";
        return r.Left + "," + r.Top + "," + r.Right + "," + r.Bottom;
    }

    public static bool Move(int x, int y) { return SetCursorPos(x, y); }
    public static string Cursor()
    {
        POINT p;
        if (!GetCursorPos(out p)) return "none";
        return p.X + "," + p.Y;
    }
    public static void Button(uint flags) { mouse_event(flags, 0, 0, 0, 0); }
    public static void Key(ushort vk, bool down) { keybd_event((byte)vk, 0, down ? 0u : 2u, 0); }

    public static string Describe(IntPtr hWnd)
    {
        if (hWnd == IntPtr.Zero) return "none";
        RECT r;
        if (!GetWindowRect(hWnd, out r)) return "nowin";
        RECT c;
        if (!GetClientRect(hWnd, out c)) { c.Left = 0; c.Top = 0; c.Right = 0; c.Bottom = 0; }
        POINT origin = new POINT { X = c.Left, Y = c.Top };
        ClientToScreen(hWnd, ref origin);
        uint pid;
        GetWindowThreadProcessId(hWnd, out pid);
        uint dpi = 96;
        try { dpi = GetDpiForWindow(hWnd); } catch { dpi = 96; }
        if (dpi == 0) dpi = 96;
        StringBuilder sb = new StringBuilder(512);
        GetWindowText(hWnd, sb, sb.Capacity);
        bool fg = GetForegroundWindow() == hWnd;
        bool iconic = IsIconic(hWnd);
        bool visible = IsWindowVisible(hWnd);
        string clientRel = (origin.X - r.Left) + "," + (origin.Y - r.Top) + "," + c.Right + "," + c.Bottom;
        string scale = ((double)dpi / 96.0).ToString(System.Globalization.CultureInfo.InvariantCulture);
        // ``iconic`` and ``visible`` travel WITH the geometry, not as a second
        // probe: a caller that reads a window while it minimizes must be able to
        // tell "same handle, no usable client area" from "the target moved".
        return hWnd.ToInt64() + "|" + r.Left + "," + r.Top + "," + r.Right + "," + r.Bottom + "|" + clientRel + "|" + pid + "|" + scale + "|" + (fg ? "1" : "0") + "|" + (iconic ? "1" : "0") + "|" + (visible ? "1" : "0") + "|" + sb.ToString();
    }

    public static string Find(string substring)
    {
        IntPtr found = IntPtr.Zero;
        EnumWindows(delegate(IntPtr hWnd, IntPtr lParam)
        {
            if (!IsWindowVisible(hWnd)) return true;
            StringBuilder sb = new StringBuilder(512);
            GetWindowText(hWnd, sb, sb.Capacity);
            string title = sb.ToString();
            if (title.Length > 0 && substring.Length > 0 && title.IndexOf(substring, StringComparison.OrdinalIgnoreCase) >= 0)
            { found = hWnd; return false; }
            return true;
        }, IntPtr.Zero);
        return Describe(found);
    }

    public static string Foreground() { return Describe(GetForegroundWindow()); }
    public static string At(int x, int y) { return Describe(WindowFromPoint(new POINT { X = x, Y = y })); }
    public static string DescribeHandle(long h) { return Describe(new IntPtr(h)); }
}

'@
$sig = '[DllImport("user32.dll")] public static extern bool SetProcessDPIAware();'
$t = Add-Type -MemberDefinition $sig -Name Dpi -Namespace W -PassThru
$t::SetProcessDPIAware() | Out-Null
$bounds = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
$bmp = [System.Drawing.Bitmap]::new($bounds.Width, $bounds.Height)
$g = [System.Drawing.Graphics]::FromImage($bmp)
# One window-surface bitmap, re-allocated only when the window's size changes.
$surface = $null
$surfaceWidth = 0
$surfaceHeight = 0
$out = [Console]::Out
$out.WriteLine('ready ' + $bounds.Width + 'x' + $bounds.Height)
$out.Flush()
while ($true) {
  $line = [Console]::In.ReadLine()
  if ($null -eq $line) { break }
  $line = $line.Trim()
  if ($line -eq '' ) { continue }
  if ($line -eq 'exit') { break }
  if ($line -eq 'stats') {
    $p = [System.Diagnostics.Process]::GetCurrentProcess()
    $out.WriteLine('stats ' + $p.WorkingSet64 + ' ' + [int]$p.TotalProcessorTime.TotalMilliseconds)
    $out.Flush()
    continue
  }
  $sw = [System.Diagnostics.Stopwatch]::StartNew()
  try {
    $parts = $line.Split(' ')
    $verb = $parts[0]
    if ($verb -eq 'move') {
      [NCDesktop]::Move([int]$parts[1], [int]$parts[2]) | Out-Null
      $out.WriteLine('ok ' + $sw.ElapsedMilliseconds + ' moved')
    } elseif ($verb -eq 'button') {
      [NCDesktop]::Button([uint32]$parts[1]) | Out-Null
      $out.WriteLine('ok ' + $sw.ElapsedMilliseconds + ' button')
    } elseif ($verb -eq 'key') {
      [NCDesktop]::Key([uint16]$parts[1], ($parts[2] -eq '1')) | Out-Null
      $out.WriteLine('ok ' + $sw.ElapsedMilliseconds + ' key')
    } elseif ($verb -eq 'find') {
      $sub = $line.Substring(5)
      $out.WriteLine('ok ' + $sw.ElapsedMilliseconds + ' ' + [NCDesktop]::Find($sub))
    } elseif ($verb -eq 'foreground') {
      $out.WriteLine('ok ' + $sw.ElapsedMilliseconds + ' ' + [NCDesktop]::Foreground())
    } elseif ($verb -eq 'cursor') {
      $out.WriteLine('ok ' + $sw.ElapsedMilliseconds + ' ' + [NCDesktop]::Cursor())
    } elseif ($verb -eq 'at') {
      $out.WriteLine('ok ' + $sw.ElapsedMilliseconds + ' ' + [NCDesktop]::At([int]$parts[1], [int]$parts[2]))
    } elseif ($verb -eq 'describe') {
      $out.WriteLine('ok ' + $sw.ElapsedMilliseconds + ' ' + [NCDesktop]::DescribeHandle([long]$parts[1]))
    } elseif ($verb -eq 'printwindow') {
      $handle = [IntPtr]::new([long]$parts[1])
      $path = $line.Substring(('printwindow ' + $parts[1] + ' ').Length)
      if ([NCDesktop]::IsMinimized($handle)) { throw 'target_minimized' }
      $raw = [NCDesktop]::BoundsOf($handle)
      if ($raw -eq '') { throw 'target_unreadable' }
      $coords = $raw.Split(',')
      $w = [int]$coords[2] - [int]$coords[0]
      $hh = [int]$coords[3] - [int]$coords[1]
      if ($w -lt 8 -or $hh -lt 8) { throw 'target_offscreen' }
      if ($null -eq $surface -or $surfaceWidth -ne $w -or $surfaceHeight -ne $hh) {
        if ($null -ne $surface) { $surface.Dispose() }
        $surface = [System.Drawing.Bitmap]::new($w, $hh)
        $surfaceWidth = $w
        $surfaceHeight = $hh
      }
      $sg = [System.Drawing.Graphics]::FromImage($surface)
      $sg.Clear([System.Drawing.Color]::Black)
      $hdc = $sg.GetHdc()
      $printed = $false
      try { $printed = [NCDesktop]::PrintTo($handle, $hdc) } finally { $sg.ReleaseHdc($hdc); $sg.Dispose() }
      if (-not $printed) { throw 'printwindow_refused' }
      $surface.Save($path, [System.Drawing.Imaging.ImageFormat]::Png)
      $size = (Get-Item -LiteralPath $path).Length
      $out.WriteLine('ok ' + $sw.ElapsedMilliseconds + ' ' + $size + ' ' + $w.ToString() + 'x' + $hh.ToString())
    } else {
      $g.CopyFromScreen(0, 0, 0, 0, $bmp.Size)
      $bmp.Save($line, [System.Drawing.Imaging.ImageFormat]::Png)
      $size = (Get-Item -LiteralPath $line).Length
      $dims = $bounds.Width.ToString() + 'x' + $bounds.Height.ToString()
      $out.WriteLine('ok ' + $sw.ElapsedMilliseconds + ' ' + $size + ' ' + $dims)
    }
  } catch {
    $sw.Stop()
    $message = $_.Exception.Message -replace "`r", ' ' -replace "`n", ' '
    $out.WriteLine('err ' + $message)
  }
  $out.Flush()
}
