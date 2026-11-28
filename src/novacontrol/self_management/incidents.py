"""Durable incident lifecycle and evidence history.

An incident is a real operational problem, not every health row.  The manager
deduplicates by a stable fingerprint, preserves older resolved incidents as
recurrence evidence, and only notifies when a NEW incident appears or its
severity rises.  That is what prevents one unresolved fault from producing a
notification on every monitoring pass.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from novacontrol.self_management.models import (
    CauseConfidence,
    Diagnosis,
    HealthSnapshot,
    Incident,
    IncidentSeverity,
    IncidentState,
    RecoveryAttempt,
    RecoveryStatus,
    checksum,
    new_id,
    utcnow,
)

Publisher = Callable[..., None]

_ALLOWED_TRANSITIONS: Mapping[IncidentState, frozenset[IncidentState]] = {
    IncidentState.DETECTED: frozenset(
        {IncidentState.INVESTIGATING, IncidentState.DIAGNOSED, IncidentState.UNRESOLVED}
    ),
    IncidentState.INVESTIGATING: frozenset(
        {IncidentState.DIAGNOSED, IncidentState.UNRESOLVED}
    ),
    IncidentState.DIAGNOSED: frozenset(
        {
            IncidentState.RECOVERY_PENDING,
            IncidentState.UNRESOLVED,
            IncidentState.ESCALATED,
        }
    ),
    IncidentState.RECOVERY_PENDING: frozenset(
        {
            IncidentState.RECOVERING,
            IncidentState.ESCALATED,
            IncidentState.RESOLVED,
            IncidentState.UNRESOLVED,
        }
    ),
    IncidentState.RECOVERING: frozenset(
        {
            IncidentState.RESOLVED,
            IncidentState.ESCALATED,
            IncidentState.UNRESOLVED,
            IncidentState.RECOVERY_PENDING,
        }
    ),
    IncidentState.UNRESOLVED: frozenset(
        {IncidentState.INVESTIGATING, IncidentState.RECOVERY_PENDING, IncidentState.ESCALATED}
    ),
    IncidentState.ESCALATED: frozenset(
        {IncidentState.INVESTIGATING, IncidentState.RECOVERY_PENDING, IncidentState.RESOLVED}
    ),
    IncidentState.RESOLVED: frozenset(),
}


class InvalidIncidentTransition(ValueError):
    pass


class IncidentManager:
    """Owns the incident records; it never performs a recovery action itself."""

    def __init__(
        self,
        *,
        store: Any | None = None,
        publisher: Publisher | None = None,
        clock: Callable[[], datetime] | None = None,
        redactor: Callable[[Any], Any] | None = None,
        limit: int = 200,
    ) -> None:
        self.store = store
        self._publish = publisher
        self._clock = clock or utcnow
        self._redactor = redactor
        self.limit = max(20, int(limit))
        self._incidents: list[Incident] = []
        self._load()

    # -- persistence ---------------------------------------------------------
    def _load(self) -> None:
        if self.store is None:
            return
        payload = self.store.read("self_management")
        rows = payload.get("incidents", ()) if isinstance(payload, Mapping) else ()
        loaded: list[Incident] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            try:
                loaded.append(Incident.from_dict(row))
            except (KeyError, TypeError, ValueError):
                # A damaged row is skipped, never turned into a fabricated one.
                continue
        self._incidents = loaded[-self.limit :]

    def _save(self) -> None:
        if self.store is None:
            return
        try:
            self.store.write(
                "self_management",
                {
                    "schema": "phase34.1",
                    "incidents": [row.to_dict() for row in self._incidents[-self.limit :]],
                },
            )
        except Exception:
            # The state store is already forgiving; a failure here must not
            # break the health pass that found the problem.
            return

    def _safe_value(self, value: Any) -> Any:
        if self._redactor is None:
            return value
        try:
            return self._redactor(value)
        except Exception:
            return value

    def _emit(self, type_: str, **payload: Any) -> None:
        if self._publish is None:
            return
        try:
            self._publish(type_, **payload)
        except Exception:
            return

    # -- observation ---------------------------------------------------------
    def observe(
        self, diagnoses: Sequence[Diagnosis], snapshot: HealthSnapshot | None = None
    ) -> tuple[Incident, ...]:
        """Open or update one incident per diagnosis; returns changed incidents."""
        changed: list[Incident] = []
        for diagnosis in diagnoses:
            current = self._open_by_fingerprint(diagnosis.fingerprint)
            if current is None:
                incident = self._create(diagnosis, snapshot)
                self._incidents.append(incident)
                self._notify_new(incident)
                changed.append(incident)
                continue
            updated = self._update(current, diagnosis, snapshot)
            if updated != current:
                self._replace(updated)
                changed.append(updated)
                if diagnosis.severity.rank > current.severity.rank:
                    self._notify_escalated(updated)
        if changed:
            self._save()
        return tuple(changed)

    def _open_by_fingerprint(self, fingerprint: str) -> Incident | None:
        # ESCALATED is not "open" for the operator queue, but the same live
        # failure must keep updating that record: treating it as absent opened a
        # brand-new incident every monitoring pass while the fault persisted.
        for incident in reversed(self._incidents):
            if (
                incident.fingerprint == fingerprint
                and incident.state is not IncidentState.RESOLVED
            ):
                return incident
        return None

    def _create(
        self, diagnosis: Diagnosis, snapshot: HealthSnapshot | None = None
    ) -> Incident:
        now = self._clock()
        related = [
            row.id
            for row in self._incidents
            if row.fingerprint == diagnosis.fingerprint and not row.open
        ][-5:]
        recurrence = 0
        if related:
            recurrence = max(
                (
                    row.recurrence
                    for row in self._incidents
                    if row.id in related
                ),
                default=0,
            ) + 1
        diagnosis_dict = self._safe_value(diagnosis.to_dict())
        return Incident(
            id=new_id("inc"),
            fingerprint=diagnosis.fingerprint,
            component=diagnosis.component,
            title=diagnosis.title,
            severity=diagnosis.severity,
            state=IncidentState.DIAGNOSED,
            detected_at=now,
            updated_at=now,
            symptoms=(diagnosis.cause,),
            evidence=tuple(
                str(self._safe_value(item)) for item in diagnosis.evidence
            ),
            diagnosis=dict(diagnosis_dict if isinstance(diagnosis_dict, Mapping) else {}),
            cause_confidence=diagnosis.confidence,
            impact=diagnosis.impact,
            related=tuple(related),
            recurrence=recurrence,
            metadata={
                "rule_id": diagnosis.rule_id,
                "rationale": diagnosis.rationale,
                "recommendations": list(diagnosis.recommendations),
                "snapshot_id": snapshot_id(snapshot),
            },
        )

    def _update(
        self,
        current: Incident,
        diagnosis: Diagnosis,
        snapshot: HealthSnapshot | None = None,
    ) -> Incident:
        now = self._clock()
        evidence = tuple(
            dict.fromkeys(
                (
                    *current.evidence,
                    *(str(self._safe_value(item)) for item in diagnosis.evidence),
                )
            )
        )[-20:]
        diagnosis_dict = self._safe_value(diagnosis.to_dict())
        severity = (
            diagnosis.severity
            if diagnosis.severity.rank >= current.severity.rank
            else current.severity
        )
        confidence = (
            diagnosis.confidence
            if diagnosis.confidence.rank > current.cause_confidence.rank
            else current.cause_confidence
        )
        recommendations = tuple(
            dict.fromkeys(
                (
                    *tuple(current.metadata.get("recommendations", ()) or ()),
                    *diagnosis.recommendations,
                )
            )
        )
        metadata = {
            **dict(current.metadata),
            "rule_id": diagnosis.rule_id,
            "rationale": diagnosis.rationale,
            "recommendations": list(recommendations),
            "snapshot_id": snapshot_id(snapshot),
        }
        return replace(
            current,
            title=diagnosis.title or current.title,
            severity=severity,
            updated_at=now,
            symptoms=tuple(dict.fromkeys((*current.symptoms, diagnosis.cause)))[-10:],
            evidence=evidence,
            diagnosis=dict(diagnosis_dict if isinstance(diagnosis_dict, Mapping) else {}),
            cause_confidence=confidence,
            impact=diagnosis.impact or current.impact,
            metadata=self._safe_value(metadata),
        )

    def _replace(self, updated: Incident) -> None:
        for index, row in enumerate(self._incidents):
            if row.id == updated.id:
                self._incidents[index] = updated
                return
        self._incidents.append(updated)

    # -- lifecycle -----------------------------------------------------------
    def get(self, incident_id: str) -> Incident:
        for incident in self._incidents:
            if incident.id == incident_id:
                return incident
        raise KeyError(f"Incident not found: {incident_id}")

    def transition(self, incident_id: str, state: IncidentState, *, note: str = "") -> Incident:
        current = self.get(incident_id)
        if state is current.state:
            return current
        if state not in _ALLOWED_TRANSITIONS[current.state]:
            raise InvalidIncidentTransition(
                f"Incident {incident_id} cannot move from {current.state.value} to {state.value}."
            )
        updated = replace(
            current,
            state=state,
            updated_at=self._clock(),
            resolution=note if state is IncidentState.RESOLVED else current.resolution,
        )
        self._replace(updated)
        self._save()
        self._emit(
            "self_management.incident_updated",
            incident_id=updated.id,
            component=updated.component,
            state=updated.state.value,
            severity=updated.severity.value,
        )
        return updated

    def record_attempt(self, incident_id: str, attempt: RecoveryAttempt) -> Incident:
        current = self.get(incident_id)
        attempts = (*current.recovery_attempts, attempt)[-20:]
        state = current.state
        if attempt.status is RecoveryStatus.RUNNING:
            state = IncidentState.RECOVERING
        elif attempt.status is RecoveryStatus.SUCCEEDED:
            state = IncidentState.RESOLVED
        elif attempt.status in (RecoveryStatus.REFUSED, RecoveryStatus.ESCALATED):
            state = IncidentState.ESCALATED
        elif attempt.status in (RecoveryStatus.FAILED, RecoveryStatus.TIMED_OUT):
            state = (
                IncidentState.ESCALATED
                if sum(
                    1
                    for row in attempts
                    if row.action_id == attempt.action_id
                    and row.status
                    in (RecoveryStatus.FAILED, RecoveryStatus.TIMED_OUT)
                )
                >= 2
                else IncidentState.RECOVERY_PENDING
            )
        allowed = _ALLOWED_TRANSITIONS[current.state]
        if state is not current.state and state not in allowed:
            state = current.state
        updated = replace(
            current,
            state=state,
            updated_at=self._clock(),
            recovery_attempts=attempts,
            resolution=(
                f"Recovery action {attempt.action_id} verified the component healthy."
                if attempt.status is RecoveryStatus.SUCCEEDED
                else current.resolution
            ),
        )
        self._replace(updated)
        self._save()
        self._emit(
            "self_management.recovery_recorded",
            incident_id=updated.id,
            action_id=attempt.action_id,
            status=attempt.status.value,
            incident_state=updated.state.value,
        )
        return updated

    def resolve(self, incident_id: str, note: str) -> Incident:
        current = self.get(incident_id)
        if current.state is IncidentState.RESOLVED:
            return current
        updated = replace(
            current,
            state=IncidentState.RESOLVED,
            updated_at=self._clock(),
            resolution=note,
        )
        self._replace(updated)
        self._save()
        self._emit(
            "self_management.incident_resolved",
            incident_id=updated.id,
            component=updated.component,
            resolution=note,
        )
        return updated

    def apply_diagnosis(self, incident_id: str, diagnosis: Diagnosis) -> Incident:
        """Attach a (possibly unknown) finding without opening a second incident."""
        current = self.get(incident_id)
        diagnosis_dict = self._safe_value(diagnosis.to_dict())
        state = (
            IncidentState.DIAGNOSED
            if IncidentState.DIAGNOSED in _ALLOWED_TRANSITIONS[current.state]
            else current.state
        )
        updated = replace(
            current,
            title=diagnosis.title or current.title,
            state=state,
            updated_at=self._clock(),
            symptoms=tuple(dict.fromkeys((*current.symptoms, diagnosis.cause)))[-10:],
            evidence=tuple(
                dict.fromkeys((*current.evidence, *(str(item) for item in diagnosis.evidence)))
            )[-20:],
            diagnosis=dict(diagnosis_dict if isinstance(diagnosis_dict, Mapping) else {}),
            cause_confidence=diagnosis.confidence,
            impact=diagnosis.impact or current.impact,
            metadata={
                **dict(current.metadata),
                "rule_id": diagnosis.rule_id,
                "rationale": diagnosis.rationale,
                "recommendations": list(diagnosis.recommendations),
            },
        )
        self._replace(updated)
        self._save()
        return updated

    def mark_investigating(self, incident_id: str) -> Incident:
        current = self.get(incident_id)
        if current.state is IncidentState.INVESTIGATING:
            return current
        if IncidentState.INVESTIGATING not in _ALLOWED_TRANSITIONS[current.state]:
            return current
        updated = replace(
            current, state=IncidentState.INVESTIGATING, updated_at=self._clock()
        )
        self._replace(updated)
        self._save()
        return updated

    def mark_recovery_pending(self, incident_id: str) -> Incident:
        current = self.get(incident_id)
        if current.state is IncidentState.RECOVERY_PENDING:
            return current
        if IncidentState.RECOVERY_PENDING not in _ALLOWED_TRANSITIONS[current.state]:
            return current
        updated = replace(
            current, state=IncidentState.RECOVERY_PENDING, updated_at=self._clock()
        )
        self._replace(updated)
        self._save()
        return updated

    def escalate(self, incident_id: str, note: str) -> Incident:
        current = self.get(incident_id)
        updated = replace(
            current,
            state=IncidentState.ESCALATED,
            updated_at=self._clock(),
            resolution=note,
        )
        self._replace(updated)
        self._save()
        self._emit(
            "self_management.incident_escalated",
            incident_id=updated.id,
            component=updated.component,
            reason=note,
        )
        return updated

    def dismiss_healthy(
        self,
        snapshot: HealthSnapshot,
        *,
        active_components: set[str] | None = None,
    ) -> tuple[Incident, ...]:
        """Resolve open incidents whose component the latest pass now reports healthy.

        This is evidence-based closure: the component's own probe passed on the
        current pass. A component with a CURRENT diagnosis is not closed even
        if its generic probe is OK: a persisted-configuration failure is one
        example where the component row says "loaded" while the same pass's
        diagnosis still names it. The caller passes those active components so
        the two sources of truth cannot contradict each other.
        """
        healthy = {
            row.component
            for row in snapshot.components
            if row.status.healthy
        }
        active = active_components or set()
        resolved: list[Incident] = []
        for incident in list(self._incidents):
            # ESCALATED is an operator-review state; a later healthy probe must
            # not erase the fact that recovery failed. RECOVERING is closed by
            # its own verifier, not by an unrelated monitoring pass.
            if incident.state in (IncidentState.RESOLVED, IncidentState.ESCALATED, IncidentState.RECOVERING):
                continue
            if incident.component in active:
                continue
            if incident.component not in healthy:
                continue
            note = (
                "Recovery was verified and the component's live probe is healthy on a later monitoring pass."
                if incident.recovery_attempts
                else "The component's live probe is healthy on a later monitoring pass; no recovery action was needed."
            )
            resolved.append(self.resolve(incident.id, note))
        return tuple(resolved)

    # -- notifications -------------------------------------------------------
    def _notify_new(self, incident: Incident) -> None:
        now = self._clock()
        self._replace(replace(incident, notified_at=now))
        self._emit(
            "self_management.incident_detected",
            incident_id=incident.id,
            component=incident.component,
            severity=incident.severity.value,
            confidence=incident.cause_confidence.value,
        )

    def _notify_escalated(self, incident: Incident) -> None:
        self._replace(replace(incident, notified_at=self._clock()))
        self._emit(
            "self_management.incident_escalated",
            incident_id=incident.id,
            component=incident.component,
            severity=incident.severity.value,
        )

    # -- reporting -----------------------------------------------------------
    def list(
        self,
        *,
        open_only: bool = False,
        component: str = "",
        limit: int = 50,
    ) -> tuple[Incident, ...]:
        rows = [
            row
            for row in self._incidents
            if (not open_only or row.open)
            and (not component or row.component == component)
        ]
        rows.sort(key=lambda row: row.updated_at, reverse=True)
        return tuple(rows[: max(1, min(int(limit), self.limit))])

    @property
    def all(self) -> tuple[Incident, ...]:
        return tuple(self._incidents)

    def report(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for row in self._incidents:
            counts[row.state.value] = counts.get(row.state.value, 0) + 1
        open_rows = [row for row in self._incidents if row.open]
        recurring = [
            row.id
            for row in self._incidents
            if row.recurrence >= 1
        ]
        return {
            "total": len(self._incidents),
            "open": len(open_rows),
            "counts": dict(sorted(counts.items())),
            "recurring": recurring[-20:],
            "severities": {
                level.value: sum(1 for row in open_rows if row.severity is level)
                for level in IncidentSeverity
            },
        }

    def recurrence_groups(self) -> tuple[dict[str, Any], ...]:
        groups: dict[str, list[Incident]] = {}
        for incident in self._incidents:
            groups.setdefault(incident.fingerprint, []).append(incident)
        rows = [
            {
                "fingerprint": fingerprint,
                "component": items[-1].component,
                "occurrences": len(items),
                "incident_ids": [item.id for item in items[-10:]],
                "latest": items[-1].id,
            }
            for fingerprint, items in groups.items()
            if len(items) > 1
        ]
        rows.sort(key=lambda row: int(str(row["occurrences"])), reverse=True)
        return tuple(rows)


def snapshot_id(snapshot: HealthSnapshot | None) -> str:
    return snapshot.snapshot_id if snapshot is not None else ""


__all__ = ["IncidentManager", "InvalidIncidentTransition"]
