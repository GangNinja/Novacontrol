"""Evidence verification, company-profile assembly, news classification and claims."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from typing import Any

from .models import (
    CompanyProfile,
    EvidenceKind,
    EvidenceRecord,
    ProfileField,
    Reliability,
    ResearchValidationError,
    SourceTier,
    VerificationStatus,
    clean,
    digest_payload,
    stable_id,
)

_PROFILE_FIELDS = (
    "name",
    "symbol",
    "exchange",
    "sector",
    "industry",
    "description",
    "products",
    "segments",
    "geographies",
    "business_model",
    "competitive_positioning",
    "customers_suppliers",
    "management",
    "risks",
)
_FIELD_ALIASES = {
    "product": "products",
    "service": "products",
    "services": "products",
    "segment": "segments",
    "geography": "geographies",
    "geographic_exposure": "geographies",
    "customer": "customers_suppliers",
    "supplier": "customers_suppliers",
    "customers": "customers_suppliers",
    "suppliers": "customers_suppliers",
    "risk": "risks",
}
_POSITIVE = {
    "accelerate",
    "accelerated",
    "beat",
    "beats",
    "best",
    "better",
    "buyback",
    "expansion",
    "grew",
    "growth",
    "improve",
    "improved",
    "increased",
    "outperform",
    "profit",
    "record",
    "recovery",
    "strong",
    "surge",
    "surpassed",
    "upgrade",
    "wins",
    "win",
}
_NEGATIVE = {
    "decline",
    "declined",
    "decrease",
    "decreased",
    "downgrade",
    "fall",
    "fell",
    "fraud",
    "impairment",
    "lawsuit",
    "loss",
    "miss",
    "missed",
    "penalty",
    "probe",
    "recall",
    "resign",
    "resigned",
    "risk",
    "sinks",
    "slump",
    "weak",
    "worse",
    "writedown",
}
_UNCERTAIN = {
    "could",
    "may",
    "might",
    "possibly",
    "reportedly",
    "rumor",
    "unconfirmed",
    "uncertain",
    "unknown",
    "would",
}
_NEGATORS = {"no", "not", "never", "without", "neither", "nor"}
_CONFLICT_TOLERANCE = 1e-6


def _normalise_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.lower()).strip()


def _is_ai(record: EvidenceRecord) -> bool:
    return record.is_ai_interpretation or record.source_tier == SourceTier.DERIVED_MODEL.value


def _field_name(raw: object) -> str:
    key = clean(raw, limit=80).lower().replace(" ", "_").replace("-", "_")
    return _FIELD_ALIASES.get(key, key)


def verify_evidence(
    records: Sequence[EvidenceRecord],
    *,
    as_of: datetime,
    cutoff: datetime,
    max_age_hours: float,
    allowed_sources: Sequence[str] = (),
) -> tuple[list[EvidenceRecord], list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Return point-in-time evidence, claim records, conflicts and warnings.

    Verification never promotes a model interpretation or a single secondary
    article into an official fact.  Contradictory official records remain
    contradictory instead of being averaged away.
    """
    allowed = {item.lower() for item in allowed_sources}
    retained: list[EvidenceRecord] = []
    excluded: list[dict[str, Any]] = []
    warnings: list[str] = []
    for record in records:
        reason = ""
        if record.published_at is None:
            reason = "missing publication timestamp"
        elif record.published_at > cutoff:
            reason = "published after the research cutoff"
        elif record.retrieved_at > as_of and record.published_at > as_of:
            reason = "evidence was not available at the research time"
        elif allowed and record.source_id.lower() not in allowed:
            reason = "source is not in the configured allowlist"
        if reason:
            excluded.append({"evidence_id": record.evidence_id, "reason": reason})
            continue
        retained.append(record)
        if record.instruction_risk:
            warnings.append(
                f"{record.evidence_id}: embedded instruction-like text was retained as "
                "untrusted evidence and never treated as an instruction"
            )
    claim_groups: dict[str, list[EvidenceRecord]] = defaultdict(list)
    for record in retained:
        key = clean(record.metadata.get("claim_key"), limit=120)
        if not key and record.kind == EvidenceKind.COMPANY_PROFILE.value:
            # Two sourced values for the same profile field are competing
            # claims even when the caller did not supply an explicit claim_key.
            raw_field = record.metadata.get("field") or record.metadata.get("profile_field")
            field = _field_name(raw_field)
            key = f"profile_field:{field}" if field else ""
        if key:
            claim_groups[f"{record.kind}:{key}"].append(record)
    conflicting: set[str] = set()
    for records_for_key in claim_groups.values():
        numeric = [
            float(record.value)
            for record in records_for_key
            if record.value is not None and not _is_ai(record)
        ]
        if len(numeric) >= 2 and max(numeric) - min(numeric) > _CONFLICT_TOLERANCE:
            conflicting.update(record.evidence_id for record in records_for_key)
        if len(records_for_key) >= 2:
            texts = {
                _normalise_text(record.claim)
                for record in records_for_key
                if record.claim and not _is_ai(record)
            }
            if len(texts) > 1 and any(
                record.source_tier == SourceTier.PRIMARY_OFFICIAL.value
                for record in records_for_key
            ):
                conflicting.update(record.evidence_id for record in records_for_key)
    verified_independent: dict[str, set[str]] = defaultdict(set)
    for record in retained:
        if (
            record.source_tier == SourceTier.INDEPENDENT.value
            and not _is_ai(record)
            and record.published_at is not None
        ):
            verified_independent[_normalise_text(record.claim)].add(record.source_id)
    verified: list[EvidenceRecord] = []
    claims: list[dict[str, Any]] = []
    for record in retained:
        status = record.verification
        age_hours = (
            max(0.0, (as_of - record.published_at).total_seconds() / 3600.0)
            if record.published_at
            else None
        )
        if record.evidence_id in conflicting:
            status = VerificationStatus.CONFLICTING_EVIDENCE.value
        elif record.instruction_risk or _is_ai(record) or record.published_at is None:
            status = VerificationStatus.UNVERIFIED.value
        elif age_hours is not None and age_hours > max_age_hours:
            status = VerificationStatus.STALE.value
        elif record.source_tier == SourceTier.PRIMARY_OFFICIAL.value:
            status = VerificationStatus.VERIFIED.value
        elif record.source_tier == SourceTier.OFFICIAL_SECONDARY.value:
            status = VerificationStatus.PARTIALLY_VERIFIED.value
        elif record.source_tier == SourceTier.INDEPENDENT.value:
            corroboration = verified_independent[_normalise_text(record.claim)]
            status = (
                VerificationStatus.VERIFIED.value
                if len(corroboration) >= 2
                else VerificationStatus.PARTIALLY_VERIFIED.value
            )
        elif record.source_tier in {
            SourceTier.OPINION.value,
            SourceTier.COMMENTARY.value,
            SourceTier.RUMOR.value,
            SourceTier.UNKNOWN.value,
        }:
            status = VerificationStatus.UNVERIFIED.value
        updated = replace(record, verification=status)
        verified.append(updated)
        claims.append(
            {
                "claim_id": "prc_" + digest_payload(record.to_dict())[:20],
                "claim": record.claim,
                "kind": record.kind,
                "evidence_id": record.evidence_id,
                "source_id": record.source_id,
                "source_name": record.source_name,
                "source_tier": record.source_tier,
                "publication_time": (
                    record.published_at.isoformat() if record.published_at else None
                ),
                "retrieval_time": record.retrieved_at.isoformat(),
                "status": status,
                "reliability": record.reliability,
                "instrument_id": record.instrument_id,
                "content_hash": record.content_hash,
                "is_ai_interpretation": _is_ai(record),
                "instruction_risk": record.instruction_risk,
            }
        )
    return verified, claims, excluded, warnings


def news_sentiment(text: str) -> tuple[str, float, tuple[str, ...]]:
    """Lexicon sentiment, explicitly not a price-direction or probability claim."""
    tokens = re.findall(r"[a-z']+", text.lower())
    positive: list[str] = []
    negative: list[str] = []
    uncertain: list[str] = []
    for index, token in enumerate(tokens):
        if token in _POSITIVE:
            previous = tokens[index - 1] if index else ""
            if previous in _NEGATORS:
                negative.append(f"not {token}")
            else:
                positive.append(token)
        elif token in _NEGATIVE:
            previous = tokens[index - 1] if index else ""
            if previous in _NEGATORS:
                positive.append(f"not {token}")
            else:
                negative.append(token)
        elif token in _UNCERTAIN:
            uncertain.append(token)
    if not positive and not negative:
        label = "uncertain" if uncertain else "neutral"
        return label, 0.0, tuple(dict.fromkeys(uncertain))
    if positive and negative:
        label = (
            "mixed"
            if abs(len(positive) - len(negative)) <= 1
            else ("positive" if len(positive) > len(negative) else "negative")
        )
    else:
        label = "positive" if positive else "negative"
    score = (len(positive) - len(negative)) / max(1, len(positive) + len(negative))
    return label, max(-1.0, min(1.0, score)), tuple(dict.fromkeys(positive + negative + uncertain))


def enrich_news(
    articles: Sequence[Any],
    *,
    as_of: datetime,
    cutoff: datetime,
    max_age_hours: float,
) -> tuple[list[Any], list[str], list[str]]:
    retained: list[Any] = []
    warnings: list[str] = []
    excluded: list[str] = []
    for article in articles:
        if article.published_at is None:
            excluded.append(f"{article.headline}: missing publication timestamp")
            continue
        if article.published_at > cutoff:
            excluded.append(f"{article.headline}: published after cutoff")
            continue
        age_hours = max(0.0, (as_of - article.published_at).total_seconds() / 3600.0)
        if age_hours > max_age_hours:
            excluded.append(f"{article.headline}: stale by configured evidence limit")
            continue
        text = f"{article.headline} {article.summary}"
        label, score, basis = news_sentiment(text)
        if article.is_ai_interpretation or article.source_tier == SourceTier.DERIVED_MODEL.value:
            verification = VerificationStatus.UNVERIFIED.value
        elif article.source_tier == SourceTier.PRIMARY_OFFICIAL.value:
            verification = VerificationStatus.VERIFIED.value
        elif article.source_tier in {
            SourceTier.RUMOR.value,
            SourceTier.OPINION.value,
            SourceTier.UNKNOWN.value,
        }:
            verification = VerificationStatus.UNVERIFIED.value
        else:
            verification = VerificationStatus.PARTIALLY_VERIFIED.value
        if article.instruction_risk:
            verification = VerificationStatus.UNVERIFIED.value
            warnings.append(
                f"{article.headline}: instruction-like content was treated as data and not executed"
            )
        retained.append(
            replace(
                article,
                sentiment=article.sentiment if article.sentiment != "uncertain" else label,
                sentiment_score=article.sentiment_score
                if article.sentiment_score is not None
                else score,
                sentiment_basis=article.sentiment_basis or basis,
                verification=verification,
            )
        )
    retained.sort(key=lambda item: item.published_at or as_of, reverse=True)
    return retained, warnings, excluded


def build_company_profile(
    *,
    instrument_id: str,
    identity: Mapping[str, Any],
    evidence: Sequence[EvidenceRecord],
    as_of: datetime,
    cutoff: datetime,
) -> CompanyProfile:
    """Assemble profile fields only from timestamped evidence or registry identity."""
    field_candidates: dict[str, list[EvidenceRecord]] = defaultdict(list)
    for record in evidence:
        if record.published_at is None or record.published_at > cutoff:
            continue
        if record.kind != EvidenceKind.COMPANY_PROFILE.value:
            continue
        if record.instruction_risk or record.is_ai_interpretation:
            continue
        field = _field_name(record.metadata.get("field") or record.metadata.get("profile_field"))
        if field not in _PROFILE_FIELDS:
            continue
        field_candidates[field].append(record)
    fields: dict[str, ProfileField] = {}
    missing: list[str] = []
    warnings: list[str] = []
    for field in _PROFILE_FIELDS:
        candidates = field_candidates.get(field, [])
        candidates.sort(key=lambda item: (item.published_at or as_of, item.retrieved_at))
        if not candidates:
            if field in identity and identity[field] not in (None, ""):
                value = identity[field]
                fields[field] = ProfileField(
                    name=field,
                    value=value,
                    evidence_ids=(),
                    published_at=as_of,
                    verification=VerificationStatus.PARTIALLY_VERIFIED.value,
                    note="NM-02 instrument registry identity; no primary filing linked",
                )
            else:
                missing.append(field)
            continue
        latest = candidates[-1]
        values = {
            _clip_text(item.metadata.get("value") or item.claim, 500)
            for item in candidates
            if (item.metadata.get("value") or item.claim)
        }
        if len(values) > 1 and any(
            item.source_tier == SourceTier.PRIMARY_OFFICIAL.value for item in candidates
        ):
            fields[field] = ProfileField(
                name=field,
                value=None,
                evidence_ids=tuple(item.evidence_id for item in candidates),
                published_at=latest.published_at,
                verification=VerificationStatus.CONFLICTING_EVIDENCE.value,
                note="sources disagree; conflicting values are preserved in the evidence explorer",
            )
            warnings.append(f"{field}: conflicting sourced values were not silently resolved")
            continue
        value = latest.metadata.get("value") or latest.claim
        if isinstance(value, (list, tuple)):
            field_value: str | tuple[str, ...] = tuple(clean(item, limit=300) for item in value)
        else:
            field_value = _clip_text(value, 500)
        fields[field] = ProfileField(
            name=field,
            value=field_value,
            evidence_ids=(latest.evidence_id,),
            published_at=latest.published_at,
            verification=latest.verification,
            note=clean(latest.metadata.get("note"), limit=300),
        )
    symbol = str(identity.get("symbol") or instrument_id.split(":", 1)[-1])
    exchange = str(identity.get("exchange") or instrument_id.split(":", 1)[0])
    name = _profile_value(fields.get("name")) or str(identity.get("name") or symbol)
    sector = _profile_value(fields.get("sector")) or str(identity.get("sector") or "unknown")
    industry = _profile_value(fields.get("industry")) or str(identity.get("industry") or "unknown")
    description = _profile_value(fields.get("description")) or ""
    return CompanyProfile(
        instrument_id=instrument_id,
        symbol=symbol,
        exchange=exchange,
        name=name,
        sector=str(sector).lower(),
        industry=str(industry).lower(),
        description=str(description),
        as_of=as_of,
        fields=fields,
        missing_fields=tuple(missing),
        warnings=tuple(warnings),
    )


def _profile_value(field: ProfileField | None) -> Any:
    return field.value if field else None


def _clip_text(value: object, limit: int = 500) -> str:
    """Trim to a word boundary and mark the trim.

    A hard ``[:500]`` cut ended a published description mid-word ("...wind
    turbine generators; c"), which reads as corrupted data rather than as a
    deliberate excerpt.
    """

    text = " ".join(str(value if value is not None else "").split())
    if len(text) <= limit:
        return text
    clipped = text[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:-–")
    if not clipped:
        clipped = text[: limit - 1].rstrip()
    return f"{clipped}…"


def evidence_is_verified(record: EvidenceRecord) -> bool:
    return record.verification == VerificationStatus.VERIFIED.value


def reliable_source(record: EvidenceRecord) -> bool:
    return record.reliability in {Reliability.HIGH.value, Reliability.MEDIUM.value}


def claim_provenance(records: Sequence[EvidenceRecord]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for record in records:
        if record.content_hash in seen:
            continue
        seen.add(record.content_hash)
        result.append(
            {
                "evidence_id": record.evidence_id,
                "source_id": record.source_id,
                "source_name": record.source_name,
                "source_url": record.source_url,
                "published_at": (record.published_at.isoformat() if record.published_at else None),
                "verification": record.verification,
                "content_hash": record.content_hash,
            }
        )
    return result


def make_claim(
    *,
    instrument_id: str,
    claim: str,
    evidence_ids: Sequence[str],
    kind: str,
    status: str,
) -> dict[str, Any]:
    if not evidence_ids:
        raise ResearchValidationError("a material claim needs at least one evidence reference")
    return {
        "claim_id": stable_id("prc_", instrument_id, claim, *evidence_ids),
        "instrument_id": instrument_id,
        "claim": clean(claim, limit=2_000),
        "kind": kind,
        "evidence_ids": list(evidence_ids),
        "verification": status,
        "is_ai_interpretation": False,
    }
