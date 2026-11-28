"""Explainable, deterministic lead qualification.

No model is consulted here. A score is a weighted summary of criteria a person
or system recorded, and a score alone never becomes verified purchase intent:
intent is only marked verified when the intent input itself carries evidence and
an explicit verification flag.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from novacontrol.customer.models import LeadStatus, new_id, utcnow


def _text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _score_input(value: Any) -> tuple[float | None, str, bool]:
    """Return (score 0..5, evidence, verified) from a flexible criterion input."""
    if isinstance(value, Mapping):
        raw = value.get("score")
        evidence = _text(value.get("evidence"))
        verified = bool(value.get("verified") is True and evidence)
    else:
        raw = value
        evidence = ""
        verified = False
    if raw is None or isinstance(raw, bool):
        return None, evidence, verified
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return None, evidence, verified
    if not 0.0 <= number <= 5.0:
        raise ValueError("A qualification criterion score must be between 0 and 5.")
    return number, evidence, verified


def qualify_lead(
    lead: Mapping[str, Any],
    criteria: Sequence[Mapping[str, Any]],
    supplied: Mapping[str, Any] | None = None,
    *,
    qualified_at: float = 0.60,
    unqualified_at: float = 0.35,
) -> dict[str, Any]:
    """Score one lead and return the explainable result.

    ``criteria`` is the workspace's configurable criterion table; ``supplied``
    contains only what was actually observed for this lead. Missing criteria are
    listed as missing information instead of being filled with a guess.
    """
    inputs = dict(lead.get("qualification_inputs") or {})
    inputs.update(dict(supplied or {}))
    rows: list[dict[str, Any]] = []
    missing: list[str] = []
    reasons: list[str] = []
    weighted = 0.0
    weight_total = 0.0
    intent_verified = False
    for criterion in criteria:
        criterion_id = _text(criterion.get("id"))
        if not criterion_id:
            continue
        name = _text(criterion.get("name")) or criterion_id
        weight = max(0.0, float(criterion.get("weight", 0.0)))
        score, evidence, verified = _score_input(inputs.get(criterion_id))
        required = bool(criterion.get("required", False))
        row = {
            "id": criterion_id,
            "name": name,
            "weight": round(weight, 4),
            "score": None if score is None else round(score, 2),
            "evidence": evidence,
            "verified": verified,
            "required": required,
            "missing": score is None,
        }
        rows.append(row)
        if score is None:
            missing.append(name)
            reasons.append(f"{name} has not been recorded.")
            continue
        weighted += (score / 5.0) * weight
        weight_total += weight
        if criterion_id == "purchase_intent" and verified and evidence:
            intent_verified = True
        if score >= 3.0:
            reasons.append(f"{name}: positive signal ({score:g}/5).")
        elif score <= 1.0:
            reasons.append(f"{name}: weak signal ({score:g}/5).")
        else:
            reasons.append(f"{name}: mixed signal ({score:g}/5).")
    ratio = round(weighted / weight_total, 4) if weight_total else 0.0
    required_missing = [
        row["name"] for row in rows if row["required"] and row["missing"]
    ]
    disqualification = _text(lead.get("disqualification_reason"))
    if disqualification:
        reasons.append(f"Recorded disqualification reason: {disqualification}")
    if disqualification:
        recommended = LeadStatus.UNQUALIFIED.value
        reasons.append("A recorded disqualification overrides the positive score.")
    elif required_missing:
        recommended = LeadStatus.RESEARCHING.value
        reasons.append("Required evidence is still missing; qualification is pending.")
    elif weight_total == 0.0:
        recommended = LeadStatus.RESEARCHING.value
        reasons.append("No criterion has been scored; the lead is not qualified.")
    elif ratio >= qualified_at:
        recommended = LeadStatus.QUALIFIED.value
    elif ratio <= unqualified_at:
        recommended = LeadStatus.UNQUALIFIED.value
    else:
        recommended = LeadStatus.RESEARCHING.value
    if lead.get("consent_status") == "withdrawn":
        recommended = LeadStatus.UNQUALIFIED.value
        reasons.append("Communication consent was withdrawn.")
    result = {
        "id": new_id("qualification"),
        "lead_id": str(lead.get("id", "")),
        "score": ratio,
        "scale": "weighted_0_to_1",
        "criteria": rows,
        "missing_information": missing,
        "required_missing": required_missing,
        "reasons": reasons,
        "recommended_status": recommended,
        "purchase_intent_verified": intent_verified,
        "verified_purchase_intent_note": (
            "Purchase intent is verified by recorded evidence."
            if intent_verified
            else "This score is not verified purchase intent; intent evidence is missing."
        ),
        "automated_recommendation": True,
        "generated_by": "deterministic_qualification",
        "created_at": utcnow().isoformat(),
    }
    if intent_verified:
        for criterion in rows:
            if criterion["id"] == "purchase_intent":
                result["intent_evidence"] = criterion["evidence"]
    return result


__all__ = ["qualify_lead"]
