"""Phase 13.3/13.4 — the operational audit trail.

Distinct from :mod:`novacontrol.core.audit`, which records one executed
automation ACTION (a desktop click, a browser navigation, a phone message) as
the controllers perform it. This package records one USER REQUEST or one
scheduled run, end to end: the request, the intent, the decision, the plan, the
tools and actions, the verification, the failures, the recovery, the model, the
latency, the resource usage, and the permission decisions — with sensitive
values redacted on the way in and a retention policy that bounds what is kept.

Two granularities, two consumers: the controllers' log answers "what did this
controller do?", and this one answers "what did this system do with my request,
under whose authority, and how did it end?".
"""

from novacontrol.audit.logger import (
    AUDIT_DIRECTORY,
    AUDIT_FILENAME,
    AuditLogger,
    AuditSink,
    InMemoryAuditSink,
    JsonlAuditSink,
    RetentionPolicy,
)
from novacontrol.audit.models import AuditRecord, json_safe
from novacontrol.audit.redact import (
    DEFAULT_RULES,
    REDACTED,
    RedactionOutcome,
    RedactionReport,
    RedactionRule,
    Redactor,
)

__all__ = [
    "AUDIT_DIRECTORY",
    "AUDIT_FILENAME",
    "DEFAULT_RULES",
    "REDACTED",
    "AuditLogger",
    "AuditRecord",
    "AuditSink",
    "InMemoryAuditSink",
    "JsonlAuditSink",
    "RedactionOutcome",
    "RedactionReport",
    "RedactionRule",
    "Redactor",
    "RetentionPolicy",
    "json_safe",
]
