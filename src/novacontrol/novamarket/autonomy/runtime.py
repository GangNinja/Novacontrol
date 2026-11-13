"""The AT-01 runtime and safety shell.

There is deliberately no scan, signal, proposal, order, fill, portfolio or
ledger code here.  The runtime can own a lease and a mode; it cannot place an
order, and nothing in this package imports an execution or financial engine.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from novacontrol.novamarket.autonomy.models import (
    AutonomyConfig,
    AutonomyLeaseError,
    AutonomyMode,
    AutonomyStateError,
    AutonomyValidationError,
    CALENDAR_STATUS,
    CycleStatus,
    EXIT_SUPERVISION_IMPLEMENTED,
    HORIZON_MAPPING,
    LEARNING_PROMOTION_IMPLEMENTED,
    LIVE_TRADING,
    ONE_CYCLE_AT_A_TIME,
    PAPER_ONLY,
    PAUSED_EXIT_POLICY,
    RELEASE_GATE,
    RuntimeState,
    TRADING_IMPLEMENTED,
    coerce_mode,
    transition_allowed,
)
from novacontrol.novamarket.autonomy.store import AutonomyStore


def _clock_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("autonomy clock must return a timezone-aware datetime")
    return value.astimezone(UTC)


def closed_status(
    *,
    operator_authorized: bool | None = None,
    operator_configured: bool | None = None,
) -> dict[str, object]:
    """The honest stopped-shell view, usable without opening the store."""

    return {
        "ok": True,
        "available": True,
        "mode": AutonomyMode.DISABLED.value,
        "runtime_state": RuntimeState.STOPPED.value,
        "desired_mode": AutonomyMode.DISABLED.value,
        "trading_implemented": TRADING_IMPLEMENTED,
        "orders_possible": False,
        "release_gate": RELEASE_GATE,
        "paper_only": PAPER_ONLY,
        "live_trading": LIVE_TRADING,
        "broker_connected": False,
        "operator_authorization_required": True,
        "operator_authorization_configured": bool(operator_configured),
        "operator_authorized": bool(operator_authorized),
        "lease": {
            "held": False,
            "owner": "",
            "expires_at": None,
            "valid": False,
            "foreign": False,
            "conflict": False,
        },
        "owner_id": "",
        "active_cycle": None,
        "recovery_required": False,
        "last_error": "",
        "last_reason": "runtime closed",
        "started_at": None,
        "paused_exit_policy": PAUSED_EXIT_POLICY,
        "exit_supervision_implemented": EXIT_SUPERVISION_IMPLEMENTED,
        "learning_promotion_implemented": LEARNING_PROMOTION_IMPLEMENTED,
        "one_cycle_at_a_time": ONE_CYCLE_AT_A_TIME,
        "horizon_mapping": dict(HORIZON_MAPPING),
        "calendar": dict(CALENDAR_STATUS),
        "config": AutonomyConfig().to_dict(),
        "controls": ["start", "pause", "resume", "stop"],
        "health": {
            "state": "ok",
            "reasons": [],
            "lease_valid": False,
            "lease_conflict": False,
        },
    }


class AutonomyRuntime:
    """One runtime shell per process, with a durable single-owner lease."""

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
        owner_id: str | None = None,
        lease_seconds: int = 60,
        store: AutonomyStore | None = None,
    ) -> None:
        self.owner_id = owner_id or f"nvmkt-autonomy-{uuid4().hex[:12]}"
        self._clock = clock or (lambda: datetime.now(UTC))
        self.lease_seconds = max(10, min(int(lease_seconds), 3600))
        self._store = store or AutonomyStore(path)
        self._mode = AutonomyMode.DISABLED
        self._state = RuntimeState.STOPPED
        self._lease_token = ""
        self._last_error = ""
        self._started_at: datetime | None = None
        self._closed = False
        # Opening the store is a recovery point, never an auto-resume point.
        self._store.recover(self._now())

    @property
    def closed(self) -> bool:
        """True once this runtime's store has been closed for shutdown."""

        return self._closed

    @property
    def store(self) -> AutonomyStore:
        """The durable store this runtime owns; AT-02 persists monitoring here."""

        return self._store

    def _require_open(self) -> None:
        if self._closed:
            raise AutonomyStateError(
                "the autonomy runtime is closed; reopen it by re-enabling NovaMarket"
            )

    def _now(self) -> datetime:
        return _clock_utc(self._clock())

    def _lease_valid(self, snapshot: dict[str, object] | None = None) -> bool:
        snap = snapshot or self._store.snapshot(self._now())
        lease = snap.get("lease")
        return bool(
            isinstance(lease, dict)
            and lease.get("valid")
            and lease.get("owner") == self.owner_id
        )

    def _foreign_live_lease(self, snapshot: dict[str, object] | None = None) -> bool:
        snap = snapshot or self._store.snapshot(self._now())
        lease = snap.get("lease")
        return bool(
            isinstance(lease, dict)
            and lease.get("valid")
            and lease.get("owner")
            and lease.get("owner") != self.owner_id
        )

    def _acquire(self) -> None:
        self._require_open()
        now = self._now()
        token = uuid4().hex
        if not self._store.acquire_lease(
            self.owner_id,
            token,
            lease_seconds=self.lease_seconds,
            now=now,
        ):
            raise AutonomyLeaseError(
                "another live autonomy runtime owns the durable lease; "
                "stop that runtime or wait for the lease to expire"
            )
        self._lease_token = token

    def _require_lease(self) -> None:
        self._require_open()
        if not self._lease_token or not self._lease_valid():
            self._state = RuntimeState.DEGRADED
            raise AutonomyLeaseError(
                "the runtime lease is missing, expired or owned by another process"
            )

    def _set_mode(self, mode: AutonomyMode, *, reason: str) -> None:
        self._require_lease()
        self._store.save_control(
            self.owner_id,
            self._lease_token,
            mode=mode.value,
            state=RuntimeState.RUNNING.value,
            reason=reason,
            now=self._now(),
        )
        self._mode = mode
        self._state = RuntimeState.RUNNING
        self._last_error = ""

    def status(
        self,
        *,
        operator_authorized: bool | None = None,
        operator_configured: bool | None = None,
    ) -> dict[str, object]:
        """Return server-authoritative state without inventing a healthy lease."""

        if self._closed:
            return closed_status(
                operator_authorized=operator_authorized,
                operator_configured=operator_configured,
            )
        now = self._now()
        snapshot = self._store.snapshot(now)
        lease = dict(snapshot["lease"])
        local_lease = self._lease_valid(snapshot)
        foreign = self._foreign_live_lease(snapshot)
        active = snapshot.get("active_cycle")
        reasons: list[str] = []

        if foreign:
            state = RuntimeState.DEGRADED
            reasons.append("a different live runtime owner holds the autonomy lease")
        elif self._mode is not AutonomyMode.DISABLED and not local_lease:
            state = RuntimeState.DEGRADED
            reasons.append("the runtime lease has expired")
        elif self._mode is AutonomyMode.DISABLED:
            state = RuntimeState.STOPPED
        else:
            state = RuntimeState.RUNNING

        if active:
            reasons.append("an autonomy cycle is recorded as active")

        return {
            "ok": not reasons or state is RuntimeState.RUNNING,
            "available": True,
            "mode": self._mode.value,
            "runtime_state": state.value,
            "desired_mode": snapshot.get("desired_mode", "disabled"),
            "trading_implemented": TRADING_IMPLEMENTED,
            "orders_possible": False,
            "release_gate": RELEASE_GATE,
            "paper_only": PAPER_ONLY,
            "live_trading": LIVE_TRADING,
            "broker_connected": False,
            "operator_authorization_required": True,
            "operator_authorization_configured": bool(operator_configured),
            "operator_authorized": bool(operator_authorized),
            "lease": {
                "held": local_lease,
                "owner": lease.get("owner", ""),
                "expires_at": lease.get("expires_at"),
                "valid": local_lease,
                "foreign": foreign,
                "conflict": foreign,
            },
            "owner_id": self.owner_id,
            "active_cycle": self._public_cycle(active),
            "recovery_required": foreign or (
                self._mode is not AutonomyMode.DISABLED and not local_lease
            ),
            "last_error": self._last_error or snapshot.get("last_error", ""),
            "last_reason": snapshot.get("last_reason", ""),
            "started_at": self._started_at.isoformat() if self._started_at else None,
            "paused_exit_policy": PAUSED_EXIT_POLICY,
            "exit_supervision_implemented": EXIT_SUPERVISION_IMPLEMENTED,
            "learning_promotion_implemented": LEARNING_PROMOTION_IMPLEMENTED,
            "one_cycle_at_a_time": ONE_CYCLE_AT_A_TIME,
            "horizon_mapping": dict(HORIZON_MAPPING),
            "calendar": dict(CALENDAR_STATUS),
            "config": self._config().to_dict(),
            "controls": ["start", "pause", "resume", "stop"],
            "health": {
                "state": "ok" if not reasons else "degraded",
                "reasons": reasons,
                "lease_valid": local_lease,
                "lease_conflict": foreign,
            },
        }

    @staticmethod
    def _public_cycle(cycle: object) -> dict[str, object] | None:
        """Never expose the lease token through a status/cycle read model."""

        if not isinstance(cycle, dict):
            return None
        return {
            key: cycle[key]
            for key in (
                "cycle_id",
                "cycle_key",
                "owner_id",
                "status",
                "started_at",
                "finished_at",
                "last_error",
            )
            if key in cycle
        }

    def _config(self) -> AutonomyConfig:
        return self._store.config()

    @staticmethod
    def _assert_paper_only_invariants() -> None:
        """Refuse to enter a mode if the build's safety constants drift."""

        if (
            not PAPER_ONLY
            or LIVE_TRADING
            or TRADING_IMPLEMENTED
            or RELEASE_GATE != "closed"
        ):
            raise AutonomyStateError(
                "refusing to start: paper-only/live-trading/release-gate invariants "
                "are not the approved AT-01 values"
            )

    def start(
        self,
        mode: AutonomyMode | str,
        *,
        reason: str = "",
        acknowledge_paper_only: bool = False,
    ) -> dict[str, object]:
        """Start/renew the shell in monitoring or autonomous mode.

        ``autonomous`` is a state-only shell at AT-01.  It cannot produce an
        order; the release gate stays closed.  The acknowledgement is still
        required so the mode cannot be entered by an accidental request.
        """

        self._require_open()
        wanted = coerce_mode(mode)
        if wanted not in (AutonomyMode.MONITORING, AutonomyMode.AUTONOMOUS):
            raise AutonomyStateError("start requires monitoring or autonomous mode")
        self._assert_paper_only_invariants()
        if wanted is AutonomyMode.AUTONOMOUS and not acknowledge_paper_only:
            raise AutonomyValidationError(
                "autonomous mode requires acknowledge_paper_only=true; no order path exists"
            )
        if self._mode is AutonomyMode.PAUSED:
            raise AutonomyStateError("resume is required from paused mode")
        if self._mode == wanted and self._lease_valid():
            return self.status()

        if self._mode is not AutonomyMode.DISABLED and not transition_allowed(
            self._mode, wanted
        ):
            raise AutonomyStateError(
                f"cannot transition from {self._mode.value} to {wanted.value}"
            )
        self._acquire()
        try:
            self._set_mode(wanted, reason=reason or "operator start")
        except Exception:
            # A failed persistence must not leave the process believing it owns
            # a mode that was never written.  The lease is released on the safe
            # path only; an unexpired lease is the durable evidence.
            self._mode = AutonomyMode.DISABLED
            self._state = RuntimeState.DEGRADED
            raise
        self._started_at = self._now()
        return self.status()

    def pause(self, *, reason: str = "") -> dict[str, object]:
        self._require_open()
        if self._mode is AutonomyMode.DISABLED:
            raise AutonomyStateError("a disabled runtime cannot be paused")
        if self._mode is AutonomyMode.PAUSED:
            return self.status()
        if not self._lease_valid():
            self._state = RuntimeState.DEGRADED
            raise AutonomyLeaseError("the runtime lease is not valid")
        self._set_mode(AutonomyMode.PAUSED, reason=reason or "operator pause")
        return self.status()

    def resume(
        self,
        mode: AutonomyMode | str,
        *,
        reason: str = "",
        acknowledge_paper_only: bool = False,
    ) -> dict[str, object]:
        self._require_open()
        if self._mode is not AutonomyMode.PAUSED:
            raise AutonomyStateError("resume requires paused mode")
        wanted = coerce_mode(mode)
        if wanted not in (AutonomyMode.MONITORING, AutonomyMode.AUTONOMOUS):
            raise AutonomyStateError("resume requires monitoring or autonomous mode")
        if wanted is AutonomyMode.AUTONOMOUS and not acknowledge_paper_only:
            raise AutonomyValidationError(
                "autonomous mode requires acknowledge_paper_only=true; no order path exists"
            )
        if not self._lease_valid():
            self._state = RuntimeState.DEGRADED
            raise AutonomyLeaseError("the runtime lease is not valid")
        self._set_mode(wanted, reason=reason or "operator resume")
        return self.status()

    def stop(self, *, reason: str = "") -> dict[str, object]:
        if self._closed:
            return self.status()
        if self._lease_token:
            self._store.release_lease(
                self.owner_id,
                self._lease_token,
                reason=reason or "operator stop",
                now=self._now(),
            )
        self._lease_token = ""
        self._mode = AutonomyMode.DISABLED
        self._state = RuntimeState.STOPPED
        self._started_at = None
        self._last_error = ""
        return self.status()

    def heartbeat(self) -> bool:
        self._require_open()
        if not self._lease_token or self._mode is AutonomyMode.DISABLED:
            return False
        renewed = self._store.heartbeat(
            self.owner_id,
            self._lease_token,
            lease_seconds=self.lease_seconds,
            now=self._now(),
        )
        if not renewed:
            self._state = RuntimeState.DEGRADED
            self._last_error = "the runtime lease could not be renewed"
        return renewed

    def recover(self) -> dict[str, object]:
        """Explicit recovery: clear this owner's lease and never auto-resume.

        The store preserves a *live* lease to protect a different owner. That
        rule must not preserve the caller's own live lease: a recovery that
        leaves it behind reports ``disabled`` while the durable row still
        blocks the next start.
        """

        self._require_open()
        if self._lease_token:
            self._store.release_lease(
                self.owner_id,
                self._lease_token,
                reason="runtime recovery",
                now=self._now(),
            )
            self._lease_token = ""
        self._store.recover(self._now())
        self._mode = AutonomyMode.DISABLED
        self._state = RuntimeState.STOPPED
        self._started_at = None
        return self.status()

    # -- the one-cycle invariant (no scanning or execution) -----------------

    def claim_cycle(
        self,
        cycle_id: str,
        cycle_key: str,
    ) -> dict[str, object]:
        """Claim the single active cycle slot; AT-01 never runs it."""

        if self._mode not in (AutonomyMode.MONITORING, AutonomyMode.AUTONOMOUS):
            raise AutonomyStateError(
                "a cycle can be claimed only while monitoring or autonomous"
            )
        self._require_lease()
        claimed = self._store.claim_cycle(
            cycle_id,
            cycle_key,
            owner_id=self.owner_id,
            lease_token=self._lease_token,
            now=self._now(),
        )
        return self._public_cycle(claimed) or {}

    def finish_cycle(
        self,
        cycle_id: str,
        *,
        status: CycleStatus | str = CycleStatus.COMPLETED,
        error: str = "",
    ) -> None:
        self._require_lease()
        self._store.finish_cycle(
            cycle_id,
            owner_id=self.owner_id,
            status=str(status),
            error=error,
            now=self._now(),
        )

    def close(self) -> None:
        """Release the lease and close the owned SQLite handle exactly once."""

        if self._closed:
            return
        self.stop(reason="runtime closed")
        self._closed = True
        self._store.close()


__all__ = ["AutonomyRuntime"]
