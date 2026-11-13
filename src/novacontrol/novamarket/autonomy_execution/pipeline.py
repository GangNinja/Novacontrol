"""AT-04 controlled autonomous paper execution over the NM-10 engine.

The pipeline reads AT-03 risk-approved proposals, verifies the persisted NM-08
decision, asks NM-10 for a no-order preflight, durably records an execution
intent, and only then forwards the proposal to NM-10.  Orders, fills, cash,
positions and ledger entries are owned by NM-10 and the accepted accounting
stores; this module has no financial write path of its own.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal
from inspect import isawaitable
from typing import Any, Callable, Mapping

from novacontrol.novamarket.execution.models import execution_id, text
from novacontrol.novamarket.autonomy_execution.models import (
    ACTIVE_INTENT_STATUSES,
    AT04_CONTRACT_VERSION,
    BROKER_CONNECTED,
    EXECUTION_AUTHORITY,
    ExecutionIntentStatus,
    ExecutionNotFound,
    AutonomyExecutionError,
    LIVE_TRADING,
    MAX_EXECUTIONS_PER_CYCLE,
    MAX_SUBMISSION_RETRIES,
    PAPER_ONLY,
    RISK_AUTHORITY,
    SUBMISSION_TIMEOUT_SECONDS,
    is_terminal,
    status_for_order,
)

_LOGGER = logging.getLogger("novacontrol.novamarket.autonomy_execution")


def _parse_time(value: object) -> datetime:
    if isinstance(value, datetime):
        moment = value
    else:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _decimal(value: object, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001 - malformed stored data is reported, not trusted
        return Decimal(default)


class AutonomyPaperExecution:
    """The one AT-04 orchestration layer; NM-10 remains the executor."""

    def __init__(
        self,
        runtime: Any,
        decisions: Any,
        execution: Any,
        portfolios: Any,
        *,
        market_data: Any | None = None,
        clock: Callable[[], datetime] | None = None,
        publisher: Any | None = None,
        submission_timeout_seconds: float = SUBMISSION_TIMEOUT_SECONDS,
        maximum_retries: int = MAX_SUBMISSION_RETRIES,
    ) -> None:
        self._runtime = runtime
        self._decisions = decisions
        self._execution = execution
        self._portfolios = portfolios
        self._market_data = market_data
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._publisher = publisher
        self._submission_timeout_seconds = max(1.0, float(submission_timeout_seconds))
        self._maximum_retries = max(1, int(maximum_retries))
        self._lock = asyncio.Lock()
        self._closed = False
        self._last_error = ""
        self._last_error_code = ""

    # -- lifecycle and access ------------------------------------------------
    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def store(self) -> Any:
        return self._execution.store

    def close(self) -> None:
        self._closed = True

    def set_publisher(self, publisher: Any | None) -> None:
        self._publisher = publisher

    def _now(self) -> datetime:
        moment = self._clock()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc)

    def _now_iso(self) -> str:
        return self._now().isoformat()

    def _runtime_status(self) -> dict[str, Any]:
        try:
            status = self._runtime.status()
        except Exception as exc:  # noqa: BLE001 - a missing runtime fails closed
            raise AutonomyExecutionError(
                "RUNTIME_NOT_AUTONOMOUS",
                f"the autonomy runtime is unavailable: {type(exc).__name__}: {exc}",
            ) from exc
        return dict(status) if isinstance(status, Mapping) else {}

    @staticmethod
    def _lease_valid(status: Mapping[str, Any]) -> bool:
        lease = status.get("lease")
        if isinstance(lease, Mapping):
            if "valid" in lease:
                return bool(lease.get("valid"))
            if "lease_valid" in lease:
                return bool(lease.get("lease_valid"))
        return bool(status.get("lease_valid"))

    def _mode(self) -> str:
        return str(self._runtime_status().get("mode") or "disabled").lower()

    def _gate(self) -> str:
        if self._closed:
            raise AutonomyExecutionError(
                "EXECUTION_PIPELINE_CLOSED", "the AT-04 execution pipeline is closed"
            )
        status = self._runtime_status()
        mode = str(status.get("mode") or "disabled").lower()
        if mode != "autonomous":
            raise AutonomyExecutionError(
                "RUNTIME_NOT_AUTONOMOUS",
                f"paper execution requires autonomous mode; runtime is {mode}",
            )
        if not self._lease_valid(status):
            raise AutonomyExecutionError(
                "LEASE_INVALID",
                "paper execution requires a valid AT-01 runtime lease",
            )
        return mode

    async def _emit(self, kind: str, payload: Mapping[str, Any]) -> None:
        publisher = self._publisher
        if publisher is None:
            return
        try:
            called = publisher(f"novamarket.autonomy.execution.{kind}", dict(payload))
            if isawaitable(called):
                await called
        except Exception as exc:  # noqa: BLE001 - observability never rolls back a fill
            _LOGGER.warning(
                "novamarket.autonomy.execution.event_failed kind=%s error=%s", kind, exc
            )

    def _event(
        self,
        intent: Mapping[str, Any],
        kind: str,
        message: str = "",
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        try:
            self._execution.store.append_autonomy_event(
                str(intent.get("execution_id") or ""),
                kind,
                message=message,
                payload=dict(payload or {}),
                at=self._now_iso(),
            )
        except Exception as exc:  # noqa: BLE001 - the durable intent remains primary
            _LOGGER.warning("novamarket.autonomy.execution.journal_event_failed: %s", exc)

    # -- durable intent ------------------------------------------------------
    def _new_intent(
        self,
        proposal: Mapping[str, Any],
        *,
        mode: str,
        actor: str,
    ) -> dict[str, Any]:
        proposal_id = str(proposal.get("proposal_id") or "")
        risk_decision_id = str(proposal.get("risk_decision_id") or "")
        now = self._now_iso()
        return {
            "execution_id": execution_id("aex", proposal_id, risk_decision_id or "no-risk"),
            "proposal_id": proposal_id,
            "signal_id": str(proposal.get("signal_id") or ""),
            "run_id": str(proposal.get("run_id") or ""),
            "risk_decision_id": risk_decision_id,
            "portfolio_id": str(proposal.get("portfolio_id") or ""),
            "instrument_id": str(proposal.get("instrument_id") or ""),
            "exchange": str(proposal.get("exchange") or ""),
            "horizon": str(proposal.get("horizon") or ""),
            "side": str(proposal.get("side") or proposal.get("action") or ""),
            "idempotency_key": f"at04:{proposal_id}",
            "status": ExecutionIntentStatus.INTENT.value,
            "order_id": "",
            "order_status": "",
            "requested_quantity": "",
            "filled_quantity": "0",
            "remaining_quantity": "",
            "average_price": "",
            "total_fees": "0",
            "fill_ids": [],
            "intent_at": now,
            "submitted_at": "",
            "reconciled_at": "",
            "order_updated_at": "",
            "reconciliation_order_updated_at": "",
            "updated_at": now,
            "created_at": now,
            "retry_count": 0,
            "reconciliation_status": "pending",
            "reconciliation_id": "",
            "reconciliation_issues": [],
            "failure_code": "",
            "failure_reason": "",
            "recovery_info": "",
            "mode_at_intent": mode,
            "actor": str(actor or ""),
            "paper_only": True,
            "live_trading": False,
            "broker_connected": False,
        }

    def _save_new_intent(self, intent: dict[str, Any]) -> dict[str, Any]:
        saved = self._execution.store.save_autonomy_intent(intent)
        if not isinstance(saved, Mapping):
            raise AutonomyExecutionError(
                "EXECUTION_JOURNAL_FAILED", "the execution journal returned no record"
            )
        result = dict(saved)
        if result.get("execution_id") != intent.get("execution_id"):
            # A durable conflict is not an error: the winner is authoritative.
            return result
        return result

    def _update_intent(self, intent: Mapping[str, Any], **changes: Any) -> dict[str, Any]:
        body = dict(intent)
        body.update(changes)
        body["updated_at"] = self._now_iso()
        return dict(self._execution.store.update_autonomy_intent(body))

    def _risk_invalid_reason(
        self, proposal: Mapping[str, Any], risk: Mapping[str, Any] | None
    ) -> str:
        if not isinstance(risk, Mapping):
            return "the proposal has no persisted NM-08 risk assessment"
        if str(risk.get("proposal_id") or "") != str(proposal.get("proposal_id") or ""):
            return "the NM-08 assessment belongs to another proposal"
        if str(risk.get("risk_decision_id") or "") != str(
            proposal.get("risk_decision_id") or ""
        ):
            return "the NM-08 decision id does not match the proposal provenance"
        if str(risk.get("outcome") or "").strip().lower() != "approved":
            return f"the NM-08 decision is {risk.get('outcome') or 'not approved'}"
        expires = risk.get("expires_at")
        if expires not in (None, ""):
            try:
                if _parse_time(expires) <= self._now():
                    return "the NM-08 decision has expired"
            except Exception:  # noqa: BLE001 - malformed provenance fails closed
                return "the NM-08 decision expiry is invalid"
        proposal_state = str(proposal.get("risk_state_version") or "")
        decision_state = str(risk.get("state_version") or "")
        if proposal_state and decision_state and proposal_state != decision_state:
            return "the NM-08 decision was made against a different risk state"
        return ""

    async def _preflight(
        self, proposal: Mapping[str, Any]
    ) -> tuple[dict[str, Any] | None, str, str]:
        """Return (preflight, error_code, reason); never creates an order."""

        proposal_id = str(proposal.get("proposal_id") or "").strip()
        if not proposal_id:
            return None, "EXECUTION_PREFLIGHT_FAILED", "the proposal has no id"
        if str(proposal.get("status") or "") != "risk_approved":
            return (
                None,
                "RISK_APPROVAL_INVALID",
                f"proposal status is {proposal.get('status') or 'unknown'}, "
                "not risk_approved",
            )
        if str(proposal.get("risk_outcome") or "").strip().lower() != "approved":
            return None, "RISK_APPROVAL_INVALID", "the proposal has no approved risk outcome"
        if not str(proposal.get("risk_decision_id") or "").strip():
            return None, "RISK_APPROVAL_INVALID", "the proposal has no risk decision id"
        try:
            if _parse_time(proposal.get("expires_at")) <= self._now():
                return None, "PROPOSAL_EXPIRED", "the proposal approval has expired"
        except Exception as exc:  # noqa: BLE001
            return None, "PROPOSAL_EXPIRED", f"the proposal expiry is invalid: {exc}"

        risk = self._decisions.store.risk_assessment(proposal_id)
        reason = self._risk_invalid_reason(proposal, risk)
        if reason:
            return None, "RISK_APPROVAL_INVALID", reason

        reference = _decimal(proposal.get("reference_price"))
        notional = _decimal(proposal.get("notional"))
        if reference <= 0 or notional <= 0:
            return (
                None,
                "EXECUTION_PREFLIGHT_FAILED",
                "reference price and proposal notional must both be positive",
            )
        side = str(proposal.get("side") or proposal.get("action") or "").lower()
        if side not in {"buy", "sell", "add_position", "reduce_position", "exit", "remove_position"}:
            return None, "EXECUTION_PREFLIGHT_FAILED", "the proposal has no executable side"
        if not str(proposal.get("exchange") or "").strip():
            return None, "EXECUTION_PREFLIGHT_FAILED", "the proposal has no exchange"

        portfolio_id = str(proposal.get("portfolio_id") or "")
        try:
            detail = self._portfolios.detail(portfolio_id)
            portfolio = dict(detail.get("portfolio") or {})
        except Exception as exc:  # noqa: BLE001
            return None, "EXECUTION_PREFLIGHT_FAILED", f"portfolio unavailable: {exc}"
        portfolio_status = str(portfolio.get("status") or "").split(".")[-1].lower()
        if portfolio_status != "active":
            return (
                None,
                "EXECUTION_PREFLIGHT_FAILED",
                f"portfolio {portfolio_id} is not active ({portfolio_status or 'unknown'})",
            )
        if self._market_data is not None:
            try:
                instrument = self._market_data.get_instrument(
                    str(proposal.get("instrument_id") or "")
                )
            except Exception as exc:  # noqa: BLE001
                return None, "EXECUTION_PREFLIGHT_FAILED", f"instrument unavailable: {exc}"
            if instrument is None:
                return None, "EXECUTION_PREFLIGHT_FAILED", "the instrument is not registered"

        try:
            inspected = await self._execution.preflight(proposal_id, actor="autonomy-at04")
        except Exception as exc:  # noqa: BLE001 - unavailable engine fails closed
            return (
                None,
                "NM10_UNAVAILABLE",
                f"NM-10 preflight is unavailable: {type(exc).__name__}: {exc}",
            )
        if not isinstance(inspected, Mapping):
            return None, "NM10_UNAVAILABLE", "NM-10 preflight returned no result"
        if not bool(inspected.get("ok")):
            error_type = str(inspected.get("error_type") or "")
            code = "NM10_UNAVAILABLE" if error_type == "unavailable" else "EXECUTION_PREFLIGHT_FAILED"
            return None, code, str(inspected.get("error") or "NM-10 preflight refused")
        order = inspected.get("order")
        if not isinstance(order, Mapping):
            return None, "NM10_UNAVAILABLE", "NM-10 preflight returned no candidate order"
        return dict(inspected), "", ""

    # -- authoritative order synchronisation ---------------------------------
    @staticmethod
    def _fill_totals(fills: list[dict[str, Any]], order: Mapping[str, Any]) -> dict[str, Any]:
        quantities = [_decimal(fill.get("quantity")) for fill in fills]
        prices = [_decimal(fill.get("price")) for fill in fills]
        fees = [_decimal(fill.get("total_fees")) for fill in fills]
        filled = sum(quantities, Decimal("0"))
        average = (
            sum((qty * price for qty, price in zip(quantities, prices)), Decimal("0"))
            / filled
            if filled > 0
            else Decimal("0")
        )
        requested = _decimal(order.get("requested_quantity"))
        declared_filled = _decimal(order.get("filled_quantity"))
        remaining = _decimal(order.get("remaining_quantity"))
        fill_ids = [str(fill.get("fill_id") or "") for fill in fills]
        issues: list[str] = []
        if len(fill_ids) != len(set(fill_ids)):
            issues.append("a fill id appears more than once")
        if filled != declared_filled:
            issues.append(
                f"fill quantity {text(filled)} does not match order filled quantity "
                f"{text(declared_filled)}"
            )
        if declared_filled + remaining != requested:
            issues.append(
                "order filled + remaining quantity does not equal requested quantity"
            )
        status = str(order.get("status") or "")
        if status == "filled" and (filled != requested or remaining != 0):
            issues.append("order is filled but its quantities are incomplete")
        if status == "partially_filled" and not (Decimal("0") < declared_filled < requested):
            issues.append("order is partially filled but its quantities are inconsistent")
        if status == "partially_filled" and filled <= 0:
            issues.append("order is partially filled but has no fill records")
        return {
            "fill_count": len(fills),
            "fill_ids": fill_ids,
            "filled_quantity": text(declared_filled),
            "remaining_quantity": text(remaining),
            "requested_quantity": text(requested),
            "filled_from_records": text(filled),
            "average_price": text(average) if filled > 0 else "",
            "total_fees": text(sum(fees, Decimal("0"))),
            "issues": issues,
        }

    async def _sync_intent(
        self,
        intent: Mapping[str, Any],
        order_obj: Any | None = None,
        *,
        reconcile: bool = True,
        replay: bool = False,
    ) -> dict[str, Any]:
        body = dict(intent)
        proposal_id = str(body.get("proposal_id") or "")
        order = order_obj or self._execution.store.order_for_proposal(proposal_id)
        if order is None:
            return self._detail(body, replay=replay)
        order_payload = order.to_dict() if hasattr(order, "to_dict") else dict(order)
        fills = [
            fill.to_dict() if hasattr(fill, "to_dict") else dict(fill)
            for fill in self._execution.store.fills(str(order_payload.get("order_id") or ""))
        ]
        totals = self._fill_totals(fills, order_payload)
        next_status = status_for_order(order_payload)
        failure_code = str(body.get("failure_code") or "")
        failure_reason = str(body.get("failure_reason") or "")
        recovery_info = str(body.get("recovery_info") or "")
        if next_status == ExecutionIntentStatus.REJECTED.value:
            failure_code = "ORDER_REJECTED"
            failure_reason = str(
                order_payload.get("rejection_reason")
                or order_payload.get("last_error")
                or "NM-10 rejected the paper order"
            )
        elif next_status in {
            ExecutionIntentStatus.FAILED.value,
            ExecutionIntentStatus.CANCELLED.value,
            ExecutionIntentStatus.EXPIRED.value,
        }:
            failure_code = failure_code or "ORDER_STATE_MISMATCH"
            failure_reason = failure_reason or str(
                order_payload.get("last_error")
                or order_payload.get("cancellation_reason")
                or f"NM-10 order is {next_status}"
            )
        if totals["issues"]:
            next_status = ExecutionIntentStatus.RECONCILIATION_REQUIRED.value
            failure_code = "FILL_RECONCILIATION_FAILED"
            failure_reason = "; ".join(totals["issues"])
            recovery_info = "order and fill records are inconsistent; execution is blocked"

        reconciliation_status = str(body.get("reconciliation_status") or "pending")
        reconciliation_id = str(body.get("reconciliation_id") or "")
        reconciliation_issues = list(body.get("reconciliation_issues") or [])
        order_updated_at = str(order_payload.get("updated_at") or "")
        already_reconciled = (
            reconciliation_status == "passed"
            and str(body.get("reconciliation_order_updated_at") or "") == order_updated_at
            and not totals["issues"]
        )
        if reconcile and not already_reconciled:
            try:
                result = await self._execution.reconcile(str(order_payload.get("order_id") or ""))
                if isinstance(result, Mapping):
                    reconciliation_status = "passed" if result.get("ok") else "failed"
                    reconciliation_id = str(result.get("reconciliation_id") or "")
                    reconciliation_issues = list(result.get("issues") or [])
                else:
                    reconciliation_status = "failed"
                    reconciliation_issues = ["NM-10 reconciliation returned no result"]
            except Exception as exc:  # noqa: BLE001 - unknown reconciliation is unsafe
                reconciliation_status = "failed"
                reconciliation_issues = [f"reconciliation unavailable: {type(exc).__name__}: {exc}"]
            if reconciliation_status != "passed" and not totals["issues"]:
                next_status = ExecutionIntentStatus.RECONCILIATION_REQUIRED.value
                failure_code = "LEDGER_RECONCILIATION_FAILED"
                failure_reason = "; ".join(reconciliation_issues) or "NM-10 reconciliation failed"
                recovery_info = "financial reconciliation is unresolved; no retry is permitted"

        changed = (
            body.get("status") != next_status
            or body.get("order_id") != str(order_payload.get("order_id") or "")
            or body.get("order_status") != str(order_payload.get("status") or "")
            or body.get("fill_ids") != totals["fill_ids"]
            or body.get("reconciliation_status") != reconciliation_status
        )
        body = self._update_intent(
            body,
            status=next_status,
            order_id=str(order_payload.get("order_id") or ""),
            order_status=str(order_payload.get("status") or ""),
            requested_quantity=totals["requested_quantity"],
            filled_quantity=totals["filled_quantity"],
            remaining_quantity=totals["remaining_quantity"],
            average_price=totals["average_price"],
            total_fees=totals["total_fees"],
            fill_ids=totals["fill_ids"],
            reconciliation_status=reconciliation_status,
            reconciliation_id=reconciliation_id,
            reconciliation_issues=reconciliation_issues,
            reconciliation_order_updated_at=order_updated_at if reconciliation_status == "passed" else "",
            reconciled_at=self._now_iso() if reconciliation_status in {"passed", "failed"} else "",
            order_updated_at=order_updated_at,
            failure_code=failure_code,
            failure_reason=failure_reason,
            recovery_info=recovery_info,
        )
        if changed:
            event_payload = {
                "execution_id": body.get("execution_id"),
                "proposal_id": body.get("proposal_id"),
                "order_id": body.get("order_id"),
                "order_status": body.get("order_status"),
                "status": next_status,
                "filled_quantity": body.get("filled_quantity"),
                "remaining_quantity": body.get("remaining_quantity"),
                "reconciliation_status": reconciliation_status,
                "failure_code": failure_code,
            }
            self._event(
                body,
                f"order_{next_status}",
                failure_reason or f"NM-10 order is {body.get('order_status')}",
                event_payload,
            )
            await self._emit(f"order_{next_status}", event_payload)
        return self._detail(body, replay=replay)

    # -- one execution attempt ----------------------------------------------
    async def _execute_proposal(
        self, proposal: Mapping[str, Any], *, actor: str
    ) -> dict[str, Any]:
        proposal_id = str(proposal.get("proposal_id") or "")
        mode = self._gate()
        existing = self._execution.store.autonomy_intent(proposal_id=proposal_id)
        order = self._execution.store.order_for_proposal(proposal_id)
        if order is not None:
            if existing is None:
                existing = self._save_new_intent(
                    self._new_intent(proposal, mode=mode, actor=actor)
                )
                self._event(existing, "order_adopted", "existing NM-10 order adopted")
            return await self._sync_intent(existing, order, reconcile=True, replay=True)
        if existing is not None and is_terminal(str(existing.get("status"))):
            return self._detail(existing, replay=True)
        if existing is not None and int(existing.get("retry_count") or 0) >= self._maximum_retries:
            failed = self._update_intent(
                existing,
                status=ExecutionIntentStatus.FAILED.value,
                failure_code="EXECUTION_SUBMISSION_UNKNOWN",
                failure_reason="the submission retry ceiling was reached without an NM-10 order",
                recovery_info="inspect NM-10 orders manually; do not create another order",
            )
            self._event(failed, "retry_exhausted", str(failed.get("failure_reason") or ""))
            return self._detail(failed)

        intent = existing or self._save_new_intent(
            self._new_intent(proposal, mode=mode, actor=actor)
        )
        if existing is None:
            self._event(
                intent,
                "intent_persisted",
                "execution intent persisted before forwarding",
                {"idempotency_key": intent.get("idempotency_key")},
            )
        inspected, error_code, reason = await self._preflight(proposal)
        if inspected is None:
            failed = self._update_intent(
                intent,
                status=ExecutionIntentStatus.PREFLIGHT_FAILED.value,
                failure_code=error_code,
                failure_reason=reason,
                recovery_info="no order was forwarded; this proposal is not retried automatically",
            )
            self._event(failed, "preflight_failed", reason, {"failure_code": error_code})
            await self._emit(
                "preflight_failed",
                {
                    "execution_id": failed.get("execution_id"),
                    "proposal_id": failed.get("proposal_id"),
                    "failure_code": error_code,
                },
            )
            self._last_error = reason
            self._last_error_code = error_code
            return self._detail(failed)

        preflight_order = dict(inspected.get("order") or {})
        mode = self._gate()
        forwarding = self._update_intent(
            intent,
            status=ExecutionIntentStatus.FORWARDING.value,
            requested_quantity=str(preflight_order.get("requested_quantity") or ""),
            remaining_quantity=str(preflight_order.get("remaining_quantity") or ""),
            mode_at_submission=mode,
            retry_count=int(intent.get("retry_count") or 0) + 1,
            submitted_at=self._now_iso(),
            failure_code="",
            failure_reason="",
        )
        self._event(
            forwarding,
            "forwarding",
            "forwarding the risk-approved proposal to NM-10",
            {"retry_count": forwarding.get("retry_count"), "mode": mode},
        )
        await self._emit(
            "forwarding",
            {
                "execution_id": forwarding.get("execution_id"),
                "proposal_id": forwarding.get("proposal_id"),
                "retry_count": forwarding.get("retry_count"),
                "mode": mode,
            },
        )
        try:
            result = await asyncio.wait_for(
                self._execution.submit(
                    proposal_id,
                    idempotency_key=str(forwarding.get("idempotency_key") or ""),
                    actor=actor,
                ),
                timeout=self._submission_timeout_seconds,
            )
        except asyncio.TimeoutError:
            found = self._execution.store.order_for_proposal(proposal_id)
            if found is not None:
                return await self._sync_intent(forwarding, found, reconcile=True, replay=True)
            unknown = self._update_intent(
                forwarding,
                status=ExecutionIntentStatus.UNKNOWN.value,
                failure_code="EXECUTION_SUBMISSION_UNKNOWN",
                failure_reason="NM-10 submission timed out; the order outcome is unknown",
                recovery_info="reconcile with NM-10 before any further attempt",
            )
            self._event(unknown, "submission_unknown", str(unknown.get("failure_reason") or ""))
            await self._emit(
                "submission_unknown",
                {
                    "execution_id": unknown.get("execution_id"),
                    "proposal_id": unknown.get("proposal_id"),
                    "failure_code": "EXECUTION_SUBMISSION_UNKNOWN",
                },
            )
            self._last_error = str(unknown.get("failure_reason") or "")
            self._last_error_code = "EXECUTION_SUBMISSION_UNKNOWN"
            return self._detail(unknown)
        except Exception as exc:  # noqa: BLE001 - an uncertain submission fails closed
            found = self._execution.store.order_for_proposal(proposal_id)
            if found is not None:
                return await self._sync_intent(forwarding, found, reconcile=True, replay=True)
            unknown = self._update_intent(
                forwarding,
                status=ExecutionIntentStatus.UNKNOWN.value,
                failure_code="NM10_UNAVAILABLE",
                failure_reason=f"NM-10 submission is uncertain: {type(exc).__name__}: {exc}",
                recovery_info="reconcile with NM-10 before any further attempt",
            )
            self._event(unknown, "submission_unknown", str(unknown.get("failure_reason") or ""))
            self._last_error = str(unknown.get("failure_reason") or "")
            self._last_error_code = "NM10_UNAVAILABLE"
            return self._detail(unknown)

        if not isinstance(result, Mapping) or not isinstance(result.get("order"), Mapping):
            unknown = self._update_intent(
                forwarding,
                status=ExecutionIntentStatus.UNKNOWN.value,
                failure_code="EXECUTION_SUBMISSION_UNKNOWN",
                failure_reason="NM-10 returned an invalid submission response",
                recovery_info="inspect NM-10 records directly; no order was confirmed",
            )
            self._event(unknown, "submission_invalid", str(unknown.get("failure_reason") or ""))
            return self._detail(unknown)
        order_payload = dict(result.get("order") or {})
        if not order_payload.get("order_id"):
            unknown = self._update_intent(
                forwarding,
                status=ExecutionIntentStatus.UNKNOWN.value,
                failure_code="EXECUTION_SUBMISSION_UNKNOWN",
                failure_reason="NM-10 returned no authoritative order id",
                recovery_info="inspect NM-10 records directly; no order was confirmed",
            )
            self._event(unknown, "submission_invalid", str(unknown.get("failure_reason") or ""))
            return self._detail(unknown)
        submitted = self._update_intent(
            forwarding,
            order_id=str(order_payload.get("order_id") or ""),
            order_status=str(order_payload.get("status") or ""),
            requested_quantity=str(order_payload.get("requested_quantity") or ""),
            filled_quantity=str(order_payload.get("filled_quantity") or "0"),
            remaining_quantity=str(order_payload.get("remaining_quantity") or ""),
        )
        self._event(
            submitted,
            "submitted",
            "NM-10 returned the authoritative paper order",
            {
                "order_id": submitted.get("order_id"),
                "order_status": submitted.get("order_status"),
            },
        )
        await self._emit(
            "submitted",
            {
                "execution_id": submitted.get("execution_id"),
                "proposal_id": submitted.get("proposal_id"),
                "order_id": submitted.get("order_id"),
                "order_status": submitted.get("order_status"),
            },
        )
        synced = await self._sync_intent(submitted, reconcile=True)
        if synced["execution"]["status"] != ExecutionIntentStatus.RECONCILIATION_REQUIRED.value:
            self._last_error = ""
            self._last_error_code = ""
        return synced

    # -- reads ---------------------------------------------------------------
    def _detail(self, intent: Mapping[str, Any], *, replay: bool = False) -> dict[str, Any]:
        body = dict(intent)
        order = None
        order_id = str(body.get("order_id") or "")
        if order_id:
            stored = self._execution.store.order(order_id)
            if stored is not None:
                order = stored.to_dict()
        if order is None:
            stored = self._execution.store.order_for_proposal(str(body.get("proposal_id") or ""))
            if stored is not None:
                order = stored.to_dict()
        fills = []
        transitions: list[dict[str, Any]] = []
        if order is not None:
            fills = [
                fill.to_dict()
                for fill in self._execution.store.fills(str(order.get("order_id") or ""))
            ]
            transitions = list(
                self._execution.store.transitions(str(order.get("order_id") or ""))
            )
        return {
            "ok": True,
            "replay": bool(replay),
            "phase": "AT-04",
            "paper_only": True,
            "live_trading": False,
            "broker_connected": False,
            "execution": body,
            "order": order,
            "fills": fills,
            "transitions": transitions,
            "events": list(self._execution.store.autonomy_events(str(body.get("execution_id") or ""))),
        }

    def execution(self, execution_id_value: str) -> dict[str, Any]:
        intent = self._execution.store.autonomy_intent(execution_id=execution_id_value)
        if intent is None:
            raise ExecutionNotFound(f"unknown autonomous paper execution {execution_id_value!r}")
        return self._detail(intent)

    def executions(
        self,
        *,
        status: str = "",
        portfolio_id: str = "",
        limit: int = 100,
    ) -> dict[str, Any]:
        rows = self._execution.store.autonomy_intents(
            status=status, portfolio_id=portfolio_id, limit=limit
        )
        return {
            "ok": True,
            "phase": "AT-04",
            "paper_only": True,
            "live_trading": False,
            "executions": [dict(row) for row in rows],
            "total": len(rows),
            "counts": self._execution.store.autonomy_counts(),
        }

    def events(self, execution_id_value: str, *, limit: int = 200) -> dict[str, Any]:
        intent = self._execution.store.autonomy_intent(execution_id=execution_id_value)
        if intent is None:
            raise ExecutionNotFound(f"unknown autonomous paper execution {execution_id_value!r}")
        events = list(self._execution.store.autonomy_events(execution_id_value, limit=limit))
        return {
            "ok": True,
            "execution_id": execution_id_value,
            "events": events,
            "total": len(events),
        }

    def failures(self, *, limit: int = 100) -> dict[str, Any]:
        rows = [
            dict(row)
            for row in self._execution.store.autonomy_intents(limit=500)
            if str(row.get("failure_code") or "")
            or str(row.get("status") or "")
            in {
                ExecutionIntentStatus.FAILED.value,
                ExecutionIntentStatus.UNKNOWN.value,
                ExecutionIntentStatus.RECONCILIATION_REQUIRED.value,
                ExecutionIntentStatus.REJECTED.value,
                ExecutionIntentStatus.PREFLIGHT_FAILED.value,
            }
        ][: max(1, min(int(limit), 500))]
        return {"ok": True, "failures": rows, "total": len(rows)}

    def status(self) -> dict[str, Any]:
        runtime: dict[str, Any] = {}
        try:
            runtime = self._runtime_status()
        except AutonomyExecutionError as exc:
            runtime = {"mode": "unavailable", "error": exc.message}
        counts = self._execution.store.autonomy_counts()
        recent = [
            dict(row) for row in self._execution.store.autonomy_intents(limit=10)
        ]
        last_detail = self._detail(recent[0]) if recent else None
        reasons: list[str] = []
        if self._last_error:
            reasons.append(self._last_error)
        if counts.get(ExecutionIntentStatus.RECONCILIATION_REQUIRED.value, 0):
            reasons.append("one or more executions require reconciliation")
        if str(runtime.get("mode") or "") != "autonomous":
            reasons.append("autonomous order forwarding is inactive outside autonomous mode")
        return {
            "ok": True,
            "phase": "AT-04",
            "implemented": True,
            "initialised": not self._closed,
            "paper_only": True,
            "live_trading": False,
            "broker_connected": False,
            "execution_authority": EXECUTION_AUTHORITY,
            "risk_authority": RISK_AUTHORITY,
            "orders_possible": True,
            "live_orders_possible": False,
            "runtime_mode": runtime.get("mode", "unknown"),
            "lease_valid": self._lease_valid(runtime),
            "one_cycle_at_a_time": True,
            "counts": counts,
            "journal": {
                "intents": counts.get("intents", 0),
                "events": counts.get("events", 0),
            },
            "health": {
                "state": "degraded" if reasons else "ok",
                "reasons": reasons,
            },
            "last_execution": recent[0] if recent else None,
            "last_detail": last_detail,
            "recent": recent,
            "last_error": self._last_error,
            "last_error_code": self._last_error_code,
        }

    def contract(self) -> dict[str, Any]:
        return {
            "ok": True,
            "phase": "AT-04",
            "milestone": "AT-04",
            "version": AT04_CONTRACT_VERSION,
            "paper_only": True,
            "live_trading": False,
            "broker_connected": False,
            "execution_implemented": True,
            "orders_possible": True,
            "live_orders_possible": False,
            "execution_authority": EXECUTION_AUTHORITY,
            "risk_authority": RISK_AUTHORITY,
            "forwarding_modes": ["autonomous"],
            "blocked_modes": ["disabled", "monitoring", "paused"],
            "statuses": [item.value for item in ExecutionIntentStatus],
            "idempotency": "one durable intent per proposal; NM-10 order id is derived from proposal_id",
            "execution_flow": [
                "risk-approved AT-03 proposal",
                "persisted NM-08 verification",
                "NM-10 no-order preflight",
                "durable execution intent",
                "NM-10 paper submit",
                "authoritative order and fill records",
                "ledger/portfolio reconciliation",
                "journal lifecycle update",
            ],
            "error_codes": [
                "RISK_APPROVAL_INVALID",
                "PROPOSAL_EXPIRED",
                "EXECUTION_PREFLIGHT_FAILED",
                "DUPLICATE_EXECUTION",
                "EXECUTION_SUBMISSION_UNKNOWN",
                "NM10_UNAVAILABLE",
                "ORDER_REJECTED",
                "ORDER_STATE_MISMATCH",
                "PARTIAL_FILL",
                "FILL_RECONCILIATION_FAILED",
                "LEDGER_RECONCILIATION_FAILED",
                "LEASE_INVALID",
                "RUNTIME_NOT_AUTONOMOUS",
            ],
            "never": [
                "connect a broker",
                "submit a real-money order",
                "mutate cash, positions or ledger entries directly",
                "create a synthetic fill",
                "create a second execution engine",
                "implement protective exits or target-weight rebalancing",
            ],
            "runtime_gate": "AT-01 autonomous mode and a valid lease; one cycle at a time",
            "fill_authority": "NM-10 fills and NM-03/NM-07 accounting",
        }

    # -- recovery and cycle --------------------------------------------------
    async def recover(self) -> dict[str, Any]:
        if self._closed:
            return {"ok": False, "status": "closed", "recovered": 0}
        inflight: dict[str, Any] = {}
        try:
            result = await self._execution.recover_pending()
            inflight = dict(result) if isinstance(result, Mapping) else {"result": result}
        except Exception as exc:  # noqa: BLE001
            inflight = {"error": f"{type(exc).__name__}: {exc}"}
        recovered = 0
        reconciled = 0
        unresolved: list[str] = []
        for row in self._execution.store.autonomy_intents(limit=500):
            if is_terminal(str(row.get("status"))):
                continue
            order = self._execution.store.order_for_proposal(str(row.get("proposal_id") or ""))
            if order is not None:
                synced = await self._sync_intent(row, order, reconcile=True)
                recovered += 1
                if synced["execution"].get("reconciliation_status") == "passed":
                    reconciled += 1
                continue
            if str(row.get("status")) in ACTIVE_INTENT_STATUSES:
                if int(row.get("retry_count") or 0) >= self._maximum_retries:
                    failed = self._update_intent(
                        row,
                        status=ExecutionIntentStatus.FAILED.value,
                        failure_code="EXECUTION_SUBMISSION_UNKNOWN",
                        failure_reason="recovery found no NM-10 order and the retry ceiling is reached",
                        recovery_info="manual investigation required; no further order will be sent",
                    )
                    unresolved.append(str(failed.get("execution_id") or ""))
                else:
                    updated = self._update_intent(
                        row,
                        recovery_info=(
                            "intent survived a restart but no NM-10 order exists; "
                            "a retry uses the same idempotency key"
                        ),
                    )
                    self._event(updated, "recovery_checked", "no NM-10 order found")
        self._last_error = inflight.get("error", "") if isinstance(inflight.get("error"), str) else self._last_error
        return {
            "ok": True,
            "status": "completed",
            "inflight": inflight,
            "recovered": recovered,
            "reconciled": reconciled,
            "unresolved": unresolved,
            "paper_only": True,
            "live_trading": False,
        }

    async def run_cycle(
        self,
        *,
        trigger: str = "manual",
        actor: str = "autonomy-execution",
        proposal_id: str = "",
    ) -> dict[str, Any]:
        if self._closed:
            raise AutonomyExecutionError(
                "EXECUTION_PIPELINE_CLOSED", "the AT-04 execution pipeline is closed"
            )
        mode = self._gate()
        async with self._lock:
            mode = self._gate()
            recovery = await self.recover()
            if proposal_id:
                proposal = self._decisions.store.proposal(proposal_id)
                if proposal is None:
                    raise ExecutionNotFound(f"unknown decision proposal {proposal_id!r}")
                candidates = [dict(proposal)]
            else:
                candidates = [
                    dict(row)
                    for row in self._decisions.store.proposals(
                        status="risk_approved", limit=200
                    )
                ]
            eligible: list[Mapping[str, Any]] = []
            for row in candidates:
                if str(row.get("status") or "") != "risk_approved":
                    if proposal_id:
                        raise AutonomyExecutionError(
                            "RISK_APPROVAL_INVALID",
                            f"proposal {proposal_id!r} is not risk_approved",
                        )
                    continue
                intent = self._execution.store.autonomy_intent(
                    proposal_id=str(row.get("proposal_id") or "")
                )
                # An explicit request returns the durable replay record. A
                # scheduler pass skips terminal work to stay bounded.
                if (
                    not proposal_id
                    and intent is not None
                    and is_terminal(str(intent.get("status")))
                ):
                    continue
                try:
                    if _parse_time(row.get("expires_at")) <= self._now():
                        if proposal_id:
                            raise AutonomyExecutionError(
                                "PROPOSAL_EXPIRED",
                                f"proposal {proposal_id!r} has expired",
                            )
                        continue
                except AutonomyExecutionError:
                    raise
                except Exception:  # noqa: BLE001 - malformed expiry is handled by preflight
                    pass
                eligible.append(row)
                if len(eligible) >= MAX_EXECUTIONS_PER_CYCLE:
                    break
            results: list[dict[str, Any]] = []
            counts = {
                "submitted": 0,
                "filled": 0,
                "partially_filled": 0,
                "rejected": 0,
                "failed": 0,
                "preflight_failed": 0,
                "reconciliation_required": 0,
            }
            for proposal in eligible:
                try:
                    result = await self._execute_proposal(proposal, actor=actor)
                except AutonomyExecutionError as exc:
                    results.append(
                        {
                            "ok": False,
                            "proposal_id": proposal.get("proposal_id"),
                            "error": exc.code,
                            "message": exc.message,
                        }
                    )
                    counts["failed"] += 1
                    self._last_error = exc.message
                    self._last_error_code = exc.code
                    continue
                except Exception as exc:  # noqa: BLE001 - one proposal must not stop the cycle
                    results.append(
                        {
                            "ok": False,
                            "proposal_id": proposal.get("proposal_id"),
                            "error": "EXECUTION_INTERNAL_ERROR",
                            "message": f"{type(exc).__name__}: {exc}",
                        }
                    )
                    counts["failed"] += 1
                    self._last_error = f"{type(exc).__name__}: {exc}"
                    self._last_error_code = "EXECUTION_INTERNAL_ERROR"
                    continue
                results.append(result)
                status = str(result.get("execution", {}).get("status") or "")
                if status in counts:
                    counts[status] += 1
                elif status == ExecutionIntentStatus.UNKNOWN.value:
                    counts["failed"] += 1
            return {
                "ok": True,
                "phase": "AT-04",
                "status": "completed" if results else "idle",
                "trigger": str(trigger),
                "mode": mode,
                "paper_only": True,
                "live_trading": False,
                "broker_connected": False,
                "orders_possible": True,
                "recovery": recovery,
                "executions": results,
                "counts": counts,
                "total": len(results),
            }

    async def maybe_run(self) -> dict[str, Any]:
        if self._closed:
            return {"status": "closed", "reason": "execution_pipeline_closed"}
        try:
            if self._mode() != "autonomous":
                return {"status": "skipped", "reason": "runtime_not_autonomous"}
        except AutonomyExecutionError as exc:
            return {"status": "skipped", "reason": exc.code, "message": exc.message}
        recent = self._execution.store.autonomy_intents(limit=20)
        # A scheduler pass is cheap when nothing is approved; run_cycle still
        # re-checks the gate under its lock.
        approved = self._decisions.store.proposals(status="risk_approved", limit=20)
        pending = [
            row
            for row in approved
            if not any(
                intents.get("proposal_id") == row.get("proposal_id")
                and is_terminal(str(intents.get("status")))
                for intents in recent
            )
        ]
        if not pending:
            return {"status": "skipped", "reason": "no_eligible_risk_approved_proposals"}
        return await self.run_cycle(trigger="scheduler", actor="autonomy-scheduler")

    async def reconcile_execution(self, execution_id_value: str) -> dict[str, Any]:
        intent = self._execution.store.autonomy_intent(execution_id=execution_id_value)
        if intent is None:
            raise ExecutionNotFound(f"unknown autonomous paper execution {execution_id_value!r}")
        order = self._execution.store.order_for_proposal(str(intent.get("proposal_id") or ""))
        if order is None:
            if is_terminal(str(intent.get("status"))):
                return self._detail(intent, replay=True)
            raise AutonomyExecutionError(
                "ORDER_STATE_MISMATCH",
                "the journal has no NM-10 order to reconcile; no retry is permitted",
            )
        return await self._sync_intent(intent, order, reconcile=True)


__all__ = ["AutonomyPaperExecution"]
