"""Screen capture as a RESOURCE, not a process per frame.

The game-control audit profiled the screenshot path: every capture spawned a
fresh PowerShell, which re-loaded ``System.Windows.Forms`` and
``System.Drawing`` and re-compiled the ``Add-Type -MemberDefinition``
P/Invoke declaration (Roslyn runs on every call) before it could BitBlt a
single pixel. That cost is per PROCESS, not per frame — so a capture that pays
it once and then serves frames is the difference between a screenshot and a
stream. ``scripts/benchmark_screen_capture.py`` measures both arms with the
same script text; the numbers, not this paragraph, choose the implementation.

One owner per program, no duplicated PowerShell:

* :func:`oneshot_capture_script` — the historical one-process-per-frame path,
  kept as the fallback for a machine where the worker cannot start at all.
* :class:`PersistentScreenCapture` — one worker process, N frames, with the
  initialization hoisted out of the loop. It speaks a line protocol on the
  worker's stdin/stdout and reports PER-FRAME latency, byte size and its own
  working set, so a caller can tell a fresh frame from a remembered path.

The path travels as a plain protocol line rather than being interpolated into
the script, so a directory containing an apostrophe (``O'Brien``) can no
longer break a capture. Nothing here spawns anything at import time: the
worker starts on the first capture that needs it.
"""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
import sys
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from novacontrol.desktop.capture_validation import THUMB_SUFFIX as _THUMB_SUFFIX
from novacontrol.desktop.input import WindowGeometry

__all__ = [
    "THUMB_SUFFIX",
    "CaptureResult",
    "PersistentScreenCapture",
    "WindowGeometry",
    "WorkerStats",
    "capture_supported_here",
    "looks_like_png",
    "oneshot_capture_script",
    "sweep_orphan_thumbnails",
]

#: C# interop, compiled ONCE by the worker at startup. It lives in the worker
#: process for a specific reason: this process calls ``SetProcessDPIAware``, so
#: ``SetCursorPos`` and ``GetWindowRect`` here speak PHYSICAL pixels — the same
#: space the captured screenshot is in. Injecting input from anywhere else
#: (a non-DPI-aware Python process) would land every click off by the DPI scale.
_INPUT_INTEROP_CS = r'''
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
'''

#: The worker program. Everything expensive happens BEFORE the read loop:
#: the two assemblies load once, the C# interop compiles once, the DPI
#: declaration runs once, and the bitmap/graphics surface is allocated once and
#: re-filled per frame by ``CopyFromScreen``. It serves capture frames AND
#: coordinate-addressed input/geometry, so both speak ONE DPI-aware space.
_WORKER_PROGRAM = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -TypeDefinition @'
""" + _INPUT_INTEROP_CS + r"""
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
# The 48x27 paint thumbnail: raw BGR bytes (48*27*3 = 3888, stride 144 needs no
# padding). It exists so a caller can judge the paint WITHOUT decoding the full
# frame: at 5.2 MP a PNG decode costs ~35 ms warm and 300+ ms cold, more than
# the capture it judges. GDI+ writes 24bppRgb as B,G,R in memory; the reader
# knows that, and the statistics are still computed in exactly one place.
function Save-NCThumb($source, $path) {
  $tw = 48; $th = 27
  $thumb = [System.Drawing.Bitmap]::new($tw, $th, [System.Drawing.Imaging.PixelFormat]::Format24bppRgb)
  $tg = [System.Drawing.Graphics]::FromImage($thumb)
  $tg.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
  $tg.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
  $tg.DrawImage($source, 0, 0, $tw, $th)
  $tg.Dispose()
  $rect = [System.Drawing.Rectangle]::new(0, 0, $tw, $th)
  $data = $thumb.LockBits($rect, [System.Drawing.Imaging.ImageLockMode]::ReadOnly, [System.Drawing.Imaging.PixelFormat]::Format24bppRgb)
  $bytes = [byte[]]::new($tw * $th * 3)
  [System.Runtime.InteropServices.Marshal]::Copy($data.Scan0, $bytes, 0, $bytes.Length)
  $thumb.UnlockBits($data)
  $thumb.Dispose()
  [System.IO.File]::WriteAllBytes($path, $bytes)
}
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
      $rest = $line.Substring(('printwindow ' + $parts[1] + ' ').Length)
      # A tab cannot occur in a Windows path, so ``path<TAB>thumb`` is
      # unambiguous while a space-containing directory still works.
      $path = $rest
      $thumbPath = ''
      if ($rest.Contains("`t")) {
        $pieces = $rest.Split("`t")
        $path = $pieces[0]
        $thumbPath = $pieces[1]
      }
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
      if ($thumbPath -ne '') { Save-NCThumb $surface $thumbPath }
      $size = (Get-Item -LiteralPath $path).Length
      $out.WriteLine('ok ' + $sw.ElapsedMilliseconds + ' ' + $size + ' ' + $w.ToString() + 'x' + $hh.ToString())
    } elseif ($verb -eq 'display') {
      # Explicit display verb, used when a thumbnail is wanted: the historical
      # bare path line stays the no-thumbnail form.
      $rest = $line.Substring(8)
      $path = $rest
      $thumbPath = ''
      if ($rest.Contains("`t")) {
        $pieces = $rest.Split("`t")
        $path = $pieces[0]
        $thumbPath = $pieces[1]
      }
      $g.CopyFromScreen(0, 0, 0, 0, $bmp.Size)
      $bmp.Save($path, [System.Drawing.Imaging.ImageFormat]::Png)
      if ($thumbPath -ne '') { Save-NCThumb $bmp $thumbPath }
      $size = (Get-Item -LiteralPath $path).Length
      $dims = $bounds.Width.ToString() + 'x' + $bounds.Height.ToString()
      $out.WriteLine('ok ' + $sw.ElapsedMilliseconds + ' ' + $size + ' ' + $dims)
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
"""

#: The one-process-per-frame program. Identical in effect to the worker's first
#: frame; kept for machines where the worker cannot start (no long-lived
#: PowerShell, a shell that closes stdin, a policy that kills persistent
#: hosts). ``{script_path}`` is a single-quoted PowerShell literal — see
#: :func:`oneshot_capture_script`.
_ONESHOT_TEMPLATE = (
    "Add-Type -AssemblyName System.Windows.Forms; "
    "Add-Type -AssemblyName System.Drawing; "
    "$sig = '[DllImport(\"user32.dll\")] public static extern bool SetProcessDPIAware();'; "
    "$t = Add-Type -MemberDefinition $sig -Name Dpi -Namespace W -PassThru; "
    "$t::SetProcessDPIAware() | Out-Null; "
    "$bounds = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds; "
    "$bmp = [System.Drawing.Bitmap]::new($bounds.Width, $bounds.Height); "
    "$g = [System.Drawing.Graphics]::FromImage($bmp); "
    "$g.CopyFromScreen(0, 0, 0, 0, $bmp.Size); "
    "$bmp.Save({script_path}); "
    "Write-Output 'screenshot_saved'"
)

#: The eight bytes every PNG starts with. A capture that "exists" but is not a
#: PNG is not evidence of a screen.
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

#: Suffix of the 48x27 paint thumbnail written beside a frame when one is asked
#: for. The name and the layout both belong to the reader, which owns the
#: statistics computed from it; this module only writes it. Re-exported here
#: because a caller asking for a thumbnail needs the name it will appear under.
THUMB_SUFFIX = _THUMB_SUFFIX


def sweep_orphan_thumbnails(directory: str | Path) -> int:
    """Remove paint side cars in ``directory`` whose frame no longer exists.

    This module names them ``<frame>.thumb``, so a side car whose ``<frame>`` is
    gone is garbage — a frame prune that removed the PNG, or a process that died
    between writing the two. The router deletes each one as it reads it, which is
    the normal path; this sweep is the backstop. Returns how many were removed,
    and an unreadable directory is worth zero rather than an exception: pruning
    must never be able to fail a session.
    """
    try:
        candidates = list(Path(directory).glob("*" + THUMB_SUFFIX))
    except OSError:
        return 0
    removed = 0
    for path in candidates:
        if path.with_name(path.name[: -len(THUMB_SUFFIX)]).exists():
            continue
        try:
            path.unlink()
        except OSError:
            continue
        removed += 1
    return removed


def capture_supported_here() -> bool:
    """Whether this platform has the capture this module implements."""
    return sys.platform.startswith("win")


def oneshot_capture_script(save_path: str) -> str:
    """The per-frame script, with the path escaped for PowerShell.

    The path is embedded as a single-quoted literal, so an apostrophe in a
    directory name must be DOUBLED (``''``) — PowerShell's own rule — not
    dropped or backticked. The worker never needs this: it receives the path as
    protocol data.
    """
    literal = "'" + str(save_path).replace("'", "''") + "'"
    return _ONESHOT_TEMPLATE.format(script_path=literal)


def looks_like_png(path: str) -> bool:
    """Whether the file at ``path`` starts with the PNG signature."""
    try:
        with open(path, "rb") as handle:
            return handle.read(len(_PNG_MAGIC)) == _PNG_MAGIC
    except OSError:
        return False


@dataclass(frozen=True, slots=True)
class CaptureResult:
    """The outcome of one capture request."""

    ok: bool
    path: str = ""
    error: str = ""
    width: int = 0
    height: int = 0
    elapsed_ms: int = 0
    size_bytes: int = 0
    #: True when the persistent worker served the frame, False when a one-shot
    #: process did — the benchmark's whole comparison rests on this flag.
    persistent: bool = False
    #: For input/geometry commands: the worker's detail payload (e.g. a window
    #: geometry record). Empty for capture frames.
    detail: str = ""
    #: What the frame contains: ``display`` (the composited desktop, full screen)
    #: or ``window_surface`` (the target's own rendered surface, window sized).
    #: Empty for input/geometry commands.
    source: str = ""
    #: The capture backend that produced it (``gdi_display`` / ``printwindow``).
    backend: str = ""
    #: The 48x27 paint thumbnail written beside this frame, when one was asked
    #: for. Empty means no thumbnail: the paint must then be judged from the
    #: full frame, which is exactly the decode this side car exists to avoid.
    thumbnail: str = ""


@dataclass(frozen=True, slots=True)
class WorkerStats:
    """The capture worker's own resource use, as the worker reports it."""

    working_set_bytes: int = 0
    cpu_ms: int = 0


@dataclass(slots=True)
class PersistentScreenCapture:
    """One capture worker process, reused across frames.

    Serialization matters: ``CopyFromScreen``/``Save`` are asked one at a time
    through :attr:`_lock`, so two concurrent captures cannot interleave their
    frames. A worker that dies is restarted once, transparently — and a worker
    that cannot start leaves the caller to fall back, reporting *why* instead
    of raising out of a screenshot.
    """

    program: str = "powershell"
    #: Tried in order when the first host is absent. Windows PowerShell 5.1
    #: ships as ``powershell``; PowerShell 7 only as ``pwsh``, and a machine can
    #: have either. A capture resource that gives up because the WRONG shell is
    #: installed would be a regression against the one-shot path it replaces.
    fallback_programs: tuple[str, ...] = ("pwsh",)
    #: The host's full argument list. None means "the built-in capture program";
    #: a caller with its own host supplies its own list.
    arguments: tuple[str, ...] | None = None
    startup_timeout: float = 25.0
    frame_timeout: float = 20.0
    #: How long a worker gets to honour the polite ``exit`` before it is
    #: terminated. Cleanup must be bounded: shutdown cannot wait forever on a
    #: host that stopped listening.
    shutdown_grace: float = 3.0
    stderr_tail_lines: int = 40
    _process: asyncio.subprocess.Process | None = field(default=None, init=False, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _stderr_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _stderr_tail: deque[str] = field(default_factory=deque, init=False, repr=False)
    _closed: bool = field(default=False, init=False)
    #: True when a cancelled exchange left a reply unread; the next exchange
    #: restarts the worker rather than reading someone else's answer.
    _desynced: bool = field(default=False, init=False, repr=False)
    spawns: int = field(default=0, init=False)
    frames: int = field(default=0, init=False)
    size: tuple[int, int] = field(default=(0, 0), init=False)
    last_error: str = field(default="", init=False)
    last_stderr: str = field(default="", init=False)

    @property
    def running(self) -> bool:
        process = self._process
        return process is not None and process.returncode is None

    def _arguments(self) -> tuple[str, ...]:
        if self.arguments is not None:
            return self.arguments
        return ("-NoProfile", "-NoLogo", "-NonInteractive", "-Command", _WORKER_PROGRAM)

    def _hosts(self) -> tuple[str, ...]:
        if self.arguments is not None:
            return (self.program,)
        return (self.program, *self.fallback_programs)

    async def ensure_started(self) -> bool:
        """Start the worker if it is not running. Returns whether it is up."""
        if self._closed:
            return False
        if self.arguments is None and not capture_supported_here():
            self.last_error = f"screen capture is not implemented on {sys.platform}"
            return False
        if self.running:
            return True
        errors: list[str] = []
        for host in self._hosts():
            if await self._start_host(host):
                return True
            errors.append(f"{host}: {self.last_error}")
        self.last_error = "could not start the capture worker (" + "; ".join(errors) + ")"
        return False

    async def _start_host(self, host: str) -> bool:
        """Start ONE host and wait for its ready banner; False leaves none behind."""
        try:
            self._process = await asyncio.create_subprocess_exec(
                host,
                *self._arguments(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # A console window created for the worker would take the
                # FOREGROUND the moment a session starts -- which is exactly the
                # window the Guardian requires to be focused before it injects a
                # key. Without this flag, starting capture could deny the first
                # action of every controlled run. It buys nothing else: the
                # worker speaks stdin/stdout and never needs a console.
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:
            self._process = None
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False
        self.spawns += 1
        self._stderr_tail.clear()
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        banner = await self._read_line(self.startup_timeout)
        if banner is not None and banner.startswith("ready "):
            self.size = _parse_size(banner.removeprefix("ready ").strip())
            return True
        self.last_error = banner or f"did not become ready in {self.startup_timeout:.0f}s"
        if banner is None and self.last_stderr:
            self.last_error = f"{self.last_error} ({self.last_stderr[-200:]})"
        await self._stop_process()
        return False

    async def capture(
        self, save_path: str, *, timeout: float | None = None, thumbnail: bool = False
    ) -> CaptureResult:
        """Write one fresh PNG frame to ``save_path`` through the worker.

        ``thumbnail=True`` also asks for the 48x27 paint thumbnail beside it, so
        validation does not have to decode the full frame.
        """
        target = str(Path(save_path).resolve())
        thumb = target + THUMB_SUFFIX if thumbnail else ""
        line = f"display {target}\t{thumb}" if thumbnail else target
        reply, failure = await self._exchange(line, timeout or self.frame_timeout)
        if reply is None:
            return CaptureResult(False, path=target, error=failure)
        if reply.startswith("err "):
            return CaptureResult(False, path=target, error=reply[4:].strip(), persistent=True)
        parts = reply.split(" ")
        width, height = _parse_size(parts[3]) if len(parts) >= 4 else (0, 0)
        size_bytes = int(parts[2]) if len(parts) >= 3 else 0
        elapsed_ms = int(parts[1]) if len(parts) >= 2 else 0
        self.frames += 1
        return CaptureResult(
            True,
            path=target,
            width=width,
            height=height,
            elapsed_ms=elapsed_ms,
            size_bytes=size_bytes,
            persistent=True,
            source="display",
            backend="gdi_display",
            thumbnail=thumb,
        )

    async def capture_window(
        self,
        handle: int,
        save_path: str,
        *,
        timeout: float | None = None,
        thumbnail: bool = False,
    ) -> CaptureResult:
        """Capture the TARGET WINDOW's own surface, at window size, via PrintWindow.

        This is the backend that can see a window another window covers: it asks
        the window to render itself instead of copying the composited display.
        Two honest failure modes travel back instead of a black frame: a
        minimized window is refused (``target_minimized``), and a renderer that
        will not answer PrintWindow raises ``printwindow_refused``. A frame that
        is produced but unpainted is caught by the caller's content validation.
        """
        target = str(Path(save_path).resolve())
        thumb = target + THUMB_SUFFIX if thumbnail else ""
        line = (
            f"printwindow {int(handle)} {target}\t{thumb}"
            if thumbnail
            else f"printwindow {int(handle)} {target}"
        )
        reply, failure = await self._exchange(line, timeout or self.frame_timeout)
        if reply is None:
            return CaptureResult(False, path=target, error=failure, source="window_surface",
                                backend="printwindow")
        if reply.startswith("err "):
            return CaptureResult(
                False, path=target, error=reply[4:].strip(), persistent=True,
                source="window_surface", backend="printwindow",
            )
        parts = reply.split(" ")
        width, height = _parse_size(parts[3]) if len(parts) >= 4 else (0, 0)
        size_bytes = int(parts[2]) if len(parts) >= 3 else 0
        elapsed_ms = int(parts[1]) if len(parts) >= 2 else 0
        self.frames += 1
        return CaptureResult(
            True,
            path=target,
            width=width,
            height=height,
            elapsed_ms=elapsed_ms,
            size_bytes=size_bytes,
            persistent=True,
            source="window_surface",
            backend="printwindow",
            thumbnail=thumb,
        )

    async def send_input(self, command: str, *, timeout: float | None = None) -> CaptureResult:
        """Send ONE input/geometry command (``move x y``, ``button f``, ``key vk 1``,
        ``find <title>``, ``foreground``, ``at x y``) through the worker.

        The worker is the SAME DPI-aware process that captures frames, so a
        coordinate read off a screenshot and a click injected here live in one
        physical-pixel space — no off-by-DPI drift between what was seen and
        where input lands.
        """
        reply, failure = await self._exchange(command, timeout or self.frame_timeout)
        if reply is None:
            return CaptureResult(False, error=failure)
        if reply.startswith("err "):
            return CaptureResult(False, error=reply[4:].strip(), persistent=True)
        parts = reply.split(" ")
        # The worker normally answers ``ok <ms> <detail>``. A reply that omits the
        # elapsed field (``ok <detail>``) still carries the fact we asked for, and
        # reading its first token as a millisecond count raised ValueError on a
        # live window's geometry line — which failed that capture and every
        # observation that followed it. The number is a measurement, never the
        # payload, so a missing one is 0 rather than an exception.
        elapsed_ms = 0
        detail = ""
        if len(parts) >= 3 and parts[1].isdigit():
            elapsed_ms = int(parts[1])
            detail = reply.split(" ", 2)[2]
        elif len(parts) >= 2 and reply.startswith("ok "):
            detail = reply[len("ok ") :].strip()
        return CaptureResult(True, error="", elapsed_ms=elapsed_ms, persistent=True, detail=detail)

    async def window_geometry(
        self, *, title: str = "", foreground: bool = False, at: tuple[int, int] | None = None,
        handle: int | None = None,
    ) -> WindowGeometry | None:
        """The geometry of the target window, or None when it cannot be found.

        ``title`` finds a window whose title contains the substring;
        ``foreground`` reads the focused window; ``at`` reads the window under a
        screen point; ``handle`` reads a specific window by its hwnd. All four
        report the SAME fields so a caller can validate identity before sending
        input.
        """
        if title:
            result = await self.send_input(f"find {title}")
        elif foreground:
            result = await self.send_input("foreground")
        elif at is not None:
            result = await self.send_input(f"at {at[0]} {at[1]}")
        elif handle is not None:
            result = await self.send_input(f"describe {handle}")
        else:
            raise ValueError(
                "window_geometry needs a title, foreground=True, an at=(x, y) point, or a handle"
            )
        if not result.ok or not result.detail or result.detail in ("none", "nowin"):
            return None
        return _parse_geometry(result.detail)

    async def cursor(self) -> tuple[int, int] | None:
        """The cursor's current physical-pixel position, or None if unreadable.

        Read-only: used to verify a move landed where it was aimed (and to
        restore a position) without sending any input of its own.
        """
        result = await self.send_input("cursor")
        if not result.ok or not result.detail or result.detail == "none":
            return None
        left, _, right = result.detail.partition(",")
        try:
            return int(left), int(right)
        except ValueError:
            return None

    async def _exchange(self, command: str, timeout: float) -> tuple[str | None, str]:
        """Send one command and read its reply, restarting the worker once.

        Shared by capture and input so both get the same resilience: a worker
        that dies mid-command is restarted once, transparently; a worker that
        cannot start leaves the caller to fall back, reporting *why*.
        """
        async with self._lock:
            if self._desynced:
                # A previous exchange was cancelled with its reply still on the
                # pipe; restart before trusting any answer from this worker.
                self._desynced = False
                await self._stop_process()
            if not await self.ensure_started():
                return None, self.last_error or "capture worker unavailable"
            reply = await self._request_guarded(command, timeout)
            if reply is None:
                await self._stop_process()
                if not await self.ensure_started():
                    return None, self.last_error or "capture worker unavailable"
                reply = await self._request_guarded(command, timeout)
            if reply is None:
                await self._stop_process()
                return None, "the capture worker stopped responding"
            return reply, ""

    async def stats(self) -> WorkerStats:
        """Ask the worker for its working set and CPU time (for measurement)."""
        async with self._lock:
            if not await self.ensure_started():
                return WorkerStats()
            reply = await self._request_guarded("stats", self.frame_timeout)
        parts = (reply or "").split(" ")
        if len(parts) < 3 or parts[0] != "stats":
            return WorkerStats()
        try:
            return WorkerStats(working_set_bytes=int(parts[1]), cpu_ms=int(parts[2]))
        except ValueError:
            return WorkerStats()

    async def aclose(self) -> None:
        """Shut the worker down: polite request, then terminate, then kill."""
        self._closed = True
        await self._stop_process()

    # -- internals ----------------------------------------------------------

    async def _request_guarded(self, line: str, timeout: float) -> str | None:
        """One request that marks the worker for restart if it is cancelled.

        The worker speaks a strict request/reply protocol over one pipe, so a
        cancellation mid-request leaves an unread reply behind: the NEXT command
        would read that reply as its own answer and silently act on the wrong
        window (``find`` returning the previous query's result). The flag makes
        the next exchange restart the worker first. No await may happen in the
        cancellation path — that path is already unwinding.
        """
        try:
            return await self._request(line, timeout)
        except asyncio.CancelledError:
            self._desynced = True
            raise

    async def _request(self, line: str, timeout: float) -> str | None:
        process = self._process
        if process is None or process.stdin is None:
            return None
        try:
            process.stdin.write((line + "\n").encode("utf-8", errors="replace"))
            await process.stdin.drain()
        except Exception as exc:
            self.last_error = f"the capture worker's input closed: {type(exc).__name__}: {exc}"
            return None
        return await self._read_line(timeout)

    async def _read_line(self, timeout: float) -> str | None:
        process = self._process
        if process is None or process.stdout is None:
            return None
        try:
            raw = await asyncio.wait_for(process.stdout.readline(), timeout)
        except TimeoutError:
            self.last_error = f"the capture worker did not answer in {timeout:.0f}s"
            # The worker may still answer the command we stopped waiting for, and
            # that late line would then be consumed as the NEXT command's reply
            # (``find`` returning the previous query's window, or a geometry line
            # arriving where a frame was expected). Mark the pipe for a restart.
            self._desynced = True
            return None
        except Exception as exc:
            self.last_error = f"reading the capture worker failed: {type(exc).__name__}: {exc}"
            return None
        if not raw:
            self.last_error = "the capture worker exited"
            return None
        return raw.decode("utf-8", errors="replace").strip()

    async def _drain_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        try:
            while True:
                raw = await process.stderr.readline()
                if not raw:
                    break
                text = raw.decode("utf-8", errors="replace").rstrip()
                if text:
                    self._stderr_tail.append(text)
                    if len(self._stderr_tail) > self.stderr_tail_lines:
                        self._stderr_tail.popleft()
                    self.last_stderr = " | ".join(self._stderr_tail)[-2000:]
        except Exception:  # pragma: no cover - stderr draining is best effort
            return

    async def _stop_process(self) -> None:
        process = self._process
        self._process = None
        task = self._stderr_task
        self._stderr_task = None
        if process is None:
            return
        if process.returncode is None and process.stdin is not None:
            with contextlib.suppress(Exception):
                process.stdin.write(b"exit\n")
                await process.stdin.drain()
                process.stdin.close()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), self.shutdown_grace)
        if process.returncode is None:
            with contextlib.suppress(TimeoutError, ProcessLookupError):
                process.terminate()
                await asyncio.wait_for(process.wait(), self.shutdown_grace)
            if process.returncode is None:
                with contextlib.suppress(Exception):  # pragma: no cover - OS owns the rest
                    process.kill()
                    await asyncio.wait_for(process.wait(), self.shutdown_grace)
        if task is not None:
            task.cancel()


def _parse_size(text: str) -> tuple[int, int]:
    left, _, right = text.partition("x")
    try:
        return int(left), int(right)
    except ValueError:
        return 0, 0


def _parse_geometry(text: str) -> WindowGeometry | None:
    """Parse the worker's geometry line.

    Current shape: ``hwnd|bounds|client|pid|scale|fg|iconic|visible|title``.
    The older seven-field shape (``...|fg|title``) is still accepted so a
    caller using a separately supplied worker is not broken by the added
    fields; in that shape minimization is inferred from the off-screen
    placeholder bounds by the caller, while ``visible`` defaults to true.

    Returns None when the payload is not the shape we expect — a geometry that
    could not be parsed is "unknown", never "zero-sized" (a zero-sized window
    would make every point fall outside it and every input refused for the
    wrong reason).
    """
    parts = text.split("|", 8)
    if len(parts) == 7:
        title = parts[6]
        minimized = False
        visible = True
    elif len(parts) >= 9:
        title = parts[8]
        minimized = parts[6] == "1"
        visible = parts[7] == "1"
    else:
        return None
    try:
        handle = int(parts[0])
        bounds = tuple(int(value) for value in parts[1].split(","))
        client = tuple(int(value) for value in parts[2].split(","))
        pid = int(parts[3])
        scale = float(parts[4])
    except ValueError:
        return None
    if len(bounds) != 4 or len(client) != 4:
        return None
    return WindowGeometry(
        handle=handle,
        title=title,
        bounds=(bounds[0], bounds[1], bounds[2], bounds[3]),
        client=(client[0], client[1], client[2], client[3]),
        pid=pid,
        scale=scale,
        foreground=parts[5] == "1",
        minimized=minimized,
        visible=visible,
    )
