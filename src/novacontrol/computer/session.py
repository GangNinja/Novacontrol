"""The dedicated-session / VM execution boundary for guaranteed containment.

Guaranteed containment for *arbitrary* applications — including games, canvas
UIs and anything with no UI Automation pattern — needs the input to be unable
to reach the user's interactive desktop. On Windows that means a different
session (or a VM), not a different desktop: a desktop created with
``CreateDesktop`` on the same window station cannot receive ``SendInput``
without ``SwitchDesktop``, which replaces the user's desktop rather than
protecting it.

This module owns the boundary and the honest answer about whether it exists
here. It never fabricates an isolated session: :meth:`IsolatedSessionAdapter.feasibility`
reports the exact blocker, and :meth:`IsolatedSessionAdapter.start` raises
:class:`SessionUnavailable` with that blocker when no transport is configured.

A transport (an RDP/console session host, a Windows Sandbox, or a VM with a
device channel) is injected by whoever owns the platform integration; this
class owns session lifecycle, identity, dispatch accounting, emergency stop,
teardown and the audit trail. Nothing in it can inject into the interactive
session — :meth:`dispatch` only ever calls the transport bound to the isolated
session.
"""

from __future__ import annotations

import platform
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

__all__ = [
    "IsolatedSessionAdapter",
    "SessionBlocker",
    "SessionIdentity",
    "SessionState",
    "SessionTransport",
    "SessionUnavailable",
]


class SessionState(StrEnum):
    """Lifecycle of the isolated execution session."""

    BLOCKED = "blocked"
    READY = "ready"
    RUNNING = "running"
    STOPPING = "stopping"
    CLOSED = "closed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SessionBlocker:
    """Why isolated-session input is not available, and what it depends on."""

    code: str
    detail: str
    dependency: str

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "detail": self.detail,
            "dependency": self.dependency,
        }


@dataclass(frozen=True, slots=True)
class SessionIdentity:
    """Who the isolated session is. Never the interactive session's id."""

    session_id: int
    name: str
    transport: str
    isolated: bool
    started_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "name": self.name,
            "transport": self.transport,
            "isolated": self.isolated,
            "started_at": self.started_at,
        }


class SessionUnavailable(RuntimeError):
    """Raised when no guaranteed-containment session can be started."""

    def __init__(self, blocker: SessionBlocker) -> None:
        super().__init__(blocker.detail)
        self.blocker = blocker


class SessionTransport(Protocol):
    """The platform channel into a dedicated session or VM.

    Implementations must address input at the isolated session (an RDP input
    channel, a VM's virtual HID device, a sandbox API). They must never fall
    back to the host's ``SendInput``.
    """

    async def start(self) -> SessionIdentity: ...

    async def dispatch(self, event: str) -> None: ...

    async def shutdown(self) -> None: ...


#: Mechanisms examined for a dedicated session, and whether each is suitable.
_MECHANISMS: tuple[dict[str, Any], ...] = (
    {
        "mechanism": "Dedicated RDP / console session with its own logon token",
        "isolates_input": True,
        "suitable": True,
        "note": (
            "A separate session id cannot receive the interactive session's input. "
            "Requires credentials or a pre-provisioned logon token, which this "
            "process does not hold."
        ),
    },
    {
        "mechanism": "Windows Sandbox / Hyper-V VM with a device transport",
        "isolates_input": True,
        "suitable": True,
        "note": (
            "The strongest boundary. Requires virtualization support and a "
            "control channel (RDP, HID injection) that this process does not own."
        ),
    },
    {
        "mechanism": "CreateDesktop + SwitchDesktop",
        "isolates_input": False,
        "suitable": False,
        "note": (
            "A new desktop on the same window station cannot receive SendInput "
            "unless it is switched in, which replaces the user's desktop. Not an "
            "isolated input surface."
        ),
    },
    {
        "mechanism": "Hidden desktop without switching",
        "isolates_input": False,
        "suitable": False,
        "note": "SendInput fails with ERROR_ACCESS_DENIED: it is not the input desktop.",
    },
    {
        "mechanism": "PostMessage / WM_KEYDOWN to the target window",
        "isolates_input": False,
        "suitable": False,
        "note": (
            "Not genuine keyboard input; many applications ignore it, and a "
            "message posted to the wrong window is still a message to it."
        ),
    },
)


@dataclass(slots=True)
class IsolatedSessionAdapter:
    """Owns the isolated session's lifecycle, accounting and audit trail."""

    transport: SessionTransport | None = None
    platform_name: str = field(default_factory=lambda: platform.system())
    state: SessionState = SessionState.BLOCKED
    identity: SessionIdentity | None = None
    audit: list[dict[str, Any]] = field(default_factory=list)
    events_dispatched: int = 0
    stopped: bool = False

    # -- feasibility ------------------------------------------------------

    def feasibility(self) -> dict[str, Any]:
        """Whether this host can run input in a dedicated session, and why not."""
        blockers: list[SessionBlocker] = []
        if not self.platform_name.lower().startswith("windows"):
            blockers.append(
                SessionBlocker(
                    code="unsupported_platform",
                    detail=(
                        "The isolated-session profile is implemented for Windows "
                        f"session semantics; this host reports {self.platform_name!r}."
                    ),
                    dependency="A Windows host with session or VM control.",
                )
            )
        if self.transport is None:
            blockers.append(
                SessionBlocker(
                    code="no_session_transport",
                    detail=(
                        "No dedicated-session transport is configured. The "
                        "interactive session cannot stand in for one: its input "
                        "queue is shared with the user's applications."
                    ),
                    dependency=(
                        "A session host (RDP/console session with its own logon "
                        "token) or a VM with a device channel, plus the credentials "
                        "or sandbox API it needs."
                    ),
                )
            )
        return {
            "supported": not blockers,
            "profile": "isolated_session",
            "blockers": [blocker.to_dict() for blocker in blockers],
            "mechanisms": [dict(item) for item in _MECHANISMS],
            "interactive_session_is_not_isolation": True,
        }

    # -- lifecycle --------------------------------------------------------

    def _note(self, kind: str, **payload: Any) -> None:
        self.audit.append(
            {
                "at": datetime.now(UTC).isoformat(),
                "event": kind,
                **payload,
            }
        )

    async def start(self) -> SessionIdentity:
        """Start (or attach to) the isolated session, or report the blocker."""
        report = self.feasibility()
        if not report["supported"]:
            blockers = report["blockers"]
            first = blockers[0] if isinstance(blockers, list) and blockers else {}
            blocker = SessionBlocker(
                code=str(first.get("code", "no_session_transport")),
                detail=str(first.get("detail", "No isolated session is available.")),
                dependency=str(first.get("dependency", "")),
            )
            self.state = SessionState.BLOCKED
            self._note("session.blocked", blocker=blocker.to_dict())
            raise SessionUnavailable(blocker)
        transport = self.transport
        if transport is None:  # pragma: no cover - feasibility() already refused
            raise SessionUnavailable(
                SessionBlocker(
                    code="no_session_transport",
                    detail="No isolated session transport is configured.",
                    dependency="A session or VM control channel.",
                )
            )
        try:
            identity = await transport.start()
        except Exception as exc:
            self.state = SessionState.FAILED
            self._note("session.failed", error=f"{type(exc).__name__}: {exc}")
            raise
        if not identity.isolated:
            # A transport that reports the interactive session is not a
            # containment profile. Refuse rather than label it isolated.
            await transport.shutdown()
            self.state = SessionState.FAILED
            blocker = SessionBlocker(
                code="transport_not_isolated",
                detail=(
                    "The transport reported the interactive session, which is not "
                    "an isolated input surface."
                ),
                dependency="A transport bound to a different session id or a VM.",
            )
            self._note("session.refused", blocker=blocker.to_dict())
            raise SessionUnavailable(blocker)
        self.identity = identity
        self.stopped = False
        self.state = SessionState.READY
        self._note("session.ready", identity=identity.to_dict())
        return identity

    async def dispatch(self, event: str) -> dict[str, Any]:
        """Send one input event into the isolated session. Never the host."""
        if (
            self.state not in (SessionState.READY, SessionState.RUNNING)
            or self.stopped
            or self.transport is None
        ):
            self._note("input.refused", event=event, state=self.state.value)
            return {
                "dispatched": False,
                "event": event,
                "reason": (
                    "emergency_stop" if self.stopped else f"session_{self.state.value}"
                ),
                "containment": "isolated_session",
                "session": self.identity.to_dict() if self.identity is not None else None,
            }
        self.state = SessionState.RUNNING
        await self.transport.dispatch(event)
        self.events_dispatched += 1
        session_id = self.identity.session_id if self.identity is not None else 0
        self._note("input.dispatched", input_event=event, session_id=session_id)
        return {
            "dispatched": True,
            "event": event,
            "reason": "",
            "containment": "isolated_session",
            "session": self.identity.to_dict() if self.identity is not None else None,
            "events_dispatched": self.events_dispatched,
        }

    async def emergency_stop(self) -> dict[str, Any]:
        """Refuse all further events in this session and record the decision."""
        self.stopped = True
        self.state = SessionState.STOPPING if self.state is SessionState.RUNNING else self.state
        self._note("session.emergency_stop", events_dispatched=self.events_dispatched)
        return {
            "stopped": True,
            "events_dispatched": self.events_dispatched,
            "state": self.state.value,
        }

    async def teardown(self) -> dict[str, Any]:
        """Close the session. Idempotent; refusal to close is recorded, not lost."""
        if self.state is SessionState.CLOSED:
            # Idempotent: a second teardown must not claim a second close.
            return {"closed": True, "events_dispatched": self.events_dispatched}
        if self.transport is not None and self.state not in (
            SessionState.CLOSED,
            SessionState.BLOCKED,
        ):
            self.state = SessionState.STOPPING
            try:
                await self.transport.shutdown()
            except Exception as exc:
                self.state = SessionState.FAILED
                self._note("session.teardown_failed", error=f"{type(exc).__name__}: {exc}")
                return {
                    "closed": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "events_dispatched": self.events_dispatched,
                }
        self.state = SessionState.CLOSED
        self._note("session.closed", events_dispatched=self.events_dispatched)
        return {"closed": True, "events_dispatched": self.events_dispatched}
