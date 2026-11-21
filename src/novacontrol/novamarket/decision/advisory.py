"""PI-17 advisory handoff as READ-ONLY evidence for the AT-03 decision path.

The multi-agent decision is research evidence.  It is attached to a decision
proposal so an operator can see what the agent coordinator concluded for the same
instrument, exchange and horizon -- it never selects the action, never satisfies
the NM-08 risk gate, never authorises an order and never touches the AT-01
runtime, its kill switch or the durable mode.

Sanitising is deliberately allow-list based: a field the decision path does not
know is dropped, and the authority flags are re-asserted here so a handoff that
claims authority cannot leak it into a proposal.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

ADVISORY_SOURCE = "PI-17 advisory handoff (read-only)"
ADVISORY_PHASE = "PI-17"

#: Fields copied from the handoff, when present.
_COPIED_FIELDS = (
    "phase",
    "cycle_id",
    "decision_id",
    "instrument_id",
    "exchange",
    "horizon",
    "recommendation",
    "evidence_strength",
    "eligible_research_signal",
    "restrictions",
    "authority",
    "execution_path",
    "decided_at",
    "cycle_status",
    "note",
)

#: Flags re-asserted by this layer; a handoff cannot weaken them.
_FORCED_FLAGS: dict[str, Any] = {
    "advisory_only": True,
    "production_authority": False,
    "order_submitted": False,
    "risk_review_required": True,
    "requires_nm08_reassessment": True,
    "requires_pi12_orchestration": True,
    "requires_operator_permission": True,
}


def _text(value: object) -> str:
    return str(value or "").strip()


def advisory_evidence(
    handoff: Mapping[str, Any] | None,
    *,
    instrument_id: str,
    exchange: str = "",
    horizon: str = "",
) -> dict[str, Any] | None:
    """Sanitise one PI-17 handoff into proposal evidence, or ``None``.

    The handoff must be for the same instrument; an exchange or horizon that is
    known on both sides must also agree.  A missing recommendation is not usable
    evidence.
    """

    if not isinstance(handoff, Mapping):
        return None
    instrument = _text(instrument_id).upper()
    handoff_instrument = _text(handoff.get("instrument_id")).upper()
    if not instrument or handoff_instrument != instrument:
        return None
    wanted_exchange = _text(exchange).upper()
    handoff_exchange = _text(handoff.get("exchange")).upper()
    if wanted_exchange and handoff_exchange and wanted_exchange != handoff_exchange:
        return None
    wanted_horizon = _text(horizon).lower()
    handoff_horizon = _text(handoff.get("horizon")).lower()
    if wanted_horizon and handoff_horizon and wanted_horizon != handoff_horizon:
        return None
    recommendation = _text(handoff.get("recommendation")).lower()
    if not recommendation:
        return None
    body: dict[str, Any] = {
        "source": ADVISORY_SOURCE,
        "is_advisory_evidence": True,
        "read_only": True,
        "affects_action": False,
        "recommendation": recommendation,
    }
    for field in _COPIED_FIELDS:
        if field in handoff:
            value = handoff[field]
            body[field] = dict(value) if isinstance(value, Mapping) else value
    body.update(_FORCED_FLAGS)
    body["phase"] = ADVISORY_PHASE
    return body


class MultiAgentAdvisoryEvidence:
    """Read-only reader that turns a PI-17 handoff into proposal evidence.

    ``service_provider`` is opened lazily, so a decision pipeline that never
    reaches a proposal never starts the multi-agent service.  Errors are raised
    to the caller; the pipeline records them and carries on without evidence.
    """

    def __init__(self, service_provider: Callable[[], Any]) -> None:
        self._service_provider = service_provider

    def latest(
        self, *, instrument_id: str, exchange: str = "", horizon: str = ""
    ) -> dict[str, Any] | None:
        service = self._service_provider()
        handoff = service.latest_handoff(
            instrument_id=instrument_id, exchange=exchange, horizon=horizon
        )
        return advisory_evidence(
            handoff, instrument_id=instrument_id, exchange=exchange, horizon=horizon
        )

    def __call__(self, signal: Any) -> dict[str, Any] | None:
        return self.latest(
            instrument_id=_text(getattr(signal, "instrument_id", "")),
            exchange=_text(getattr(signal, "exchange", "")),
            horizon=_text(getattr(signal, "horizon", "")),
        )


__all__ = [
    "ADVISORY_PHASE",
    "ADVISORY_SOURCE",
    "MultiAgentAdvisoryEvidence",
    "advisory_evidence",
]
