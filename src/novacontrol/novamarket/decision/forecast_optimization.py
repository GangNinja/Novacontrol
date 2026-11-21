"""PI-23 forecast-optimization evidence as READ-ONLY decision evidence.

PI-23 answers two questions for a scope: *which* model's forecast should be
preferred (selection), *how far* that model can be trusted (reliability) and
which forecast was actually usable (fallback/abstention).  Those records used to
be reachable only through the PI-23 HTTP routes, so the decision path could not
see them at all.

This module makes them consumable by attaching the latest records for the same
instrument and horizon to a decision proposal.  It is evidence, never authority:

* the evidence never selects the action, the side or the notional;
* NM-08 remains the only risk authority and NM-10 the only paper-execution
  authority -- both are re-asserted on the evidence itself;
* it cannot satisfy, weaken or bypass the NM-08 risk gate, authorise an order,
  or let a decision run outside the AT-01 runtime mode and lease.

Sanitising is allow-list based.  A field this layer does not know is dropped, so
a PI-23 record that claims authority cannot leak it into a proposal.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

FORECAST_OPTIMIZATION_SOURCE = "PI-23 forecast optimization evidence (read-only)"
FORECAST_OPTIMIZATION_PHASE = "PI-23"

#: Reasons/rationale are operator-facing text, not a transcript.
_MAX_LINES = 6

#: Flags re-asserted by this layer; a PI-23 record cannot weaken them.
_FORCED_FLAGS: dict[str, Any] = {
    "read_only": True,
    "advisory_only": True,
    "affects_action": False,
    "order_submitted": False,
    "new_forecast_created": False,
    "risk_authority": "NM-08",
    "execution_authority": "NM-10",
    "requires_nm08_reassessment": True,
    "requires_operator_permission": True,
}


def _text(value: object) -> str:
    return str(value if value is not None else "").strip()


def _lines(value: object) -> list[str]:
    """Bound a reason/rationale list to short operator-facing strings."""

    if isinstance(value, str):
        text = value.strip()
        return [text[:300]] if text else []
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
        return []
    body: list[str] = []
    for item in value[:_MAX_LINES]:
        text = _text(item)
        if text:
            body.append(text[:300])
    return body


def _scope_matches(
    record: Mapping[str, Any], *, instrument_id: str, horizon: str
) -> bool:
    """A record is usable only for the scope the decision asked about."""

    record_instrument = _text(record.get("instrument_id")).upper()
    if record_instrument and record_instrument != instrument_id.upper():
        return False
    record_horizon = _text(record.get("horizon")).lower()
    if horizon and record_horizon and record_horizon != horizon.lower():
        return False
    return True


def _selection_evidence(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "selection_id": _text(record.get("selection_id")),
        "created_at": _text(record.get("created_at")),
        "outcome": _text(record.get("outcome")).lower(),
        "selected_model_id": _text(record.get("selected_model_id")),
        "selected_version": _text(record.get("selected_version")),
        "sample_count": record.get("sample_count"),
        "selection_policy_version": _text(record.get("selection_policy_version")),
        "test_used_for_selection": bool(record.get("test_used_for_selection")),
        "calibration_status": _text(record.get("calibration_status")),
        "rationale": _lines(record.get("rationale")),
    }


def _reliability_evidence(
    record: Mapping[str, Any], *, model_id: str
) -> dict[str, Any] | None:
    """The assessment for the selected model, never a composite over models."""

    assessments = record.get("assessments")
    if not isinstance(assessments, Sequence):
        return None
    for item in assessments:
        if not isinstance(item, Mapping):
            continue
        if model_id and _text(item.get("model_id")) != model_id:
            continue
        return {
            "reliability_id": _text(item.get("reliability_id")),
            "assessment_id": _text(record.get("reliability_assessment_id")),
            "policy_version": _text(record.get("policy_version")),
            "model_id": _text(item.get("model_id")),
            "classification": _text(item.get("classification")),
            "health": _text(item.get("health")),
            "drift_detected": bool(item.get("drift_detected")),
            "calibration_status": _text(item.get("calibration_status")),
            "sample_count": item.get("sample_count"),
            "numeric_score": None,
        }
    return None


def _fallback_evidence(record: Mapping[str, Any]) -> dict[str, Any]:
    selected = record.get("selected")
    selected_body = selected if isinstance(selected, Mapping) else {}
    return {
        "fallback_id": _text(record.get("fallback_id")),
        "created_at": _text(record.get("created_at")),
        "outcome": _text(record.get("outcome")).lower(),
        "abstained": bool(record.get("abstained")),
        "preferred_model_id": _text(record.get("preferred_model_id")),
        "selected_model_id": _text(selected_body.get("model_id")),
        "selected_forecast_id": _text(selected_body.get("forecast_id")),
        "reasons": _lines(record.get("reasons")),
    }


def forecast_optimization_evidence(
    selection: Mapping[str, Any] | None = None,
    reliability: Mapping[str, Any] | None = None,
    fallback: Mapping[str, Any] | None = None,
    *,
    instrument_id: str,
    horizon: str = "",
) -> dict[str, Any] | None:
    """Sanitise PI-23 records into one evidence block, or ``None``.

    Every supplied record must belong to the decision's instrument (and its
    horizon when both sides know one).  A record for another scope is dropped
    rather than blended, so a 5d selection can never be attached to a 1h signal.
    """

    instrument = _text(instrument_id).upper()
    if not instrument:
        return None
    body: dict[str, Any] = {
        "source": FORECAST_OPTIMIZATION_SOURCE,
        "phase": FORECAST_OPTIMIZATION_PHASE,
        "is_advisory_evidence": True,
        "instrument_id": instrument,
        "horizon": _text(horizon).lower(),
    }
    available: list[str] = []
    selected_model = ""
    if isinstance(selection, Mapping) and _scope_matches(
        selection, instrument_id=instrument, horizon=horizon
    ):
        selection_body = _selection_evidence(selection)
        body["selection"] = selection_body
        selected_model = selection_body["selected_model_id"]
        available.append("selection")
    if isinstance(reliability, Mapping) and _scope_matches(
        reliability, instrument_id=instrument, horizon=horizon
    ):
        reliability_body = _reliability_evidence(reliability, model_id=selected_model)
        if reliability_body is not None:
            body["reliability"] = reliability_body
            available.append("reliability")
    if isinstance(fallback, Mapping) and _scope_matches(
        fallback, instrument_id=instrument, horizon=horizon
    ):
        body["fallback"] = _fallback_evidence(fallback)
        available.append("fallback")
    if not available:
        return None
    body["available"] = available
    body.update(_FORCED_FLAGS)
    return body


class ForecastOptimizationEvidence:
    """Read-only reader that turns PI-23 records into proposal evidence.

    ``service_provider`` is opened lazily, so a decision that never reaches a
    proposal never starts the PI-23 service.  Errors are raised to the caller;
    the pipeline records them and carries on without evidence.
    """

    def __init__(self, service_provider: Callable[[], Any]) -> None:
        self._service_provider = service_provider

    @staticmethod
    def _latest(service: Any, kind: str, *, instrument_id: str, horizon: str) -> Any:
        body = service.history(
            kind=kind, instrument_id=instrument_id, horizon=horizon, limit=1
        )
        records = body.get("records") if isinstance(body, Mapping) else None
        if not records:
            return None
        first = records[0]
        return first if isinstance(first, Mapping) else None

    def latest(
        self, *, instrument_id: str, horizon: str = ""
    ) -> dict[str, Any] | None:
        service = self._service_provider()
        return forecast_optimization_evidence(
            self._latest(
                service, "selection", instrument_id=instrument_id, horizon=horizon
            ),
            self._latest(
                service, "reliability", instrument_id=instrument_id, horizon=horizon
            ),
            self._latest(
                service, "fallback", instrument_id=instrument_id, horizon=horizon
            ),
            instrument_id=instrument_id,
            horizon=horizon,
        )

    def __call__(self, signal: Any) -> dict[str, Any] | None:
        return self.latest(
            instrument_id=_text(getattr(signal, "instrument_id", "")),
            horizon=_text(getattr(signal, "horizon", "")),
        )


__all__ = [
    "FORECAST_OPTIMIZATION_PHASE",
    "FORECAST_OPTIMIZATION_SOURCE",
    "ForecastOptimizationEvidence",
    "forecast_optimization_evidence",
]
