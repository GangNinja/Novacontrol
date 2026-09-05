"""Source presentation helpers: chips, titles, verification, domains.

Owns: formatting sources for display, verifying source quality.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlparse

from novacontrol.explore.synthesizer import clean_snippet
from novacontrol.explore.models import ResearchSource


def domain(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def source_chips(sources: Sequence[ResearchSource]) -> tuple[dict[str, Any], ...]:
    chips, seen = [], set()
    for source in sources:
        d = domain(source.url)
        if not d or d in seen:
            continue
        seen.add(d)
        clean_title = clean_source_title(source.title, source.url)
        chips.append({"label": d, "type": source.source_type, "title": clean_title, "url": source.url})
        if len(chips) >= 5:
            break
    return tuple(chips)


def clean_source_title(title: str, url: str) -> str:
    cleaned = title.strip()
    # Remove domain prefix patterns
    domain_match = re.match(r"^[a-zA-Z0-9.-]+\.(com|org|net|io)\s+https?://", cleaned)
    if domain_match:
        rest = cleaned[domain_match.end():]
        rest = re.sub(r"^[a-zA-Z0-9.-]+\s*", "", rest)
        rest = re.sub(r"[›»]+", " ", rest)
        rest = re.sub(r"\s+", " ", rest).strip()
        if rest and len(rest) > 5:
            cleaned = rest
    cleaned = re.sub(r"https?://[^\s]+", "", cleaned)
    cleaned = re.sub(r"[›»>]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if ("/" in cleaned or "-" in cleaned) and len(cleaned) < 30:
        cleaned = cleaned.replace("-", " ").replace("_", " ").replace("/", " ").title()
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = re.sub(r"^[a-zA-Z0-9.-]+\.(com|org|net|io)\s*", "", cleaned)
    cleaned = re.sub(r"^wiki\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*-\s*$", "", cleaned)
    cleaned = re.sub(r"(\w)\s*-\s*(\w)", r"\1 \2", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) < 5 and url:
        try:
            parsed = urlparse(url)
            parts = [p for p in parsed.path.split("/") if p and p != "www"]
            cleaned = " ".join(parts[-2:]).replace("-", " ").replace("_", " ").title()
        except Exception:
            pass
    return cleaned or domain(url) or "Source"


def verification(sources: Sequence[ResearchSource], *, provider_status: str) -> dict[str, object]:
    domains = tuple(sorted({d for s in sources if (d := domain(s.url))}))
    independent_count = len(domains)
    has_snippets = sum(1 for s in sources if s.snippet.strip())
    if provider_status == "online":
        if independent_count >= 3 and has_snippets >= 2:
            confidence = "medium"
        elif independent_count >= 2:
            confidence = "low-medium"
        elif independent_count == 1:
            confidence = "low"
        else:
            confidence = "offline"
    else:
        confidence = "offline"
    notes = [
        f"Checked {len(sources)} source(s) across {independent_count} independent domain(s).",
        "Treat this as verified only to the level supported by the listed sources.",
    ]
    if independent_count < 2:
        notes.append("Use caution: fewer than two independent sources were available.")
    return {"confidence": confidence, "source_count": len(sources), "independent_domains": domains,
            "notes": notes, "claims": _claim_checks(sources)}


def _claim_checks(sources: Sequence[ResearchSource]) -> tuple[dict[str, Any], ...]:
    checks = []
    for i, source in enumerate(sources[:5], start=1):
        claim = clean_snippet(source.snippet or source.title)
        if not claim:
            continue
        checks.append({"claim": claim, "source_index": i, "domain": domain(source.url),
                        "status": "supported_by_listed_source"})
    return tuple(checks)
