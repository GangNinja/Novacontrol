# NovaForge gameplay acceptance harness — a DISPOSABLE window, not a game.
#
# It exists so the remediation can be proved end to end on this machine with no
# game involved.
#
# DEFAULT TASK (``-Task ready``, the NF-07/NF-08 acceptance): the window starts
# titled
#
#     NC FORGE HARNESS - idle
#
# and retitles itself to
#
#     NC FORGE HARNESS - READY
#
# the first time it receives a key press. NovaForge's objective verifier reads
# that title from the OS (not from its own action record), so a verified success
# can only happen if the injected key actually reached this window.
#
# STAGE TASK (``-Task stages``, the NF-10 multi-state task): the title names a
# stage, and the correct action CHANGES with it:
#
#     STAGE IDLE  --A-->  STAGE ARMED  --B-->  STAGE COMPLETE
#     STAGE FAULT --R-->  STAGE IDLE            (recovery)
#
# Any other gameplay key from IDLE/ARMED moves to FAULT. The state is entirely
# in the window title, so the policy and the verifier read the SAME measured
# fact; there is no hidden script the agent is really following.
#
# The window re-activates itself while it runs: injected keyboard input goes to
# the FOREGROUND window, and this harness is deliberately the foreground window
# for the duration of an acceptance run. It closes on Esc or after -Seconds.
#
# Nothing here touches a game, a save file or a network connection.

param(
  [int]$Seconds = 120,
  [switch]$KeepOpen,
  [ValidateSet('ready', 'stages')]
  [string]$Task = 'ready'
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$form = New-Object System.Windows.Forms.Form
$form.Text = if ($Task -eq 'stages') { 'NC FORGE HARNESS - STAGE IDLE' } else { 'NC FORGE HARNESS - idle' }
$form.Width = 520
$form.Height = 220
$form.StartPosition = 'CenterScreen'
$form.TopMost = $true
$form.KeyPreview = $true

$label = New-Object System.Windows.Forms.Label
$label.AutoSize = $false
$label.Dock = 'Fill'
$label.TextAlign = 'MiddleCenter'
$label.Font = New-Object System.Drawing.Font('Consolas', 14)
$label.Text = if ($Task -eq 'stages') {
  "STAGE: IDLE`r`n`r`nPress A to arm."
} else {
  "WAITING FOR INPUT`r`n`r`nNovaForge writes here through its guarded input path."
}
$form.Controls.Add($label)

# A process started from a hidden/minimized host inherits a minimized show
# state, and a minimized window has no client area to address (Windows reports
# it at -32000,-32000). The harness restores ITSELF so the geometry NovaForge
# validates is the real client rect, not a minimized placeholder.
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class NcForeground {
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
  [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, IntPtr processId);
  [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
  [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint idAttach, uint idAttachTo, bool attach);

  // Windows only lets the foreground process (or one that received the last
  // input) call SetForegroundWindow, so a harness started by a terminal is
  // refused. Attaching to the current foreground thread lifts that restriction
  // for the duration of the call; the attachment is always undone.
  public static bool Claim(IntPtr handle) {
    ShowWindow(handle, 9);   // SW_RESTORE
    bool attached = false;
    uint mine = GetCurrentThreadId();
    IntPtr current = GetForegroundWindow();
    if (current != IntPtr.Zero && current != handle) {
      uint theirs = GetWindowThreadProcessId(current, IntPtr.Zero);
      if (theirs != 0 && theirs != mine) {
        attached = AttachThreadInput(mine, theirs, true);
      }
    }
    BringWindowToTop(handle);
    bool ok = SetForegroundWindow(handle);
    if (attached) {
      uint theirs = GetWindowThreadProcessId(current, IntPtr.Zero);
      AttachThreadInput(mine, theirs, false);
    }
    return ok;
  }
}
"@

$state = @{ presses = 0; stage = 'IDLE' }

$goStage = {
  param($stage)
  $state.stage = $stage
  $form.Text = "NC FORGE HARNESS - STAGE $stage"
  $label.Text = switch ($stage) {
    'IDLE' { "STAGE: IDLE`r`n`r`nPress A to arm." }
    'ARMED' { "STAGE: ARMED`r`n`r`nPress B to complete." }
    'FAULT' { "STAGE: FAULT`r`n`r`nWrong action. Press R to recover." }
    'COMPLETE' { "STAGE: COMPLETE`r`n`r`nThe objective marker is present." }
    default { "STAGE: $stage" }
  }
}

$onKey = {
  param($sender, $eventArgs)
  $key = $eventArgs.KeyCode
  if ($key -eq [System.Windows.Forms.Keys]::Escape) {
    $form.Close()
    return
  }
  # Modifier keys arrive as their own KeyDown events; they are not actions and
  # must not be read as a wrong key in the stage task.
  if ($key -in @(
    [System.Windows.Forms.Keys]::ShiftKey,
    [System.Windows.Forms.Keys]::ControlKey,
    [System.Windows.Forms.Keys]::Menu,
    [System.Windows.Forms.Keys]::LWin,
    [System.Windows.Forms.Keys]::RWin,
    [System.Windows.Forms.Keys]::CapsLock,
    [System.Windows.Forms.Keys]::Tab
  )) { return }

  if ($Task -eq 'ready') {
    $state.presses = $state.presses + 1
    if ($state.presses -eq 1) {
      $form.Text = 'NC FORGE HARNESS - READY'
    }
    $label.Text = "RECEIVED $($state.presses) KEY(S)`r`n`r`nTitle is now: $($form.Text)"
    return
  }

  # Stage task: the current title is the state, and the action is checked
  # against it. COMPLETE is terminal until the window is restarted.
  $stage = $state.stage
  if ($stage -eq 'COMPLETE') { return }
  if ($key -eq [System.Windows.Forms.Keys]::A -and $stage -eq 'IDLE') {
    & $goStage 'ARMED'; return
  }
  if ($key -eq [System.Windows.Forms.Keys]::B -and $stage -eq 'ARMED') {
    & $goStage 'COMPLETE'; return
  }
  if ($key -eq [System.Windows.Forms.Keys]::R -and $stage -eq 'FAULT') {
    & $goStage 'IDLE'; return
  }
  & $goStage 'FAULT'
}
$form.Add_KeyDown($onKey)

# Keep this disposable window focused so injected keyboard input lands in it and
# not in whatever terminal started the probe.
$focusTimer = New-Object System.Windows.Forms.Timer
$focusTimer.Interval = 700
$focusTimer.Add_Tick({
  if ($form.Visible) {
    if ($form.WindowState -eq [System.Windows.Forms.FormWindowState]::Minimized) {
      $form.WindowState = [System.Windows.Forms.FormWindowState]::Normal
    }
    $form.Activate() | Out-Null
    [NcForeground]::Claim($form.Handle) | Out-Null
    [System.Windows.Forms.Application]::DoEvents()
  }
})
$focusTimer.Start()

$closeTimer = New-Object System.Windows.Forms.Timer
$closeTimer.Interval = [Math]::Max(1000, $Seconds * 1000)
$closeTimer.Add_Tick({ $closeTimer.Stop(); if (-not $KeepOpen) { $form.Close() } })
$closeTimer.Start()

$form.Add_Shown({
  $form.WindowState = [System.Windows.Forms.FormWindowState]::Normal
  [NcForeground]::Claim($form.Handle) | Out-Null
})

[void]$form.ShowDialog()
