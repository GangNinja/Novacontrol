"""PI-21 query, handoff and reconciliation operations.

The only state-changing operation that can reach a financial system is
``handoff_cycle``.  It forwards a matching, persisted, NM-08-approved AT-03
proposal id to the existing AT-04 executor.  PI-21 never supplies an order,
quantity, price or risk decision of its own.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from novacontrol.novamarket.portfolio_management.evidence import ManagementSources
from novacontrol.novamarket.portfolio_management.models import (
    CycleStatus,
    PortfolioManagementNotFound,
    PortfolioManagementStateError,
    stable_id,
)


def _text(value: object, limit: int = 400) -> str:
    return str(value or "").strip()[:limit]


def _dict(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _items(value: object) -> list[dict[str, Any]]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [dict(row) for row in value if isinstance(row, Mapping)]
    if isinstance(value, Mapping):
        for key in ("items", "executions", "results", "events"):
            rows = value.get(key)
            if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes, bytearray)):
                return [dict(row) for row in rows if isinstance(row, Mapping)]
    return []


def _as_int(value: object, default: int, low: int, high: int) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        parsed = default
    return max(low, min(high, int(parsed)))


def _mtime(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class PortfolioManagementActionsMixin:
    if TYPE_CHECKING:
        from novacontrol.novamarket.portfolio_management.store import (
            PortfolioManagementStore,
        )

        _store: PortfolioManagementStore
        _sources: ManagementSources

        def _now(self) -> datetime: ...

        def _runtime(self) -> dict[str, Any]: ...

        def _mode(self, runtime: Mapping[str, Any] | None = None) -> str: ...

        def _kill_switch(self, runtime: Mapping[str, Any]) -> bool: ...

    def cycles(
        self,
        *,
        portfolio_id: str = "",
        status: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        body = self._store.cycles(
            portfolio_id=_text(portfolio_id, 160),
            status=_text(status, 40),
            limit=_as_int(limit, 50, 1, 100),
            offset=_as_int(offset, 0, 0, 100_000),
        )
        return {"ok": True, "phase": "PI-21", **body}

    def cycle(self, cycle_id: str) -> dict[str, Any]:
        cycle = self._store.get_cycle(cycle_id)
        cycle["events"] = self._store.events(cycle_id, limit=500)
        cycle["ok"] = True
        return cycle

    def latest_cycle(self, portfolio_id: str = "") -> dict[str, Any] | None:
        return self._store.latest_cycle(_text(portfolio_id, 160))

    def snapshot(self, portfolio_id: str) -> dict[str, Any]:
        row = self._store.latest_snapshot(_text(portfolio_id, 160))
        if row is None:
            raise PortfolioManagementNotFound(
                f"no management snapshot exists for portfolio {portfolio_id!r}"
            )
        return {"ok": True, **row}

    def positions(self, portfolio_id: str) -> dict[str, Any]:
        rows = self._store.latest_positions(_text(portfolio_id, 160))
        return {
            "ok": True,
            "portfolio_id": portfolio_id,
            "positions": rows,
            "total": len(rows),
        }

    def opportunities(self, portfolio_id: str) -> dict[str, Any]:
        rows = self._store.latest_opportunities(_text(portfolio_id, 160))
        return {
            "ok": True,
            "portfolio_id": portfolio_id,
            "opportunities": rows,
            "eligible": sum(1 for row in rows if row.get("eligible")),
            "total": len(rows),
        }

    def decisions(self, portfolio_id: str, *, limit: int = 100) -> dict[str, Any]:
        rows = self._store.latest_decisions(
            _text(portfolio_id, 160), limit=_as_int(limit, 100, 1, 500)
        )
        return {
            "ok": True,
            "portfolio_id": portfolio_id,
            "decisions": rows,
            "total": len(rows),
        }

    def capital(self, portfolio_id: str) -> dict[str, Any]:
        row = self._store.latest_capital(_text(portfolio_id, 160))
        if row is None:
            raise PortfolioManagementNotFound(
                f"no capital assessment exists for portfolio {portfolio_id!r}"
            )
        return {"ok": True, **row}

    def cash(self, portfolio_id: str) -> dict[str, Any]:
        row = self._store.latest_cash(_text(portfolio_id, 160))
        if row is None:
            raise PortfolioManagementNotFound(
                f"no cash assessment exists for portfolio {portfolio_id!r}"
            )
        return {"ok": True, **row}

    def exposure(self, portfolio_id: str) -> dict[str, Any]:
        row = self._store.latest_exposure(_text(portfolio_id, 160))
        if row is None:
            raise PortfolioManagementNotFound(
                f"no exposure assessment exists for portfolio {portfolio_id!r}"
            )
        return {"ok": True, **row}

    def rebalance(self, portfolio_id: str) -> dict[str, Any]:
        row = self._store.latest_rebalance(_text(portfolio_id, 160))
        if row is None:
            raise PortfolioManagementNotFound(
                f"no rebalancing proposal exists for portfolio {portfolio_id!r}"
            )
        return {"ok": True, **row}

    def analytics(self, portfolio_id: str) -> dict[str, Any]:
        cycle = self._store.latest_cycle(_text(portfolio_id, 160))
        if cycle is None:
            raise PortfolioManagementNotFound(
                f"no management cycle exists for portfolio {portfolio_id!r}"
            )
        return {
            "ok": True,
            "phase": "PI-21",
            "portfolio_id": portfolio_id,
            "cycle_id": cycle.get("cycle_id"),
            "analytics": cycle.get("analytics") or {},
            "authoritative_valuation": _dict(cycle.get("snapshot", {})).get("valuation")
            if isinstance(cycle.get("snapshot"), Mapping)
            else None,
            "separation": {
                "profitability_is_not_decision_quality": True,
                "recommendation_outcomes_require_measured_follow_up": True,
                "execution_quality_authority": "NM-10",
            },
        }

    async def handoff_cycle(
        self,
        cycle_id: str,
        *,
        actor: str = "operator",
        max_changes: int = 10,
    ) -> dict[str, Any]:
        cycle = self._store.get_cycle(cycle_id)
        status = str(cycle.get("status") or "")
        if status == CycleStatus.RUNNING.value:
            raise PortfolioManagementStateError(
                "a running management cycle cannot hand off; wait for it to complete"
            )
        if status == CycleStatus.FAILED.value:
            raise PortfolioManagementStateError("a failed management cycle cannot hand off")
        if status in {
            CycleStatus.WAITING_FOR_EVIDENCE.value,
            CycleStatus.CANCELLED.value,
            CycleStatus.INTERRUPTED.value,
            CycleStatus.MONITORING_ONLY.value,
        }:
            raise PortfolioManagementStateError(
                f"a management cycle in status {status} cannot hand off to PI-12",
                code="CYCLE_NOT_HANDOFFABLE",
            )
        handoff = _dict(cycle.get("handoff"))
        if handoff.get("completed") and handoff.get("status") in {
            CycleStatus.EXECUTED.value,
            CycleStatus.PARTIALLY_EXECUTED.value,
            CycleStatus.ORDER_PENDING.value,
        }:
            return {
                "ok": True,
                "phase": "PI-21",
                "cycle_id": cycle_id,
                "handoff": handoff,
                "replayed": True,
                "order_submission": False,
                "financial_mutation": False,
            }
        runtime = self._runtime()
        mode = self._mode(runtime)
        if mode != "autonomous":
            raise PortfolioManagementStateError(
                f"paper handoff requires autonomous mode; runtime is {mode}",
                code="RUNTIME_NOT_AUTONOMOUS",
            )
        if self._kill_switch(runtime):
            raise PortfolioManagementStateError(
                "the autonomy kill switch blocks new handoffs",
                code="KILL_SWITCH_ACTIVE",
            )
        if _text(cycle.get("mode"), 32).lower() != "autonomous":
            raise PortfolioManagementStateError(
                "the review was produced outside autonomous mode; it cannot be handed off",
                code="REVIEW_NOT_AUTONOMOUS",
            )
        decisions = _items(cycle.get("decisions"))
        eligible = [
            row
            for row in decisions
            if _dict(row.get("handoff")).get("eligible")
            and _text(_dict(row.get("handoff")).get("proposal_id"), 160)
        ]
        limit = _as_int(max_changes, 10, 1, 20)
        eligible = eligible[:limit]
        handoff_id = stable_id("pmho_", cycle_id, actor)
        if not eligible:
            handoff = {
                "handoff_id": handoff_id,
                "status": CycleStatus.WAITING_FOR_RISK_ASSESSMENT.value,
                "reason": (
                    "No management recommendation has a matching persisted NM-08-approved "
                    "AT-03 proposal; no order was submitted."
                ),
                "executions": [],
                "completed": True,
                "order_submission": False,
                "financial_mutation": False,
                "at": self._now().isoformat(),
            }
            self._finish_handoff(cycle_id, handoff)
            return {
                "ok": True,
                "phase": "PI-21",
                "cycle_id": cycle_id,
                "handoff": handoff,
                "replayed": False,
                "order_submission": False,
                "financial_mutation": False,
            }
        results: list[dict[str, Any]] = []
        try:
            current_proposals = _items(
                self._sources.at03_proposals(str(cycle.get("portfolio_id") or ""))
            )
        except Exception as exc:  # noqa: BLE001 - stale approval cannot be bypassed
            raise PortfolioManagementStateError(
                "the persisted AT-03 proposal state could not be revalidated; "
                "no handoff was attempted",
                code="RISK_PROPOSAL_READ_FAILED",
            ) from exc
        for decision in eligible:
            current = self._runtime()
            if self._mode(current) != "autonomous":
                results.append(
                    {
                        "ok": False,
                        "decision_id": decision.get("decision_id"),
                        "proposal_id": _dict(decision.get("handoff")).get("proposal_id"),
                        "error": "RUNTIME_LEFT_AUTONOMOUS",
                        "message": (
                            "The runtime left autonomous mode; no further handoff was attempted."
                        ),
                    }
                )
                break
            if self._kill_switch(current):
                results.append(
                    {
                        "ok": False,
                        "decision_id": decision.get("decision_id"),
                        "proposal_id": _dict(decision.get("handoff")).get("proposal_id"),
                        "error": "KILL_SWITCH_ACTIVE",
                        "message": "The autonomy kill switch became active during the handoff.",
                    }
                )
                break
            proposal_id = _text(_dict(decision.get("handoff")).get("proposal_id"), 160)
            proposal = next(
                (
                    row
                    for row in current_proposals
                    if _text(row.get("proposal_id"), 160) == proposal_id
                ),
                None,
            )
            expiry = _mtime((proposal or {}).get("expires_at"))
            still_approved = bool(
                proposal
                and _text(proposal.get("status"), 40) == "risk_approved"
                and _text(proposal.get("risk_outcome"), 30).lower() == "approved"
                and (expiry is None or expiry > self._now())
            )
            if not still_approved:
                results.append(
                    {
                        "ok": False,
                        "decision_id": decision.get("decision_id"),
                        "proposal_id": proposal_id,
                        "error": "PROPOSAL_NOT_RISK_APPROVED",
                        "message": (
                            "The AT-03 proposal is no longer persisted, risk-approved or "
                            "unexpired; no order was submitted."
                        ),
                    }
                )
                continue
            try:
                called = self._sources.execute_proposal(proposal_id, actor)
                body = await called if inspect.isawaitable(called) else called
                result = _dict(body)
                result.setdefault("proposal_id", proposal_id)
                result["decision_id"] = decision.get("decision_id")
                results.append(result)
            except Exception as exc:  # noqa: BLE001 - one refusal must not hide the rest
                results.append(
                    {
                        "ok": False,
                        "decision_id": decision.get("decision_id"),
                        "proposal_id": proposal_id,
                        "error": getattr(exc, "code", "EXECUTION_REFUSED"),
                        "message": f"{type(exc).__name__}: {exc}",
                    }
                )
        execution_rows = [self._execution_row(row) for row in results]
        status_value = self._handoff_status(execution_rows)
        submitted = any(self._order_submitted(row) for row in execution_rows)
        handoff = {
            "handoff_id": handoff_id,
            "status": status_value,
            "actor": _text(actor, 160),
            "proposal_ids": [_dict(row.get("handoff")).get("proposal_id") for row in eligible],
            "executions": execution_rows,
            "count": len(execution_rows),
            "completed": status_value
            in {
                CycleStatus.EXECUTED.value,
                CycleStatus.PARTIALLY_EXECUTED.value,
                CycleStatus.RECONCILIATION_REQUIRED.value,
                CycleStatus.FAILED.value,
                CycleStatus.RISK_REJECTED.value,
            },
            "order_submission": submitted,
            "financial_mutation": (
                "through existing PI-12/NM-08/NM-10 only" if submitted else False
            ),
            "at": self._now().isoformat(),
        }
        self._finish_handoff(cycle_id, handoff)
        return {
            "ok": True,
            "phase": "PI-21",
            "cycle_id": cycle_id,
            "handoff": handoff,
            "replayed": False,
            "order_submission": submitted,
            "financial_mutation": (
                "through existing PI-12/NM-08/NM-10 only" if submitted else False
            ),
        }

    async def reconcile_cycle(self, cycle_id: str, *, actor: str = "operator") -> dict[str, Any]:
        cycle = self._store.get_cycle(cycle_id)
        handoff = _dict(cycle.get("handoff"))
        executions = _items(handoff.get("executions"))
        if not executions:
            return {
                "ok": True,
                "phase": "PI-21",
                "cycle_id": cycle_id,
                "reconciled": 0,
                "handoff": handoff,
                "reason": "no PI-21 handoff executions are recorded",
                "financial_mutation": False,
            }
        reconciled: list[dict[str, Any]] = []
        for row in executions:
            execution_id = _text(row.get("execution_id"), 160)
            if not execution_id:
                reconciled.append({**row, "reconciliation": "no_execution_id"})
                continue
            try:
                called = self._sources.reconcile_execution(execution_id)
                body = await called if inspect.isawaitable(called) else called
                result = _dict(body)
                reconciled.append({**row, **self._execution_row(result), "reconciled": True})
            except Exception as exc:  # noqa: BLE001 - unresolved is reported explicitly
                reconciled.append(
                    {
                        **row,
                        "reconciled": False,
                        "reconciliation_error": f"{type(exc).__name__}: {exc}",
                    }
                )
        status_value = self._handoff_status(reconciled)
        handoff = {
            **handoff,
            "status": status_value,
            "executions": reconciled,
            "completed": status_value
            in {
                CycleStatus.EXECUTED.value,
                CycleStatus.PARTIALLY_EXECUTED.value,
                CycleStatus.RECONCILIATION_REQUIRED.value,
                CycleStatus.FAILED.value,
                CycleStatus.RISK_REJECTED.value,
            },
            "reconciled_at": self._now().isoformat(),
            "reconciled_by": _text(actor, 160),
            "financial_mutation": False,
            "reconciliation_authority": "NM-10",
        }
        self._finish_handoff(cycle_id, handoff)
        return {
            "ok": True,
            "phase": "PI-21",
            "cycle_id": cycle_id,
            "reconciled": len(reconciled),
            "handoff": handoff,
            "financial_mutation": False,
        }

    async def recover(self, *, reason: str = "PI-21 recovery") -> dict[str, Any]:
        local = self._store.recover(at=self._now())
        autonomy: dict[str, Any] = {}
        errors: dict[str, str] = {}
        callback = self._sources.recover_autonomy
        callback_configured = callback is not None
        if callback is not None:
            try:
                called = callback(reason)
                body = await called if inspect.isawaitable(called) else called
                autonomy = _dict(body)
            except Exception as exc:  # noqa: BLE001 - local recovery still reports
                errors["autonomy_recovery"] = f"{type(exc).__name__}: {exc}"
        runtime = self._runtime()
        return {
            "ok": not errors,
            "phase": "PI-21",
            "reason": _text(reason, 200),
            "management_cycles_interrupted": local["count"],
            "interrupted_cycle_ids": local["interrupted"],
            "autonomy": autonomy,
            "mode": _text(autonomy.get("mode") or self._mode(runtime), 40) or "disabled",
            "mode_verified": bool(callback_configured and not errors),
            "auto_resumed": False,
            "recovery_complete": not errors,
            "errors": errors,
            "paper_only": True,
            "live_trading": False,
            "financial_mutation": False,
        }

    def events(self, cycle_id: str, *, limit: int = 200) -> dict[str, Any]:
        self._store.get_cycle(cycle_id)
        rows = self._store.events(cycle_id, limit=_as_int(limit, 200, 1, 1000))
        return {
            "ok": True,
            "cycle_id": cycle_id,
            "events": rows,
            "total": len(rows),
        }

    # -- internals ----------------------------------------------------------
    def _finish_handoff(self, cycle_id: str, handoff: Mapping[str, Any]) -> None:
        cycle = self._store.get_cycle(cycle_id)
        payload = {
            key: value
            for key, value in cycle.items()
            if key
            not in {
                "idempotency_key",
                "created_at",
                "updated_at",
                "completed_at",
                "config_version",
                "snapshot_version",
            }
        }
        payload["handoff"] = dict(handoff)
        payload["execution_outcome"] = _text(handoff.get("status"), 40)
        history = _items(payload.get("status_history"))
        history.append(
            {
                "status": _text(handoff.get("status"), 40),
                "at": self._now().isoformat(),
                "source": "pi12_handoff",
            }
        )
        payload["status_history"] = history
        self._store.update_cycle_payload(
            cycle_id,
            payload=payload,
            status=_text(handoff.get("status"), 40) or CycleStatus.ORDER_PENDING.value,
        )
        self._store.append_event(
            cycle_id,
            "handoff.updated",
            f"PI-12 handoff is {handoff.get('status')}.",
            payload={"executions": len(_items(handoff.get("executions")))},
            at=self._now(),
        )

    @staticmethod
    def _execution_row(result: Mapping[str, Any]) -> dict[str, Any]:
        execution = _dict(result.get("execution"))
        order = _dict(result.get("order"))
        execution_id = _text(
            result.get("execution_id")
            or execution.get("execution_id")
            or _dict(result.get("intent")).get("execution_id"),
            160,
        )
        order_id = _text(
            execution.get("order_id") or order.get("order_id") or result.get("order_id"),
            160,
        )
        status = _text(
            execution.get("status")
            or order.get("status")
            or result.get("status")
            or ("failed" if result.get("ok") is False else ""),
            40,
        ).lower()
        return {
            "ok": result.get("ok") is not False,
            "decision_id": _text(result.get("decision_id"), 120),
            "proposal_id": _text(result.get("proposal_id"), 160),
            "execution_id": execution_id,
            "order_id": order_id,
            "status": status,
            "filled_quantity": execution.get("filled_quantity") or order.get("filled_quantity"),
            "remaining_quantity": execution.get("remaining_quantity")
            or order.get("remaining_quantity"),
            "average_price": execution.get("average_price") or order.get("average_price"),
            "total_fees": execution.get("total_fees") or order.get("total_fees"),
            "error": _text(result.get("error"), 120),
            "message": _text(result.get("message"), 500),
            "paper_only": True,
            "live_trading": False,
        }

    @staticmethod
    def _handoff_status(executions: Sequence[Mapping[str, Any]]) -> str:
        if not executions:
            return CycleStatus.WAITING_FOR_RISK_ASSESSMENT.value
        statuses = {_text(row.get("status"), 40).lower() for row in executions}
        if statuses & {"reconciliation_required", "unknown"}:
            return CycleStatus.RECONCILIATION_REQUIRED.value
        if statuses & {"submitted", "intent", "forwarding", "open", "accepted"}:
            return CycleStatus.ORDER_PENDING.value
        if statuses & {"partially_filled"}:
            return CycleStatus.PARTIALLY_EXECUTED.value
        if statuses & {"filled"} and statuses & {"failed", "rejected", "cancelled", "expired"}:
            return CycleStatus.PARTIALLY_EXECUTED.value
        if statuses and statuses <= {"filled"}:
            return CycleStatus.EXECUTED.value
        if statuses & {"rejected"}:
            return CycleStatus.RISK_REJECTED.value
        if any(row.get("error") for row in executions):
            return CycleStatus.FAILED.value
        return CycleStatus.ORDER_PENDING.value

    @staticmethod
    def _order_submitted(row: Mapping[str, Any]) -> bool:
        """True only when PI-12/NM-10 returned evidence of an actual submission."""

        if not row.get("ok"):
            return False
        if _text(row.get("execution_id"), 160) or _text(row.get("order_id"), 160):
            return True
        return _text(row.get("status"), 40).lower() in {
            "submitted",
            "intent",
            "forwarding",
            "accepted",
            "open",
            "partially_filled",
            "filled",
        }


__all__ = ["PortfolioManagementActionsMixin"]
