"""The AT-01 runtime and safety shell.

There is deliberately no scan, signal, proposal, order, fill, portfolio or
ledger code here.  The runtime can own a lease and a mode; it cannot place an
order, and nothing in this package imports an execution or financial engine.
"""

from __future__ import annotations

import logging
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
    AUTOMATIC_POLICY_PROMOTION,
    BROKER_CONNECTED,
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


_LOGGER = logging.getLogger("novacontrol.novamarket.autonomy")


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
        "orders_possible": TRADING_IMPLEMENTED,
        "release_gate": RELEASE_GATE,
        "paper_only": PAPER_ONLY,
        "live_trading": LIVE_TRADING,
        "live_orders_possible": False,
        "broker_connected": BROKER_CONNECTED,
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
        "automatic_policy_promotion": AUTOMATIC_POLICY_PROMOTION,
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
        self._failed = False
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

    @property
    def config(self) -> AutonomyConfig:
        """The persisted runtime configuration, including auto-start policy."""

        return self._config()

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

    def _record_control(
        self,
        operation: str,
        *,
        actor: str,
        reason: str,
        outcome: str,
        requested_mode: str = "",
        resulting_mode: str | None = None,
        error: str = "",
    ) -> None:
        """Append a refusal or no-op control event without changing state.

        The runtime's own state is authoritative: a failed audit write must
        never turn a refusal into a state change, nor mask the operator-facing
        error.
        """

        try:
            snapshot = self._store.snapshot(self._now())
            lease = snapshot.get("lease")
            lease_body = lease if isinstance(lease, dict) else {}
            self._store.append_control_event(
                operation=operation,
                outcome=outcome,
                actor=actor,
                reason=reason,
                now=self._now(),
                from_mode=self._mode.value,
                requested_mode=requested_mode,
                resulting_mode=(
                    self._mode.value if resulting_mode is None else resulting_mode
                ),
                desired_mode=str(snapshot.get("desired_mode") or ""),
                lease_owner=str(lease_body.get("owner") or ""),
                lease_valid=bool(lease_body.get("valid")),
                error=error,
            )
        except Exception as exc:  # noqa: BLE001 - audited state stays authoritative
            _LOGGER.warning(
                "novamarket.autonomy.control_audit_unavailable op=%s: %s",
                operation,
                exc,
            )

    def _set_mode(
        self,
        mode: AutonomyMode,
        *,
        operation: str,
        actor: str,
        reason: str,
        desired_mode: AutonomyMode | None = None,
    ) -> None:
        self._require_lease()
        now = self._now()
        wanted_desired = desired_mode or mode
        # The mode row and its audit event commit together: a reader can never
        # observe a transition without the event that explains it, and a
        # rollback leaves neither.
        self._store.save_control(
            self.owner_id,
            self._lease_token,
            mode=mode.value,
            desired_mode=wanted_desired.value,
            state=RuntimeState.RUNNING.value,
            reason=reason,
            now=now,
            control={
                "operation": operation,
                "actor": actor,
                "reason": reason,
                "occurred_at": now,
                "from_mode": self._mode.value,
                "requested_mode": mode.value,
                "resulting_mode": mode.value,
                "desired_mode": wanted_desired.value,
                "lease_owner": self.owner_id,
                "lease_valid": True,
                "outcome": "applied",
            },
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

        if self._failed:
            state = RuntimeState.FAILED
            reasons.append(self._last_error or "the runtime failed to start")
        elif foreign:
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
            "orders_possible": TRADING_IMPLEMENTED,
            "release_gate": RELEASE_GATE,
            "paper_only": PAPER_ONLY,
            "live_trading": LIVE_TRADING,
            "live_orders_possible": False,
            "broker_connected": BROKER_CONNECTED,
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
            "automatic_policy_promotion": AUTOMATIC_POLICY_PROMOTION,
            "one_cycle_at_a_time": ONE_CYCLE_AT_A_TIME,
            "horizon_mapping": dict(HORIZON_MAPPING),
            "calendar": dict(CALENDAR_STATUS),
            "config": self._config().to_dict(),
            "controls": ["start", "pause", "resume", "stop"],
            "control_events": self._store.control_events(limit=20),
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
            or BROKER_CONNECTED
            or not TRADING_IMPLEMENTED
            or RELEASE_GATE != "open"
        ):
            raise AutonomyStateError(
                "refusing to start: the runtime is not in the approved integrated "
                "paper-only state (paper_only, no live trading/broker, paper "
                "execution implemented, release gate open)"
            )

    def start(
        self,
        mode: AutonomyMode | str,
        *,
        reason: str = "",
        acknowledge_paper_only: bool = False,
        actor: str = "system",
    ) -> dict[str, object]:
        """Start/renew the integrated paper runtime.

        ``autonomous`` enables the AT-03/AT-04 paper path.  Every order still
        requires NM-08 approval and NM-10 execution; the acknowledgement
        prevents an accidental transition into that mode.  Every accepted,
        no-op or refused attempt is appended to the durable control audit.
        """

        self._require_open()
        try:
            wanted = coerce_mode(mode)
        except (TypeError, ValueError) as exc:
            self._record_control(
                "start",
                actor=actor,
                reason=reason,
                outcome="refused",
                requested_mode=str(mode),
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
        if wanted not in (AutonomyMode.MONITORING, AutonomyMode.AUTONOMOUS):
            error = "start requires monitoring or autonomous mode"
            self._record_control(
                "start",
                actor=actor,
                reason=reason,
                outcome="refused",
                requested_mode=wanted.value,
                error=error,
            )
            raise AutonomyStateError(error)
        try:
            self._assert_paper_only_invariants()
        except AutonomyStateError as exc:
            self._record_control(
                "start",
                actor=actor,
                reason=reason,
                outcome="refused",
                requested_mode=wanted.value,
                error=str(exc),
            )
            raise
        self._failed = False
        if wanted is AutonomyMode.AUTONOMOUS and not acknowledge_paper_only:
            error = (
                "autonomous mode requires acknowledge_paper_only=true; paper "
                "orders remain subject to NM-08 and NM-10"
            )
            self._record_control(
                "start",
                actor=actor,
                reason=reason,
                outcome="refused",
                requested_mode=wanted.value,
                error=error,
            )
            raise AutonomyValidationError(error)
        if self._mode is AutonomyMode.PAUSED:
            error = "resume is required from paused mode"
            self._record_control(
                "start",
                actor=actor,
                reason=reason,
                outcome="refused",
                requested_mode=wanted.value,
                error=error,
            )
            raise AutonomyStateError(error)
        if self._mode == wanted and self._lease_valid():
            self._record_control(
                "start",
                actor=actor,
                reason=reason,
                outcome="noop",
                requested_mode=wanted.value,
            )
            return self.status()

        if self._mode is not AutonomyMode.DISABLED and not transition_allowed(
            self._mode, wanted
        ):
            error = f"cannot transition from {self._mode.value} to {wanted.value}"
            self._record_control(
                "start",
                actor=actor,
                reason=reason,
                outcome="refused",
                requested_mode=wanted.value,
                error=error,
            )
            raise AutonomyStateError(error)
        self._state = RuntimeState.STARTING
        try:
            self._acquire()
        except AutonomyLeaseError as exc:
            self._state = RuntimeState.DEGRADED
            self._record_control(
                "start",
                actor=actor,
                reason=reason,
                outcome="refused",
                requested_mode=wanted.value,
                error=str(exc),
            )
            raise
        try:
            self._set_mode(
                wanted,
                operation="start",
                actor=actor,
                reason=reason or "operator start",
            )
            # An explicit start is an opt-in to application-start autonomy.
            self._store.save_auto_start(
                enabled=True, mode=wanted.value, now=self._now()
            )
        except Exception as exc:
            # A failed persistence must not leave the process believing it owns
            # a mode that was never written.  Fail closed and require recovery.
            self._mode = AutonomyMode.DISABLED
            self._state = RuntimeState.FAILED
            self._failed = True
            self._last_error = f"{type(exc).__name__}: {exc}"
            raise
        self._started_at = self._now()
        return self.status()

    def pause(self, *, reason: str = "", actor: str = "system") -> dict[str, object]:
        self._require_open()
        if self._mode is AutonomyMode.DISABLED:
            error = "a disabled runtime cannot be paused"
            self._record_control(
                "pause",
                actor=actor,
                reason=reason,
                outcome="refused",
                requested_mode=AutonomyMode.PAUSED.value,
                error=error,
            )
            raise AutonomyStateError(error)
        if self._mode is AutonomyMode.PAUSED:
            self._record_control(
                "pause",
                actor=actor,
                reason=reason,
                outcome="noop",
                requested_mode=AutonomyMode.PAUSED.value,
            )
            return self.status()
        if not self._lease_valid():
            self._state = RuntimeState.DEGRADED
            error = "the runtime lease is not valid"
            self._record_control(
                "pause",
                actor=actor,
                reason=reason,
                outcome="refused",
                requested_mode=AutonomyMode.PAUSED.value,
                error=error,
            )
            raise AutonomyLeaseError(error)
        paused_from = self._mode
        self._set_mode(
            AutonomyMode.PAUSED,
            operation="pause",
            actor=actor,
            reason=reason or "operator pause",
            desired_mode=paused_from,
        )
        return self.status()

    def resume(
        self,
        mode: AutonomyMode | str | None = None,
        *,
        reason: str = "",
        acknowledge_paper_only: bool = False,
        actor: str = "system",
    ) -> dict[str, object]:
        self._require_open()
        if self._mode is not AutonomyMode.PAUSED:
            error = "resume requires paused mode"
            self._record_control(
                "resume",
                actor=actor,
                reason=reason,
                outcome="refused",
                requested_mode=str(mode or ""),
                error=error,
            )
            raise AutonomyStateError(error)
        if mode is None:
            snapshot = self._store.snapshot(self._now())
            try:
                wanted = coerce_mode(str(snapshot.get("desired_mode") or ""))
            except (ValueError, TypeError):
                wanted = AutonomyMode.MONITORING
            if wanted not in (AutonomyMode.MONITORING, AutonomyMode.AUTONOMOUS):
                # Older paused rows recorded ``paused`` as their desired mode;
                # resuming into the safe monitoring shell is the only fallback.
                wanted = AutonomyMode.MONITORING
        else:
            try:
                wanted = coerce_mode(mode)
            except (TypeError, ValueError) as exc:
                self._record_control(
                    "resume",
                    actor=actor,
                    reason=reason,
                    outcome="refused",
                    requested_mode=str(mode),
                    error=f"{type(exc).__name__}: {exc}",
                )
                raise
        if wanted not in (AutonomyMode.MONITORING, AutonomyMode.AUTONOMOUS):
            error = "resume requires monitoring or autonomous mode"
            self._record_control(
                "resume",
                actor=actor,
                reason=reason,
                outcome="refused",
                requested_mode=wanted.value,
                error=error,
            )
            raise AutonomyStateError(error)
        if wanted is AutonomyMode.AUTONOMOUS and not acknowledge_paper_only:
            error = (
                "autonomous mode requires acknowledge_paper_only=true; paper "
                "orders remain subject to NM-08 and NM-10"
            )
            self._record_control(
                "resume",
                actor=actor,
                reason=reason,
                outcome="refused",
                requested_mode=wanted.value,
                error=error,
            )
            raise AutonomyValidationError(error)
        if not self._lease_valid():
            self._state = RuntimeState.DEGRADED
            error = "the runtime lease is not valid"
            self._record_control(
                "resume",
                actor=actor,
                reason=reason,
                outcome="refused",
                requested_mode=wanted.value,
                error=error,
            )
            raise AutonomyLeaseError(error)
        self._set_mode(
            wanted, operation="resume", actor=actor, reason=reason or "operator resume"
        )
        return self.status()

    def stop(
        self,
        *,
        reason: str = "",
        disable_autostart: bool = False,
        actor: str = "system",
    ) -> dict[str, object]:
        """Stop the runtime.  An operator/emergency stop also disables auto-start;
        a graceful application shutdown preserves it for the next launch."""

        return self._stop(
            operation="stop",
            actor=actor,
            reason=reason or "operator stop",
            disable_autostart=disable_autostart,
        )

    def _stop(
        self,
        *,
        operation: str,
        actor: str,
        reason: str,
        disable_autostart: bool,
    ) -> dict[str, object]:
        if self._closed:
            return self.status()
        now = self._now()
        from_mode = self._mode.value
        if disable_autostart:
            self._store.save_auto_start(
                enabled=False,
                mode=self._config().auto_start_mode,
                now=now,
            )
        self._state = RuntimeState.STOPPING
        if self._lease_token:
            # The lease release and its audit event commit together, so an
            # emergency stop can never be durable without its record.
            self._store.release_lease(
                self.owner_id,
                self._lease_token,
                reason=reason or "operator stop",
                now=now,
                control={
                    "operation": operation,
                    "actor": actor,
                    "reason": reason,
                    "occurred_at": now,
                    "from_mode": from_mode,
                    "requested_mode": AutonomyMode.DISABLED.value,
                    "resulting_mode": AutonomyMode.DISABLED.value,
                    "desired_mode": AutonomyMode.DISABLED.value,
                    "lease_owner": self.owner_id,
                    "lease_valid": True,
                    "outcome": "applied",
                },
            )
        elif operation != "shutdown" or from_mode != AutonomyMode.DISABLED.value:
            self._record_control(
                operation,
                actor=actor,
                reason=reason,
                outcome="noop",
                requested_mode=AutonomyMode.DISABLED.value,
                resulting_mode=AutonomyMode.DISABLED.value,
                error=(
                    "no runtime lease was held; only in-memory state changed"
                    if operation != "shutdown"
                    else "the runtime was already stopped"
                ),
            )
        self._lease_token = ""
        self._mode = AutonomyMode.DISABLED
        self._state = RuntimeState.STOPPED
        self._failed = False
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
            now = self._now()
            self._store.release_lease(
                self.owner_id,
                self._lease_token,
                reason="runtime recovery",
                now=now,
                control={
                    "operation": "recover",
                    "actor": "system",
                    "reason": "runtime recovery",
                    "occurred_at": now,
                    "from_mode": self._mode.value,
                    "requested_mode": AutonomyMode.DISABLED.value,
                    "resulting_mode": AutonomyMode.DISABLED.value,
                    "desired_mode": AutonomyMode.DISABLED.value,
                    "lease_owner": self.owner_id,
                    "lease_valid": True,
                    "outcome": "applied",
                },
            )
            self._lease_token = ""
        self._store.recover(self._now())
        self._mode = AutonomyMode.DISABLED
        self._state = RuntimeState.STOPPED
        self._failed = False
        self._started_at = None
        self._last_error = ""
        return self.status()

    def fail(self, error: str) -> dict[str, object]:
        """Mark the runtime failed closed; no new entries may start."""

        self._failed = True
        self._last_error = str(error or "runtime dependency failure")
        self._state = RuntimeState.FAILED
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
        self._stop(
            operation="shutdown",
            actor="system",
            reason="runtime closed",
            disable_autostart=False,
        )
        self._closed = True
        self._store.close()


__all__ = ["AutonomyRuntime"]
