"""The NovaForge overlay: the supervisor's panel ON TOP of a real game.

The game is not embedded — embedding a DirectX/Unity window is neither reliable
nor cheap — so this window is the workspace's visible half instead: a small,
translucent, always-on-top panel that shows what the agent is doing and lets the
operator steer it WITHOUT taking their eyes off the game.

It is deliberately thin. Every button calls the same :class:`PilotEngine` the
headless runner uses, so the panel cannot claim a state the engine is not in.

What it offers
--------------
* live status: game, session state, objective, the decision being made, the
  action actually sent, and the VERIFIED outcome (or why it could not be
  verified);
* a natural-language box: type what you want, press Send, and the instruction
  becomes a plan the engine executes;
* agent controls: Start, Pause/Resume, Stop, Takeover, Hand back;
* teaching: while you play, the panel records what you do; when you hand back,
  those actions become a replayable skill for the objective you named;
* presentation: opacity slider, collapse, and global hotkeys
  ``Ctrl+Alt+O`` (show/hide the panel) and ``Ctrl+Alt+P`` (pause/resume), so the
  full game view is one keystroke away.

Run it with ``python -m novacontrol.novaforge.overlay``.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import os
import sys
from collections import deque
import threading
import time
import tkinter as tk
from ctypes import wintypes
from pathlib import Path
from tkinter import ttk
from typing import Any, Callable, Sequence

# The pilot is deterministic; the model probe at boot is not needed and must not
# add seconds (or model residency) to opening a panel.
os.environ.setdefault("NOVACONTROL_DISABLE_OLLAMA", "1")

from novacontrol.novaforge.pilot import (  # noqa: E402 - after the env gate
    PROFILES,
    LearnedSkills,
    PilotEngine,
    Step,
)

#: Overlay palette. One place, so the panel reads as one surface.
BG = "#0b1016"
BG_ALT = "#121a23"
FG = "#e6edf3"
MUTED = "#8b98a5"
ACCENT = "#3fb950"
WARN = "#d29922"
BAD = "#f85149"
BLUE = "#58a6ff"

HOTKEY_TOGGLE = 1
HOTKEY_PAUSE = 2

#: Winsock-free Win32 bits the panel needs to stay visible above a game.
GWL_EXSTYLE = -20
GWL_STYLE = -16
WS_CAPTION = 0x00C00000
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
HWND_TOPMOST = -1
VK_MENU = 0x12
VK_RETURN = 0x0D
KEYEVENTF_KEYUP = 0x0002


def _top_level(hwnd: int) -> int:
    """The top-level window above one of Tk's child windows."""
    user32 = ctypes.windll.user32
    parent = user32.GetParent(hwnd)
    return parent or hwnd


def _window_rect(hwnd: int) -> tuple[int, int, int, int]:
    user32 = ctypes.windll.user32
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top


def _is_borderless_fullscreen(hwnd: int) -> bool:
    """True when a window is a captionless pane covering the whole monitor.

    That is the state in which Windows draws the game above a topmost overlay:
    an exclusive (or borderless-flip) fullscreen surface owns the display, so a
    second window cannot appear over it no matter how often it is raised.
    """
    if not hwnd:
        return False
    user32 = ctypes.windll.user32
    style = user32.GetWindowLongW(hwnd, GWL_STYLE) & 0xFFFFFFFF
    left, top, width, height = _window_rect(hwnd)
    screen_w = user32.GetSystemMetrics(0)
    screen_h = user32.GetSystemMetrics(1)
    covers = width >= screen_w - 2 and height >= screen_h - 2
    return bool(covers and not (style & WS_CAPTION))

#: Where the panel remembers the operator's own presentation choices. Opacity is
#: a preference about THEIR screen, so it survives a restart instead of snapping
#: back to a default that covers the game again.
PREFS_PATH = Path("data/novaforge_overlay.json")


def _load_prefs() -> dict[str, Any]:
    try:
        data = json.loads(PREFS_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - an unreadable preference is the default
        return {}
    return data if isinstance(data, dict) else {}


def _ensure_tcl_paths() -> None:
    """Let Tk start from a virtualenv that has no Tcl of its own.

    ``_tkinter`` resolves ``init.tcl`` relative to ``sys.prefix``. Inside a venv
    that is ``.venv``, which has no ``lib/tcl8.6``, so ``python -m
    novacontrol.novaforge.overlay`` died with "Can't find a usable init.tcl"
    while the same code worked under the base interpreter. Pointing the two
    library variables at the BASE installation's Tcl is the documented remedy;
    the variables are only set when they are absent, so a correctly configured
    interpreter keeps whatever it already chose.
    """
    if os.environ.get("TCL_LIBRARY") and os.environ.get("TK_LIBRARY"):
        return
    base = Path(getattr(sys, "base_prefix", sys.prefix))
    for tcl in (base / "tcl" / "tcl8.6", base / "lib" / "tcl8.6", base / "tcl" / "tcl9.0"):
        if not (tcl / "init.tcl").exists():
            continue
        os.environ.setdefault("TCL_LIBRARY", str(tcl))
        tk_dir = tcl.parent / tcl.name.replace("tcl", "tk", 1)
        if (tk_dir / "tk.tcl").exists():
            os.environ.setdefault("TK_LIBRARY", str(tk_dir))
        return


def _save_prefs(prefs: dict[str, Any]) -> None:
    try:
        PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
        PREFS_PATH.write_text(json.dumps(prefs, indent=1), encoding="utf-8")
    except Exception:  # noqa: BLE001 - presentation must never fail a session
        pass


# ── global hotkeys ─────────────────────────────────────────────────────────


class Hotkeys:
    """Two system-wide hotkeys, registered on their own thread.

    Tk's ``bind`` only fires while the panel has focus, which is exactly when a
    supervisor does NOT need a shortcut — the whole point is to reach the panel
    while playing the game. ``RegisterHotKey`` with a message loop is the OS
    mechanism for that.
    """

    MOD_ALT = 0x0001
    MOD_CONTROL = 0x0002
    WM_HOTKEY = 0x0312

    def __init__(self, on_hotkey: Callable[[int], None]) -> None:
        self._on_hotkey = on_hotkey
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self.error = ""

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="novaforge-hotkeys", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            user32 = ctypes.windll.user32
        except AttributeError:  # pragma: no cover - not Windows
            self.error = "global hotkeys are only available on Windows"
            return
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        combos = {
            HOTKEY_TOGGLE: (self.MOD_CONTROL | self.MOD_ALT, 0x4F),  # Ctrl+Alt+O
            HOTKEY_PAUSE: (self.MOD_CONTROL | self.MOD_ALT, 0x50),  # Ctrl+Alt+P
        }
        registered: list[int] = []
        for ident, (mods, vk) in combos.items():
            if user32.RegisterHotKey(None, ident, mods, vk):
                registered.append(ident)
        if not registered:
            self.error = "another program already owns Ctrl+Alt+O / Ctrl+Alt+P"
            return
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == self.WM_HOTKEY:
                try:
                    self._on_hotkey(int(msg.wParam))
                except Exception:  # noqa: BLE001 - a callback cannot kill the loop
                    pass
        for ident in registered:
            user32.UnregisterHotKey(None, ident)


# ── human input recorder ───────────────────────────────────────────────────


#: Virtual keys the recorder understands. A key that is not named here is not
#: recorded: a skill is a replayable action, and an unnamed key cannot be replayed.
VK_NAMES: dict[int, str] = {
    0x57: "w", 0x41: "a", 0x53: "s", 0x44: "d", 0x51: "q", 0x45: "e",
    0x52: "r", 0x46: "f", 0x47: "g", 0x43: "c", 0x56: "v", 0x20: "space",
    0x0D: "enter", 0x1B: "escape", 0x10: "shift", 0x11: "ctrl", 0x09: "tab",
    0x26: "up", 0x28: "down", 0x25: "left", 0x27: "right",
    **{0x31 + index: str(index + 1) for index in range(9)},
}


class HumanRecorder:
    """Records what the operator does to the game while teaching.

    Polls the OS instead of installing a hook: a low-level keyboard hook is a
    global interception point, far more invasive than a teaching session needs,
    and a 30 Hz poll is enough to capture discrete menu clicks and key presses.
    Only input while the GAME is in front is recorded, so typing an objective in
    the overlay is never mistaken for gameplay.
    """

    def __init__(self, hwnd: int) -> None:
        self.hwnd = hwnd
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.actions: list[dict[str, Any]] = []
        self.error = ""

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="novaforge-recorder", daemon=True)
        self._thread.start()

    def stop(self) -> list[dict[str, Any]]:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        return list(self.actions)

    def _run(self) -> None:
        try:
            user32 = ctypes.windll.user32
        except AttributeError:  # pragma: no cover - not Windows
            self.error = "recording is only available on Windows"
            return
        point = wintypes.POINT()
        down: dict[int, float] = {}
        last = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            if user32.GetForegroundWindow() != self.hwnd:
                time.sleep(0.03)
                continue
            # -- mouse --------------------------------------------------
            for vk, label in ((0x01, "left"), (0x02, "right")):
                pressed = bool(user32.GetAsyncKeyState(vk) & 0x8000)
                was = vk in down
                if pressed and not was:
                    down[vk] = now
                    if user32.GetCursorPos(ctypes.byref(point)):
                        client = wintypes.POINT(point.x, point.y)
                        user32.ScreenToClient(self.hwnd, ctypes.byref(client))
                        self.actions.append(
                            {
                                "kind": "click_at",
                                "x": int(client.x),
                                "y": int(client.y),
                                "button": label,
                                "at": round(now - last, 2),
                            }
                        )
                elif not pressed and was:
                    down.pop(vk, None)
            # -- keys ---------------------------------------------------
            for vk, name in VK_NAMES.items():
                pressed = bool(user32.GetAsyncKeyState(vk) & 0x8000)
                was = vk in down
                if pressed and not was:
                    down[vk] = now
                elif not pressed and was:
                    began = down.pop(vk)
                    held = now - began
                    # A held movement key is a HOLD, a tapped menu key is a KEY.
                    self.actions.append(
                        {
                            "kind": "hold" if held >= 0.45 else "key",
                            "value": name,
                            "duration_ms": int(max(0.05, held) * 1000),
                            "at": round(now - last, 2),
                        }
                    )
                    last = now
            time.sleep(0.03)


# ── the panel ──────────────────────────────────────────────────────────────


class OverlayApp:
    """Tk panel + one asyncio thread owning the application and the pilot."""

    def __init__(
        self,
        *,
        initial_game: str = "",
        initial_task: str = "",
        opacity: float = 0.0,
        autolaunch: bool = False,
    ) -> None:
        _ensure_tcl_paths()
        self.prefs = _load_prefs()
        self._initial_game = initial_game.strip()
        self._initial_task = initial_task.strip()
        self._autolaunch = autolaunch
        self.root = tk.Tk()
        self.root.title("NovaForge Pilot")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        alpha = opacity or float(self.prefs.get("alpha") or 0.86)
        self.root.attributes("-alpha", max(0.2, min(1.0, alpha)))
        self.root.configure(bg=BG)
        self.root.geometry(str(self.prefs.get("geometry") or "+40+40"))
        self.loop = asyncio.new_event_loop()
        self.app: Any = None
        self.engine: PilotEngine | None = None
        self.ready = threading.Event()
        self.recorder: HumanRecorder | None = None
        self.teach_objective = ""
        self.teach_signature: tuple[str, ...] = ()
        self._body_visible = True
        self._hidden = False
        self._pause_requested = False
        self._hotkey_queue: deque[int] = deque()
        self._build()
        # A Tk topmost flag is a request, not a guarantee: a game that takes the
        # display drops the panel behind it and never gives it back. Marking the
        # window as a no-activate tool window and re-asserting topmost on a timer
        # is how an overlay stays over a fullscreen game without stealing focus.
        self._hwnd = _top_level(self.root.winfo_id())
        self._make_overlay_window()
        self.hotkeys = Hotkeys(self._queue_hotkey)
        self.hotkeys.start()
        self.root.after(1200, self._report_hotkeys)
        self.root.after(120, self._poll_hotkeys)
        self.root.after(700, self._keep_on_top)
        threading.Thread(target=self._boot, name="novaforge-pilot-loop", daemon=True).start()
        self.root.after(400, self._refresh)

    # -- construction -------------------------------------------------------

    def _build(self) -> None:
        header = tk.Frame(self.root, bg=BG_ALT, cursor="fleur")
        header.pack(fill="x")
        tk.Label(header, text="◆ NOVAFORGE PILOT", bg=BG_ALT, fg=BLUE,
                 font=("Segoe UI Semibold", 9)).pack(side="left", padx=8, pady=5)
        self.state_label = tk.Label(header, text="starting…", bg=BG_ALT, fg=MUTED,
                                    font=("Consolas", 8))
        self.state_label.pack(side="right", padx=8)
        for widget in (header, self.state_label):
            widget.bind("<Button-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)
            widget.bind("<ButtonRelease-1>", self._drag_end)

        self.body = tk.Frame(self.root, bg=BG, padx=10, pady=8)
        self.body.pack(fill="both", expand=True)

        row = tk.Frame(self.body, bg=BG)
        row.pack(fill="x")
        tk.Label(row, text="Game", bg=BG, fg=MUTED, font=("Segoe UI", 8)).pack(side="left")
        self.game_var = tk.StringVar(value=PROFILES[0].window_title)
        self.game_box = ttk.Combobox(
            row, textvariable=self.game_var, values=[p.window_title for p in PROFILES],
            state="readonly", width=24, font=("Segoe UI", 8),
        )
        self.game_box.pack(side="left", padx=(6, 0))

        self.body.pack_propagate(True)
        self.info = {}
        for key, label in (
            ("objective", "Objective"),
            ("decision", "Decision"),
            ("action", "Action"),
            ("outcome", "Outcome"),
            ("verification", "Verified"),
            ("obstacle", "Obstacle"),
        ):
            frame = tk.Frame(self.body, bg=BG)
            frame.pack(fill="x", pady=(5, 0))
            tk.Label(frame, text=label.upper(), bg=BG, fg=MUTED,
                     font=("Segoe UI", 7), width=10, anchor="w").pack(side="left", anchor="n")
            value = tk.Label(
                frame, text="—", bg=BG, fg=FG, font=("Segoe UI", 8),
                wraplength=330, justify="left", anchor="w",
            )
            value.pack(side="left", fill="x", expand=True)
            self.info[key] = value

        # The game's own controls. The operator asked for the overlay to carry
        # them so the panel describes what it can send without a second window.
        legend = tk.LabelFrame(
            self.body, text=" CONTROLS ", bg=BG, fg=MUTED, bd=1, relief="flat",
            font=("Segoe UI", 7), labelanchor="nw",
        )
        legend.pack(fill="x", pady=(8, 0))
        self.controls_grid = tk.Frame(legend, bg=BG)
        self.controls_grid.pack(fill="x", padx=4, pady=3)
        self._control_labels: list[tk.Label] = []

        instr = tk.Frame(self.body, bg=BG)
        instr.pack(fill="x", pady=(8, 0))
        self.instruction = tk.Entry(
            instr, bg=BG_ALT, fg=FG, insertbackground=FG, relief="flat",
            font=("Segoe UI", 9),
        )
        self.instruction.pack(side="left", fill="x", expand=True, ipady=4)
        self.instruction.bind("<Return>", lambda _event: self._send())
        tk.Button(instr, text="Send", command=self._send, bg=BLUE, fg="#04121f",
                  relief="flat", font=("Segoe UI Semibold", 8), padx=8).pack(side="left", padx=(4, 0))

        tasks = tk.Frame(self.body, bg=BG)
        tasks.pack(fill="x", pady=(6, 0))
        tk.Label(tasks, text="Task", bg=BG, fg=MUTED, font=("Segoe UI", 8)).pack(side="left")
        self.task_var = tk.StringVar(value="")
        self.task_box = ttk.Combobox(tasks, textvariable=self.task_var, state="readonly",
                                     width=26, font=("Segoe UI", 8))
        self.task_box.pack(side="left", padx=(6, 4))
        tk.Button(tasks, text="Run task", command=self._run_task, bg=BG_ALT, fg=FG,
                  relief="flat", font=("Segoe UI", 8), padx=6).pack(side="left")
        self.game_box.bind("<<ComboboxSelected>>", lambda _event: self._sync_tasks())

        controls = tk.Frame(self.body, bg=BG)
        controls.pack(fill="x", pady=(8, 0))
        self.buttons: dict[str, tk.Button] = {}
        for name, label, colour, command in (
            ("start", "Start", ACCENT, self._start),
            ("pause", "Pause", WARN, self._toggle_pause),
            ("stop", "Stop", BAD, self._stop),
            ("takeover", "Take over", BLUE, self._takeover),
            ("handback", "Hand back", ACCENT, self._hand_back),
            ("teach", "Teach", BLUE, self._start_teach),
            ("teach_stop", "Done teaching", WARN, self._stop_teach),
        ):
            button = tk.Button(controls, text=label, command=command, bg=colour,
                               fg="#04121f", relief="flat",
                               font=("Segoe UI Semibold", 8), padx=6, pady=3)
            button.pack(side="left", padx=(0, 4))
            self.buttons[name] = button

        foot = tk.Frame(self.body, bg=BG)
        foot.pack(fill="x", pady=(8, 0))
        tk.Label(foot, text="Opacity", bg=BG, fg=MUTED,
                 font=("Segoe UI", 8)).pack(side="left")
        self.alpha = tk.DoubleVar(value=float(self.prefs.get("alpha") or 0.86))
        tk.Scale(foot, from_=0.2, to=1.0, resolution=0.02, orient="horizontal",
                 variable=self.alpha, command=self._set_alpha, bg=BG, fg=FG,
                 highlightthickness=0, troughcolor=BG_ALT, length=150,
                 showvalue=False).pack(side="left", padx=4)
        self.learned_label = tk.Label(foot, text="learned 0", bg=BG, fg=MUTED,
                                      font=("Consolas", 8))
        self.learned_label.pack(side="right")
        self._sync_tasks()
        self._sync_controls()

        hint = tk.Label(self.body, text="Ctrl+Alt+O show/hide   Ctrl+Alt+P pause/resume",
                        bg=BG, fg=MUTED, font=("Consolas", 7), anchor="w", justify="left",
                        wraplength=420)
        hint.pack(fill="x", pady=(6, 0))
        self.hint = hint
        # Tk-local bindings are the FALLBACK, not the mechanism: a global
        # RegisterHotKey can lose to another program (measured: two stale
        # overlay instances owned the chords, the third registered nothing and
        # said so only in a field nobody read). These fire while the panel has
        # focus, so the shortcuts still exist when the global ones are taken.
        def _bind_hotkey(binding: str, ident: int) -> None:
            """One focus-local Tk binding, with the id captured explicitly.

            A default-argument lambda here could not be typed; a closure binds
            the same value and reads as what it is.
            """

            def handler(_event: object) -> None:
                self._hotkey(ident)

            self.root.bind_all(binding, handler)

        for sequence, ident in (("<Control-Alt-o>", HOTKEY_TOGGLE),
                                ("<Control-Alt-p>", HOTKEY_PAUSE)):
            _bind_hotkey(sequence, ident)

    def _make_overlay_window(self) -> None:
        """Raise the panel into the topmost band WITHOUT touching its styles.

        Editing WS_EX_* behind Tk's back made the window a hit-testable but blank
        pane: the panel's own pixels stopped being composed while WindowFromPoint
        still resolved to it. Tk's ``overrideredirect`` + ``-topmost`` already
        produce a working layered window, so only the z-order is re-asserted here.
        """
        try:
            ctypes.windll.user32.SetWindowPos(
                self._hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
            )
        except Exception:  # noqa: BLE001 - not Windows, Tk is enough there
            pass

    def _report_hotkeys(self) -> None:
        """Say on the panel when the global shortcuts could not be registered."""
        if self.hotkeys.error:
            self.hint.configure(
                text=(
                    f"Global hotkeys unavailable ({self.hotkeys.error}). "
                    "Ctrl+Alt+O / Ctrl+Alt+P still work while this panel has focus."
                ),
                fg=WARN,
            )

    def _keep_on_top(self) -> None:
        """Re-assert topmost every second; a game can knock the panel down."""
        try:
            if not self._hidden:
                ctypes.windll.user32.SetWindowPos(
                    self._hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                    SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
                )
                self._hwnd = _top_level(self.root.winfo_id())
        except Exception:  # noqa: BLE001
            pass
        finally:
            self.root.after(1000, self._keep_on_top)

    def _game_hwnd(self, title: str) -> int:
        """The game's own top-level window, by title (0 when it is not up)."""
        try:
            user32 = ctypes.windll.user32
        except AttributeError:  # pragma: no cover - not Windows
            return 0
        found: list[int] = []

        # Same shape as embedding.py: build the callback type and CALL it, rather
        # than using it as a decorator (a decorator on a typed function is
        # untyped to mypy).
        callback_type = getattr(ctypes, "WINFUNCTYPE")(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
        )

        def visit(hwnd: int, _lparam: int) -> bool:
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            if title.lower() in buffer.value.lower():
                found.append(hwnd)
            return True

        user32.EnumWindows(callback_type(visit), 0)
        return found[0] if found else 0

    def _release_fullscreen(self, title: str) -> str:
        """Ask the game to leave exclusive fullscreen, so the panel can be seen.

        Returns what was done, for the panel to show. Alt+Enter is the standard
        toggle every Unity title here honours; when the window is already
        captioned there is nothing to do.
        """
        hwnd = self._game_hwnd(title)
        if not hwnd or not _is_borderless_fullscreen(hwnd):
            return ""
        try:
            user32 = ctypes.windll.user32
            user32.SetForegroundWindow(hwnd)
            time.sleep(0.4)
            user32.keybd_event(VK_MENU, 0, 0, 0)
            user32.keybd_event(VK_RETURN, 0, 0, 0)
            time.sleep(0.08)
            user32.keybd_event(VK_RETURN, 0, KEYEVENTF_KEYUP, 0)
            user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
            time.sleep(1.6)
        except Exception:  # noqa: BLE001 - a refused toggle is reported below
            return ""
        if _is_borderless_fullscreen(hwnd):
            return (
                "The game is exclusive fullscreen and refused Alt+Enter; an "
                "overlay cannot be drawn over that surface."
            )
        return "Game switched to a windowed display so the overlay stays visible."

    # -- backend ------------------------------------------------------------

    def _boot(self) -> None:
        asyncio.set_event_loop(self.loop)
        try:
            from novacontrol.application import NovaControlApplication

            self.app = NovaControlApplication(data_dir="data")
            self.loop.run_until_complete(self.app.start())
            self.engine = PilotEngine(self.app, learned_path="data/novaforge_learned.json")
        except Exception as exc:  # noqa: BLE001 - a boot failure is shown, not hidden
            self._fatal = f"boot failed: {type(exc).__name__}: {exc}"
            self.ready.set()
            return
        self.ready.set()
        if self._autolaunch and self._initial_game:
            # Started BY the operator with a game named: bring it up before the
            # panel accepts work, so "Open NovaForge and select a game" is one
            # action rather than an empty panel over the desktop.
            try:
                self.loop.run_until_complete(self._launch_initial())
            except Exception as exc:  # noqa: BLE001 - shown in the panel
                self.engine.obstacle = f"launch failed: {type(exc).__name__}: {exc}"
        self.loop.run_forever()

    async def _launch_initial(self) -> None:
        assert self.engine is not None
        await self.engine.launch(self._initial_game)
        if self.engine.session_id:
            note = await asyncio.to_thread(self._release_fullscreen, self._initial_game)
            if note:
                self.engine.obstacle = note
            if self._initial_task:
                await self.engine.run_task(self._initial_task)

    def _submit(self, coro: Any) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    # -- UI actions ---------------------------------------------------------

    def _sync_tasks(self) -> None:
        game = self.game_var.get()
        profile = next((row for row in PROFILES if row.window_title == game), None)
        names = [task.name for task in profile.tasks] if profile else []
        self.task_box.configure(values=names)
        self.task_var.set(names[0] if names else "")
        self._sync_controls()

    def _sync_controls(self) -> None:
        """Draw the selected game's controls in two columns."""
        for label in self._control_labels:
            label.destroy()
        self._control_labels = []
        profile = next((row for row in PROFILES if row.window_title == self.game_var.get()), None)
        pairs = list(profile.controls) if profile else []
        if not pairs:
            pairs = [("(no profile)", "")]
        for index, (action, keys) in enumerate(pairs):
            row, column = divmod(index, 2)
            name = tk.Label(self.controls_grid, text=action, bg=BG, fg=MUTED,
                            font=("Segoe UI", 7), anchor="w", width=12)
            name.grid(row=row, column=column * 2, sticky="w", padx=(0, 2))
            combo = tk.Label(self.controls_grid, text=keys, bg=BG, fg=FG,
                             font=("Consolas", 8), anchor="w")
            combo.grid(row=row, column=column * 2 + 1, sticky="w", padx=(0, 14))
            self._control_labels.extend((name, combo))

    def _set_alpha(self, value: str) -> None:
        try:
            alpha = max(0.2, min(1.0, float(value)))
            self.root.attributes("-alpha", alpha)
            self.prefs["alpha"] = round(alpha, 2)
            _save_prefs(self.prefs)
        except Exception:  # noqa: BLE001
            pass

    def _remember_geometry(self) -> None:
        self.prefs["geometry"] = self.root.geometry()
        _save_prefs(self.prefs)

    def _start(self) -> None:
        if self.engine is None:
            return
        game = self.game_var.get()
        task = self.task_var.get()

        async def run() -> None:
            assert self.engine is not None
            if game and (not self.engine.title or self.engine.title != game):
                await self.engine.launch(game)
            if self.engine.session_id:
                note = await asyncio.to_thread(self._release_fullscreen, game)
                if note:
                    self.engine.obstacle = note
                if task:
                    await self.engine.run_task(task)

        self._submit(run())

    def _run_task(self) -> None:
        if self.engine is None or not self.task_var.get():
            return
        self._submit(self.engine.run_task(self.task_var.get()))

    def _toggle_pause(self) -> None:
        if self.engine is None:
            return
        self._pause_requested = not self._pause_requested
        if self._pause_requested:
            self.engine.pause()
        else:
            self.engine.resume()

    def _stop(self) -> None:
        if self.engine is None:
            return
        if self.recorder is not None:
            self._stop_teach()
        self._submit(self.engine.stop())

    def _takeover(self) -> None:
        if self.engine is not None:
            self.engine.take_over()

    def _hand_back(self) -> None:
        if self.engine is not None:
            self._submit(self.engine.hand_back())

    def _start_teach(self) -> None:
        """Record the operator playing, so the demonstration can be replayed."""
        if self.engine is None or not self.engine.session_id:
            self.info["obstacle"].configure(text="Start the game first, then teach.")
            return
        self.teach_objective = (
            self.instruction.get().strip() or self.task_var.get() or self.engine.objective
        )
        self.engine.take_over()

        async def begin() -> None:
            assert self.engine is not None
            reading = await self.engine.observe()
            self.teach_signature = reading.signature()
            handle = int(reading.handle or 0)
            self.recorder = HumanRecorder(handle)
            self.recorder.start()
            self.info["obstacle"].configure(
                text=f"Teaching {self.teach_objective!r}: play now, then press Done teaching."
            )

        self._submit(begin())

    def _stop_teach(self) -> None:
        recorder = self.recorder
        self.recorder = None
        if recorder is None or self.engine is None:
            return
        actions = recorder.stop()

        async def finish() -> None:
            assert self.engine is not None
            steps: list[Step] = []
            for action in actions:
                if action.get("kind") == "click_at":
                    steps.append(Step(kind="click_at", x=int(action["x"]), y=int(action["y"])))
                elif action.get("kind") == "hold":
                    steps.append(Step(kind="hold", value=str(action["value"]),
                                      duration_ms=int(action.get("duration_ms") or 300)))
                elif action.get("kind") == "key":
                    steps.append(Step(kind="key", value=str(action["value"])))
            if steps and self.engine.profile is not None:
                self.engine.learned.teach(
                    game=self.engine.profile.name,
                    objective=self.teach_objective,
                    signature=self.teach_signature,
                    steps=steps,
                )
                self.info["obstacle"].configure(
                    text=f"Learned {len(steps)} step(s) for {self.teach_objective!r}."
                )
            else:
                self.info["obstacle"].configure(text="Nothing was recorded while you played.")
            await self.engine.hand_back()

        self._submit(finish())

    def _send(self) -> None:
        text = self.instruction.get().strip()
        if not text or self.engine is None:
            return
        self.instruction.delete(0, "end")
        self._submit(self._run_objective(text))

    async def _run_objective(self, text: str) -> None:
        assert self.engine is not None
        if not self.engine.session_id:
            await self.engine.launch(self.game_var.get())
        await self.engine.run_objective(text)

    # -- hotkeys and drag ---------------------------------------------------

    def _queue_hotkey(self, ident: int) -> None:
        """Called on the HOTKEY thread: record only, never touch Tk from here.

        Tk is single-threaded. The first version called ``root.after`` straight
        from the Windows message loop; the chord was received and registered, yet
        nothing moved because the cross-thread call was dropped. A plain deque
        append is safe, and the Tk thread picks it up on its own timer.
        """
        self._hotkey_queue.append(int(ident))

    def _poll_hotkeys(self) -> None:
        try:
            while self._hotkey_queue:
                self._hotkey(self._hotkey_queue.popleft())
        except Exception:  # noqa: BLE001 - a hotkey cannot kill the panel
            pass
        finally:
            self.root.after(120, self._poll_hotkeys)

    def _hotkey(self, ident: int) -> None:
        if ident == HOTKEY_TOGGLE:
            self._toggle_visible()
        elif ident == HOTKEY_PAUSE:
            self._toggle_pause()

    def _toggle_visible(self) -> None:
        if self._hidden:
            self.root.deiconify()
            self.root.attributes("-topmost", True)
        else:
            self.root.withdraw()
        self._hidden = not self._hidden

    def _drag_start(self, event: tk.Event) -> None:
        self._drag = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())

    def _drag_move(self, event: tk.Event) -> None:
        dx, dy = self._drag
        self.root.geometry(f"+{event.x_root - dx}+{event.y_root - dy}")

    def _drag_end(self, _event: tk.Event) -> None:
        self._remember_geometry()

    # -- refresh ------------------------------------------------------------

    def _refresh(self) -> None:
        try:
            if not self.ready.is_set():
                self.state_label.configure(text="boot…", fg=WARN)
            elif self.engine is None:
                self.state_label.configure(text="UNAVAILABLE", fg=BAD)
                self.info["obstacle"].configure(
                    text=getattr(self, "_fatal", "the pilot did not start")
                )
            else:
                status = self.engine.status()
                colour = {
                    "running": ACCENT, "paused": WARN, "takeover": BLUE,
                    "failed": BAD, "stopped": BAD, "launching": WARN,
                }.get(str(status.get("state")), MUTED)
                goal = status.get("task") or status.get("state")
                self.state_label.configure(
                    text=f"{str(status.get('state','')).upper()} · {status.get('game','')} · {goal}",
                    fg=colour,
                )
                for key in ("objective", "decision", "action", "outcome",
                            "verification", "obstacle"):
                    text = str(status.get(key) or "—")
                    self.info[key].configure(
                        text=text if len(text) <= 240 else text[:237] + "…"
                    )
                self.learned_label.configure(text=f"learned {status.get('learned', 0)}")
                self.buttons["pause"].configure(
                    text="Resume" if status.get("state") == "paused" else "Pause"
                )
        finally:
            self.root.after(500, self._refresh)

    def run(self) -> None:
        self.root.mainloop()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m novacontrol.novaforge.overlay",
        description="NovaForge's supervisor panel, on top of a running game.",
    )
    parser.add_argument("--game", default="", help="game title to select (and launch with --launch)")
    parser.add_argument("--task", default="", help="task name to select") 
    parser.add_argument("--opacity", type=float, default=0.0, help="0.2-1.0 (default: last used)")
    parser.add_argument("--launch", action="store_true", help="launch the named game at start")
    args = parser.parse_args(list(argv) if argv is not None else None)
    app = OverlayApp(
        initial_game=args.game,
        initial_task=args.task,
        opacity=args.opacity,
        autolaunch=args.launch,
    )
    if args.game:
        app.game_var.set(args.game)
        app._sync_tasks()
        if args.task:
            app.task_var.set(args.task)
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
