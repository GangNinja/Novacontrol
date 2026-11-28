"""Grounded support assistance for customer tickets.

This is deliberately a deterministic reader over approved, recorded product
knowledge. It never invents a troubleshooting result, never claims an action ran,
and escalates when the evidence does not support an answer or the request is
sensitive (account, billing, refunds, contracts).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from novacontrol.customer.models import (
    SENSITIVE_TICKET_CATEGORIES,
    TicketCategory,
    new_id,
    utcnow,
)

_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "do", "does", "for",
    "from", "has", "have", "how", "i", "in", "is", "it", "my", "of", "on", "or",
    "our", "please", "the", "this", "to", "was", "we", "what", "when", "where",
    "which", "who", "why", "with", "you", "your", "not", "no", "need", "help",
}

_CATEGORY_TERMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (TicketCategory.BILLING.value, ("billing", "invoice", "refund", "charge", "payment", "price", "subscription")),
    (TicketCategory.ACCOUNT.value, ("account", "login", "log in", "password", "email address", "access", "user")),
    (TicketCategory.BUG.value, ("bug", "crash", "error", "exception", "broken", "fail", "failure")),
    (TicketCategory.FEATURE_REQUEST.value, ("feature", "request", "wish", "would like", "add support", "roadmap")),
    (TicketCategory.USAGE.value, ("how do", "how to", "usage", "configure", "setup", "set up", "tutorial", "documentation")),
    (TicketCategory.TECHNICAL.value, ("technical", "install", "connection", "network", "timeout", "slow", "performance")),
)


def _text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9][a-z0-9_-]{1,}", value.lower())
        if token not in _STOPWORDS and len(token) > 1
    }


def classify_issue(subject: str, body: str) -> str:
    combined = f"{subject} {body}".lower()
    scores: list[tuple[int, str]] = []
    for category, terms in _CATEGORY_TERMS:
        hits = sum(1 for term in terms if term in combined)
        if hits:
            scores.append((hits, category))
    if not scores:
        return TicketCategory.GENERAL.value
    scores.sort(key=lambda row: (-row[0], row[1]))
    return scores[0][1]


def _knowledge_rows(
    knowledge: Sequence[Mapping[str, Any]], query: str
) -> list[dict[str, Any]]:
    query_tokens = _tokens(query)
    scored: list[tuple[float, dict[str, Any], set[str]]] = []
    for raw in knowledge:
        if not isinstance(raw, Mapping):
            continue
        if raw.get("approved") is not True or raw.get("verified") is not True:
            continue
        title = _text(raw.get("title"))
        body = _text(raw.get("body"))
        keywords = " ".join(str(item) for item in raw.get("keywords") or [])
        haystack = f"{title} {body} {keywords}".lower()
        overlap = query_tokens & _tokens(haystack)
        if not overlap:
            continue
        score = len(overlap) / max(1, len(query_tokens))
        scored.append((score, dict(raw), overlap))
    scored.sort(key=lambda row: (-row[0], str(row[1].get("title", ""))))
    return [
        {
            "id": row[1].get("id"),
            "title": row[1].get("title"),
            "body": row[1].get("body"),
            "source": row[1].get("source"),
            "version": row[1].get("version"),
            "score": round(row[0], 4),
            "matched_terms": sorted(row[2]),
        }
        for row in scored[:3]
    ]


def support_assist(
    ticket: Mapping[str, Any],
    knowledge: Sequence[Mapping[str, Any]],
    *,
    product_snapshot: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Analyse one ticket using only approved, verified recorded knowledge."""
    subject = _text(ticket.get("subject"))
    body = _text(ticket.get("description"))
    category = str(ticket.get("category") or classify_issue(subject, body))
    query = f"{subject} {body}"
    hits = _knowledge_rows(knowledge, query)
    sensitive = category in SENSITIVE_TICKET_CATEGORIES
    urgency = str(ticket.get("priority", "normal")).lower() in {"high", "critical", "urgent"}
    legal_terms = any(
        term in query.lower()
        for term in ("refund", "legal", "contract", "lawsuit", "chargeback", "security breach")
    )
    steps: list[str] = []
    for hit in hits:
        source = next(
            (row for row in knowledge if row.get("id") == hit.get("id")), None
        )
        for step in (source or {}).get("steps") or []:
            text = _text(step)
            if text and text not in steps:
                steps.append(text)
    escalation_reasons: list[str] = []
    if not hits:
        escalation_reasons.append("No approved and verified knowledge matched the request.")
    elif not steps:
        escalation_reasons.append("The matching approved article has no recorded troubleshooting steps.")
    if sensitive:
        escalation_reasons.append("The ticket is in a sensitive account or billing category.")
    if legal_terms:
        escalation_reasons.append("The request contains refund, legal or contractual terms.")
    if urgency:
        escalation_reasons.append("The ticket has a high or critical priority.")
    escalation_required = bool(escalation_reasons)
    if hits:
        cited = "; ".join(
            f"{hit.get('title')} ({hit.get('source') or 'recorded documentation'})"
            for hit in hits
        )
        draft = (
            "Thanks for reporting this. I found the following approved product "
            f"documentation that may help: {cited}.\n\n"
            + "\n".join(f"- {step}" for step in steps[:5])
        )
        if not steps:
            draft += (
                "\n\nThe approved article is available, but it does not contain "
                "recorded troubleshooting steps. A human should review the request."
            )
    else:
        cited = ""
        draft = (
            "Thanks for contacting support. I could not find approved product "
            "documentation that answers this request, so I am escalating it for "
            "human review rather than guessing. No troubleshooting action has been "
            "claimed or performed."
        )
    if sensitive or legal_terms:
        draft += (
            "\n\nThis request involves an account, billing, refund or contractual "
            "matter. A human must review it before any account change, refund, "
            "billing adjustment or commitment."
        )
    result = {
        "id": new_id("support-assist"),
        "ticket_id": str(ticket.get("id", "")),
        "product_id": str((product_snapshot or {}).get("product_id", "")),
        "classification": category,
        "classification_basis": "recorded ticket text; deterministic keyword classifier",
        "grounded": bool(hits),
        "sources": hits,
        "troubleshooting_steps": steps,
        "draft_response": draft,
        "escalation_required": escalation_required,
        "escalation_reasons": escalation_reasons,
        "human_review_required": sensitive or legal_terms or urgency,
        "sensitive_category": sensitive,
        "actions_performed": [],
        "verification_note": (
            "Suggestions are drawn only from approved, verified knowledge. "
            "No action was executed and no result is claimed."
        ),
        "automated_recommendation": True,
        "created_at": utcnow().isoformat(),
    }
    return result


__all__ = ["classify_issue", "support_assist"]
