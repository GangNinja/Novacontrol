"""Phase 35 — Business Opportunity & Validation Engine.

One package over NovaControl's existing Explore, knowledge, state, event, audit
and API seams. It adds no parallel research stack, no duplicate agent registry
and no autonomous external-action runner.
"""

from novacontrol.business.models import (
    Opportunity,
    OpportunityStage,
    SourceRef,
    EvidenceRecord,
    EvidenceKind,
    EvidenceQuality,
    SignalType,
)
from novacontrol.business.service import (
    BusinessConflict,
    BusinessError,
    BusinessOpportunityService,
    DuplicateOpportunity,
    OpportunityNotFound,
    ResearchUnavailable,
)

__all__ = [
    "BusinessConflict",
    "BusinessError",
    "BusinessOpportunityService",
    "DuplicateOpportunity",
    "EvidenceKind",
    "EvidenceQuality",
    "EvidenceRecord",
    "Opportunity",
    "OpportunityNotFound",
    "OpportunityStage",
    "ResearchUnavailable",
    "SignalType",
    "SourceRef",
]
