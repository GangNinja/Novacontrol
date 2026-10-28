"""Coordinate-addressed desktop input, with the safety contract attached.

Phase C of the game-control audit. Two halves that must travel together:

* The SAFETY half (pure Python, testable on any OS): :class:`InputGuard`
  records every button/key pressed DOWN and guarantees the matching release UP
  on normal completion, exception, timeout, cancellation, or an explicit stop.
  This is the invariant a game dies without — a stuck Shift or a held mouse
  button left behind by a controller that crashed mid-action. Nothing sends
  input without going through the guard, so the invariant is structural rather
  than a convention someone remembers.
* The GEOMETRY half: converting a coordinate from the space it was measured in
  (a screenshot's pixels) into the space input is injected in, plus the window
  identity/foreground checks that stop a click from landing in a window that is
  no longer the one we addressed. Measuring happens in ONE DPI space — the
  capture worker is per-monitor-DPI-aware — and input is injected in that same
  space, so the conversion below exists for the one real mismatch case: a
  coordinate captured under a different scale than the target window now has.

:mod:`desktop.capture` owns the process that actually injects; this module owns
the rules about what may be injected and what must be undone.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum

__all__ = [
    "InputAbort",
    "InputGuard",
    "InputStop",
    "InputTarget",
    "MouseButton",
    "WindowGeometry",
    "client_to_screen",
    "key_release_command",
    "mouse_flags",
    "normalize_key",
    "scale_point",
    "screen_to_client",
    "validate_input_target",
    "vk_for_key",
]


class MouseButton(StrEnum):
    """The mouse buttons this build can hold down and release."""

    LEFT = "left"
    RIGHT = "right"
    MIDDLE = "middle"


#: Mouse-button flags as ``mouse_event``/``SendInput`` expects them: the DOWN
#: event is the base flag, UP is base<<1 (LEFTDOWN=0x0002 / LEFTUP=0x0004,
#: RIGHTDOWN=0x0008 / RIGHTUP=0x0010, MIDDLEDOWN=0x0020 / MIDDLEUP=0x0040).
#: Declared once so a click, a press and a release can never disagree about
#: which bit means which button edge.
_MOUSE_FLAGS: dict[MouseButton, int] = {
    MouseButton.LEFT: 0x0002,
    MouseButton.RIGHT: 0x0008,
    MouseButton.MIDDLE: 0x0020,
}


class InputAbort(RuntimeError):
    """Raised when an input operation is aborted by a stop request."""


def mouse_flags(button: MouseButton, *, down: bool) -> int:
    """The ``mouse_event`` flag for one button edge (DOWN is base, UP is base<<1)."""
    base = _MOUSE_FLAGS[button]
    return base if down else base << 1


#: Virtual-key codes for the keys a game or a UI actually needs held. Anything
#: not in this table is refused with a reason rather than silently dropped —
#: guessing a keycode is how a "press W" becomes a stray keystroke somewhere.
_VK: dict[str, int] = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "return": 0x0D, "esc": 0x1B,
    "escape": 0x1B, "space": 0x20, "pageup": 0x21, "pagedown": 0x22, "end": 0x23,
    "home": 0x24, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "insert": 0x2D, "delete": 0x2E,
    "shift": 0x10, "ctrl": 0x11, "control": 0x11, "alt": 0x12, "menu": 0x12,
    "pause": 0x13, "capslock": 0x14, "numlock": 0x90, "scrolllock": 0x91,
    # Letters and digits. The keys must be LOWERCASE: ``normalize_key``
    # lowercases every name before lookup, so an uppercase ``"A".."Z"`` table
    # left most letters (b, r, q, …) unknown to the validator while a, s, d, w
    # only worked because they were listed again below. NF-10's stage task is
    # what exposed it — the second action in the task names "b".
    **{chr(code).lower(): code for code in range(ord("A"), ord("Z") + 1)},
    **{str(digit): 0x30 + digit for digit in range(10)},
    # function keys
    **{f"f{index}": 0x6F + index for index in range(1, 25)},
    # movement keys games use as a held set
    "w": 0x57, "a": 0x41, "s": 0x53, "d": 0x44,
}


def normalize_key(name: str) -> str:
    """Canonical, lookup-ready form of a spoken or written key name.

    Strips the decorations people type (``<enter>``, ``Ctrl+Shift+S``'s first
    token is handled by the caller), lowercases, and drops ``_``/``-``/space.
    """
    cleaned = (
        name.strip()
        .strip("<>[]{}()")
        .replace("_", "")
        .replace("-", "")
        .replace(" ", "")
        .lower()
    )
    return cleaned


def vk_for_key(name: str) -> int | None:
    """The virtual-key code for a key name, or None when it is not a key we know."""
    return _VK.get(normalize_key(name))


def key_release_command(name: str) -> str | None:
    """A stable identifier for the release of ``name``, or None if unknown.

    Keys are identified by their canonical name so a press and its release can
    be matched even when the caller spelled them differently (``Ctrl`` vs
    ``control``). The identifier is what the guard records; the runner turns it
    into an actual key-up.
    """
    canonical = normalize_key(name)
    if canonical in _VK:
        return canonical
    return None


@dataclass(frozen=True, slots=True)
class InputStop:
    """A one-shot stop flag for in-flight input.

    A stop request must be able to interrupt an input sequence that is already
    running — the alternative is a controller that keeps sending keys after the
    operator said stop. The flag is checked between actions; a raised
    :class:`InputAbort` unwinds to the guard, which releases whatever is held.
    """

    _stopped: bool = False

    def stop(self) -> None:
        """Request that in-flight input stop at the next checkpoint."""
        object.__setattr__(self, "_stopped", True)

    @property
    def stopped(self) -> bool:
        return self._stopped

    def check(self, what: str = "input") -> None:
        """Raise :class:`InputAbort` when a stop has been requested."""
        if self._stopped:
            raise InputAbort(f"Input aborted by a stop request before {what}.")


@dataclass(slots=True)
class _HeldInput:
    """One pressed-down button or key, released exactly once."""

    kind: str  # "button" | "key"
    name: str
    released: bool = False
    failed: str = ""


@dataclass(slots=True)
class InputGuard:
    """Guarantees every press is matched by a release.

    The guard is the ONLY way input may be sent. It counts presses and releases
    separately so a caller (or a test) can assert they agree, and its
    ``__aexit__`` releases whatever is still held whether the body finished,
    raised, timed out, or was cancelled — a cancellation cannot skip cleanup,
    because the release happens in the guard's own exit path.
    """

    # These are injected so the guard is pure: it records and orders the
    # press/release edges, and the runner translates them into real input.
    press: Callable[[str, str], Awaitable[None]]  # (kind, name)
    release: Callable[[str, str], Awaitable[None]]  # (kind, name)
    abort: InputStop = field(default_factory=InputStop)
    _held: list[_HeldInput] = field(default_factory=list, repr=False)
    presses: int = 0
    releases: int = 0
    release_failures: list[str] = field(default_factory=list)

    async def __aenter__(self) -> InputGuard:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
        await self.release_all()
        return False  # never swallow the original exception

    @property
    def held(self) -> tuple[str, ...]:
        """Names still held down, in press order."""
        return tuple(item.name for item in self._held if not item.released)

    def held_items(self) -> tuple[tuple[str, str], ...]:
        """(kind, name) pairs still held down, in press order.

        ``held`` alone cannot tell the ``left`` ARROW key from the ``left``
        mouse button, so callers that report input state need the pair. A
        release that failed stays held: reporting it is the point.
        """
        return tuple(
            (item.kind, item.name) for item in self._held if not item.released
        )

    async def press_button(self, button: MouseButton | str) -> None:
        self.abort.check("pressing a mouse button")
        await self._press("button", _button_name(button))

    async def release_button(self, button: MouseButton | str) -> None:
        await self._release("button", _button_name(button))

    async def press_key(self, key: str) -> None:
        canonical = key_release_command(key)
        if canonical is None:
            raise ValueError(f"Unknown key: {key!r} — refusing to guess a keycode.")
        self.abort.check(f"pressing the {canonical} key")
        await self._press("key", canonical)

    async def release_key(self, key: str) -> None:
        canonical = key_release_command(key)
        if canonical is None:
            raise ValueError(f"Unknown key: {key!r} — refusing to guess a keycode.")
        await self._release("key", canonical)

    async def _press(self, kind: str, name: str) -> None:
        record = _HeldInput(kind=kind, name=name)
        self._held.append(record)
        try:
            await self.press(kind, name)
        except Exception:
            # A press that did not go out is not a press: it must not be
            # remembered as held (the guard would later "release" something
            # never pressed) nor counted against the presses==releases check.
            self._held.remove(record)
            raise
        self.presses += 1

    async def _release(self, kind: str, name: str) -> None:
        self.abort.check(f"releasing the {name} input")
        for record in reversed(self._held):
            if record.kind == kind and record.name == name and not record.released:
                try:
                    await self.release(kind, name)
                except Exception as exc:  # a failed release is recorded, not lost
                    self.release_failures.append(f"{kind}:{name}: {type(exc).__name__}: {exc}")
                    raise
                else:
                    record.released = True
                    self.releases += 1
                return
        raise ValueError(f"Cannot release {kind} {name!r}: it is not held.")

    async def release_all(self) -> None:
        """Release everything still held, newest first, and never raise.

        Called from ``__aexit__`` on EVERY exit path. A release that fails is
        recorded and the rest are still attempted: one stuck key must not stop
        the others from being freed.
        """
        for record in reversed(self._held):
            if record.released:
                continue
            try:
                await self.release(record.kind, record.name)
            except Exception as exc:
                record.failed = f"{type(exc).__name__}: {exc}"
                self.release_failures.append(f"{record.kind}:{record.name}: {record.failed}")
            else:
                record.released = True
                self.releases += 1
        # A record whose release FAILED stays held. Clearing it here made the
        # guard report "nothing held" about a key that may still be down — the
        # exact lie the guard exists to prevent — and it also threw away the
        # pending edge a later release could still retry.
        self._held = [record for record in self._held if not record.released]

    def presses_match_releases(self) -> bool:
        """The invariant: every press was matched by exactly one release."""
        return self.presses == self.releases and not self.release_failures


def _button_name(button: MouseButton | str) -> str:
    if isinstance(button, MouseButton):
        return button.value
    normalized = str(button).strip().lower()
    # Accept the common spellings; refuse anything else rather than defaulting
    # to the left button (a stray right-click is not benign).
    aliases = {
        "left": "left", "l": "left", "leftbutton": "left",
        "right": "right", "r": "right", "rightbutton": "right",
        "middle": "middle", "m": "middle", "middlebutton": "middle", "center": "middle",
    }
    if normalized not in aliases:
        raise ValueError(f"Unknown mouse button: {button!r} (expected left, right or middle).")
    return aliases[normalized]


@dataclass(frozen=True, slots=True)
class WindowGeometry:
    """Where a target window is, in the capture worker's physical-pixel space.

    ``bounds`` is the whole window; ``client`` is the client area relative to
    ``bounds`` (the part input should address, since the title bar is not
    clickable content). ``scale`` is the window's DPI scale as a multiplier
    (1.0 = 96 DPI, 2.0 = 200%), so a coordinate measured on a screenshot taken
    at a different scale can be converted rather than trusted blindly.

    ``minimized`` and ``visible`` are reported separately from identity. A
    minimized exclusive-fullscreen game still has the same handle and pid, but
    its window is at the off-screen ``-16000`` placeholder and the desktop is
    what a screen capture would see. Callers must not treat that as a live
    frame of the target.
    """

    handle: int
    title: str
    bounds: tuple[int, int, int, int]  # left, top, right, bottom (physical px)
    #: Client area as (offset_x, offset_y, width, height): the first two are the
    #: client origin relative to ``bounds``, the last two are its SIZE. That is
    #: what ``GetClientRect`` + ``ClientToScreen`` report, and reading the last
    #: two as absolute edges silently refuses every point right of
    #: ``offset_x + width`` on a window that does not start at the screen origin.
    client: tuple[int, int, int, int]
    pid: int = 0
    scale: float = 1.0
    foreground: bool = False
    minimized: bool = False
    visible: bool = True

    @property
    def width(self) -> int:
        return max(0, self.bounds[2] - self.bounds[0])

    @property
    def height(self) -> int:
        return max(0, self.bounds[3] - self.bounds[1])

    @property
    def client_origin(self) -> tuple[int, int]:
        """Absolute screen position of the client area's top-left corner."""
        return (self.bounds[0] + self.client[0], self.bounds[1] + self.client[1])

    def client_bounds(self) -> tuple[int, int, int, int]:
        """The client area in ABSOLUTE physical-pixel coordinates.

        ``client`` stores an offset plus a size, so the right/bottom edges are
        computed from the origin rather than read out of the tuple. The
        distinction only shows on a window whose top-left is not (0, 0) — which
        is most windows, and is exactly the case a test written against a
        window at the origin cannot catch.
        """
        origin_x, origin_y = self.client_origin
        return (origin_x, origin_y, origin_x + self.client[2], origin_y + self.client[3])

    def contains(self, x: int, y: int, *, client: bool = False) -> bool:
        """Whether a physical-pixel point falls inside the window (or its client area)."""
        left, top, right, bottom = self.client_bounds() if client else self.bounds
        return left <= x < right and top <= y < bottom

    def to_dict(self) -> dict[str, object]:
        return {
            "handle": self.handle,
            "title": self.title,
            "pid": self.pid,
            "bounds": {
                "left": self.bounds[0], "top": self.bounds[1],
                "right": self.bounds[2], "bottom": self.bounds[3],
                "width": self.width, "height": self.height,
            },
            "client": {
                "left": self.client[0], "top": self.client[1],
                "right": self.client[2], "bottom": self.client[3],
            },
            "scale": self.scale,
            "foreground": self.foreground,
            "minimized": self.minimized,
            "visible": self.visible,
        }


def scale_point(x: int, y: int, *, from_scale: float, to_scale: float) -> tuple[int, int]:
    """Convert a point between two DPI scales.

    A coordinate read off a screenshot taken at 200% is already in physical
    pixels, so when input is injected in the same space this is identity. It
    matters only when the two spaces genuinely differ — e.g. a target window on
    a different monitor at another scale — and the conversion is a multiply,
    not a guess.
    """
    if from_scale <= 0 or to_scale <= 0:
        raise ValueError(f"Invalid DPI scale: from={from_scale!r} to={to_scale!r}.")
    ratio = to_scale / from_scale
    return (round(x * ratio), round(y * ratio))


def client_to_screen(x: int, y: int, geometry: WindowGeometry) -> tuple[int, int]:
    """A client-area-relative point to an absolute screen point."""
    origin_x, origin_y = geometry.client_origin
    return (origin_x + x, origin_y + y)


def screen_to_client(x: int, y: int, geometry: WindowGeometry) -> tuple[int, int]:
    """An absolute screen point to a client-area-relative point."""
    origin_x, origin_y = geometry.client_origin
    return (x - origin_x, y - origin_y)


@dataclass(frozen=True, slots=True)
class InputTarget:
    """The validated destination for a coordinate-addressed input action."""

    geometry: WindowGeometry
    x: int
    y: int

    def to_dict(self) -> dict[str, object]:
        return {"window": self.geometry.title, "pid": self.geometry.pid, "x": self.x, "y": self.y}


def validate_input_target(
    geometry: WindowGeometry | None,
    x: int,
    y: int,
    *,
    expected_handle: int | None = None,
    expected_pid: int | None = None,
    expected_title: str | None = None,
    require_foreground: bool = False,
    in_client: bool = True,
) -> InputTarget:
    """Refuse to send input at a point that is not where we think it is.

    Three checks, each of which has a distinct failure the caller needs to
    distinguish:

    * the window must still exist (``geometry is not None``);
    * its IDENTITY must match — a handle, pid, or title substring the caller
      named. Without this, a click addressed to "the game window" lands in
      whatever window happens to be under that coordinate now;
    * the point must be INSIDE the window (its client area by default, so a
      click cannot land on the title bar and drag the window instead).

    ``require_foreground`` adds a fourth: the target must be the focused
    window, which is what makes a keyboard press land in the intended app at
    all.
    """
    if geometry is None:
        raise ValueError("The target window no longer exists — refusing to send input.")
    if expected_handle is not None and geometry.handle != expected_handle:
        raise ValueError(
            f"Target window identity changed (handle {geometry.handle} != {expected_handle}) "
            "— refusing to send input to a different window."
        )
    if expected_pid is not None and geometry.pid != expected_pid:
        raise ValueError(
            f"Target window identity changed (pid {geometry.pid} != {expected_pid}) "
            "— refusing to send input to a different process."
        )
    if expected_title is not None and expected_title.lower() not in geometry.title.lower():
        raise ValueError(
            f"Target window is no longer titled like {expected_title!r} (now {geometry.title!r}) "
            "— refusing to send input to a different window."
        )
    if not geometry.contains(x, y, client=in_client):
        where = "client area" if in_client else "bounds"
        extent = geometry.client_bounds() if in_client else geometry.bounds
        raise ValueError(
            f"Point ({x}, {y}) is outside the target window's {where} "
            f"{extent} — refusing to click into empty space."
        )
    if require_foreground and not geometry.foreground:
        raise ValueError(
            f"Target window {geometry.title!r} is not the foreground window — "
            "refusing to send input that would land in a different window."
        )
    return InputTarget(geometry=geometry, x=x, y=y)
