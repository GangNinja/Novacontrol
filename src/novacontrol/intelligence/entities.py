"""Modular entity extraction.

Intent classification says WHAT the user wants; entities say WHAT IT APPLIES
TO. Tools need the second half, and it is the half that breaks most quietly —
"set volume to 40%" that yields no ``volume`` value is a command that cannot be
executed, and "search YouTube for X" that loses the site searches everywhere.

Each extractor is an independent unit with a declared name and the entity kinds
it owns, so:

  * adding a new entity type is a new class, not a new branch in the engine;
  * one extractor's mistake cannot silently overwrite another's result — the
    registry merges by kind and first writer wins for a given kind.

Extractors run on NORMALIZED text and never touch the network or the model.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from novacontrol.intelligence.intent import IntentName
from novacontrol.intelligence.normalize import correct_token

# Applications with canonical spellings — the typo-tolerance vocabulary. Kept
# in canonical form because the desktop runner resolves by name.
APPLICATION_VOCABULARY: tuple[str, ...] = (
    "chrome", "edge", "firefox", "brave", "notepad", "calculator", "calc",
    "paint", "explorer", "settings", "steam", "spotify", "outlook", "word",
    "excel", "powerpoint", "code", "vscode", "terminal", "whatsapp", "youtube",
    "gmail", "github", "files",
)

# Website names that are usable as-is when launching the browser.
KNOWN_SITES: tuple[str, ...] = (
    "youtube", "gmail", "github", "google", "maps", "drive", "twitter", "x",
    "reddit", "netflix", "spotify", "linkedin", "wikipedia", "amazon",
)

# Deliberately stops at whitespace: a greedy prefix would swallow the verb too,
# turning "find report.pdf" into a file literally named "find report.pdf".
_FILE_LIKE = re.compile(
    r"\b[\w.\-]+\.(?:pdf|txt|md|csv|json|ya?ml|xlsx?|docx?|pptx?|png|jpe?g|gif|zip|tar|gz|"
    r"mp[34]|mp4|avi|mkv|log|ini|toml|cfg|py|js|ts|tsx|jsx|html|css|sh|bat|ps1|exe|sql)\b",
    re.IGNORECASE,
)
_QUOTED = re.compile(r"[\"'`]([^\"'`]{1,200})[\"'`]")
_URL = re.compile(r"\b(?:https?://|www\.)\S+", re.IGNORECASE)
_BARE_DOMAIN = re.compile(r"\b([a-z0-9-]+(?:\.[a-z0-9-]+)+)\b", re.IGNORECASE)
_PERCENT = re.compile(r"\b(\d{1,3})\s*(?:%|percent)\b")
_BARE_LEVEL = re.compile(r"\b(?:to|=|at)\s*(\d{1,3})\b")
_NUMBER = re.compile(r"\b(\d+(?:\.\d+)?)\b")
# A project is named in prose ("find my NovaControl project"), never by an
# extension, so it needs its own reader rather than the filename pattern.
_NAMED_PROJECT = re.compile(
    r"\b(?:my|our|the)\s+([\w][\w .&+'-]{0,40}?)\s+project\b"
    r"|\bproject\s+(?:called|named)\s+([\w][\w .&+'-]{0,40})",
    re.IGNORECASE,
)

_SEARCH_FOR_SITE = re.compile(
    r"\b(?:search|look up|google|find)\b(?:.*?)\b(?:" + "|".join(KNOWN_SITES) + r")\b\s*(?:for|about)?\s*(.+)$",
    re.IGNORECASE,
)
_SITE_IN_TEXT = re.compile(r"\b(" + "|".join(KNOWN_SITES) + r")\b", re.IGNORECASE)
_NOISE = re.compile(
    r"\b(?:the|a|an|my|our|please|now|app|application|program|browser|website|"
    r"site|on|phone|called|named|into|to|in|from|for|and)\b",
    re.IGNORECASE,
)
# A folder is often named after a verb ("open my report folder", "where is my
# NovaControl directory"): the leading request words are not part of the name.
_FOLDER_LEADING_REQUEST = re.compile(
    r"^(?:(?:open|launch|show|go|locate|find|display|where|is|are|was|were)\s+)+",
    re.IGNORECASE,
)


class EntityExtractor(Protocol):
    """One independently-testable source of entities.

    ``name`` and ``kinds`` are read-only properties rather than plain
    attributes so an immutable (frozen) extractor satisfies the protocol: a
    writable attribute contract would demand a mutable implementation.
    """

    @property
    def name(self) -> str: ...  # pragma: no cover - protocol

    @property
    def kinds(self) -> tuple[str, ...]: ...  # pragma: no cover - protocol

    def extract(self, normalized: str, intent: IntentName) -> dict[str, Any]:
        """Return the entities this extractor can prove are present."""
        ...  # pragma: no cover - protocol


@dataclass(frozen=True, slots=True)
class EntityExtractorRegistry:
    """Ordered collection of extractors, merged by kind.

    First writer wins for a kind: extractors earlier in registration order are
    more specific (a literal filename beats a guessed topic), so their evidence
    is not overwritten by a later, looser reader.
    """

    extractors: tuple[EntityExtractor, ...] = ()

    def register(self, extractor: EntityExtractor) -> "EntityExtractorRegistry":
        return EntityExtractorRegistry((*self.extractors, extractor))

    def extract(self, normalized: str, intent: IntentName) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for extractor in self.extractors:
            try:
                found = extractor.extract(normalized, intent)
            except Exception:  # an extractor must never break understanding
                continue
            for kind, value in found.items():
                if value in (None, "", (), [], {}):
                    continue
                merged.setdefault(kind, value)
        return merged

    def kinds(self) -> tuple[str, ...]:
        seen: list[str] = []
        for extractor in self.extractors:
            for kind in extractor.kinds:
                if kind not in seen:
                    seen.append(kind)
        return tuple(seen)


@dataclass(frozen=True, slots=True)
class ApplicationExtractor:
    """Application names, typo-corrected against the canonical vocabulary."""

    name: str = "application"
    kinds: tuple[str, ...] = ("application",)
    vocabulary: tuple[str, ...] = APPLICATION_VOCABULARY

    def extract(self, normalized: str, intent: IntentName) -> dict[str, Any]:
        if intent not in (
            IntentName.OPEN_APPLICATION,
            IntentName.CLOSE_APPLICATION,
            IntentName.PHONE_OPEN_APP,
        ):
            return {}
        value = " ".join(correct_token(token, self.vocabulary) for token in normalized.split())
        # Only claim the entity when it actually names something known: the
        # caller's own prefix stripping is more precise for unknown targets.
        tokens = [token for token in value.split() if token in self.vocabulary]
        if not tokens:
            return {}
        return {"application": tokens[0] if len(tokens) == 1 else " ".join(tokens)}


@dataclass(frozen=True, slots=True)
class FileExtractor:
    """File names (extension-anchored) and the folders they live in."""

    name: str = "file"
    kinds: tuple[str, ...] = ("file", "folder")

    def extract(self, normalized: str, intent: IntentName) -> dict[str, Any]:
        found: dict[str, Any] = {}
        match = _FILE_LIKE.search(normalized)
        if match:
            found["file"] = match.group(0).strip().strip(".,;:-_ ")
        folder = re.search(
            r"\b([\w -]{2,40}?)\s+(?:folder|directory)\b",
            normalized,
            re.IGNORECASE,
        )
        if folder:
            folder_name = _FOLDER_LEADING_REQUEST.sub("", _NOISE.sub(" ", folder.group(1)).strip()).strip()
            if folder_name:
                found["folder"] = folder_name
        return found


@dataclass(frozen=True, slots=True)
class ProjectExtractor:
    """Project names, which are named in prose rather than by extension."""

    name: str = "project"
    kinds: tuple[str, ...] = ("project",)

    def extract(self, normalized: str, intent: IntentName) -> dict[str, Any]:
        # Scoped to the intents that actually take a project: a bare "my X
        # project" inside, say, a volume request is not a project target.
        if intent not in (
            IntentName.FIND_FILE,
            IntentName.PROJECT_ANALYSIS,
            IntentName.OPEN_FOLDER,
        ):
            return {}
        match = _NAMED_PROJECT.search(normalized)
        if match is None:
            return {}
        project = (match.group(1) or match.group(2) or "").strip().strip(".,;:?!'\"")
        return {"project": project} if project else {}


@dataclass(frozen=True, slots=True)
class UrlExtractor:
    """Explicit URLs, bare domains, and known site names."""

    name: str = "url"
    kinds: tuple[str, ...] = ("url", "website")

    def extract(self, normalized: str, intent: IntentName) -> dict[str, Any]:
        found: dict[str, Any] = {}
        explicit = _URL.search(normalized)
        if explicit:
            found["url"] = explicit.group(0).rstrip(".,;:")
            return found
        domain = _BARE_DOMAIN.search(normalized)
        if domain and "." in domain.group(1):
            candidate = domain.group(1).rstrip(".,;:")
            # "report.pdf" is a file, not a domain — the file extractor owns it.
            if not _FILE_LIKE.fullmatch(candidate) and candidate.lower() not in {"e.g", "i.e"}:
                found["url"] = candidate
                return found
        site = _SITE_IN_TEXT.search(normalized)
        if site:
            found["website"] = site.group(1).lower()
        return found


@dataclass(frozen=True, slots=True)
class QueryExtractor:
    """The search query, and the site it is scoped to when one is named."""

    name: str = "query"
    kinds: tuple[str, ...] = ("query", "website")

    def extract(self, normalized: str, intent: IntentName) -> dict[str, Any]:
        if intent not in (
            IntentName.SEARCH_WEB,
            IntentName.BROWSER_ACTION,
            IntentName.NAVIGATE,
            IntentName.RESEARCH,
        ):
            return {}
        found: dict[str, Any] = {}
        site_match = _SEARCH_FOR_SITE.search(normalized)
        if site_match:
            named_site = _SITE_IN_TEXT.search(normalized)
            if named_site is not None:
                found["website"] = named_site.group(1).lower()
            found["query"] = site_match.group(1).strip().strip(".,;:?!")
            found = {kind: value for kind, value in found.items() if value}
            return found
        for verb in ("search the web for ", "search for ", "search online for ", "look up ", "google ", "search "):
            if verb in normalized:
                query = normalized.split(verb, 1)[1].strip().strip(".,;:?!")
                # Strip a trailing site scope ("... on youtube").
                query = re.sub(r"\s+on\s+(?:" + "|".join(KNOWN_SITES) + r")\s*$", "", query, flags=re.IGNORECASE)
                if query:
                    found["query"] = query
                break
        return found


@dataclass(frozen=True, slots=True)
class LevelExtractor:
    """Percentages and explicit levels ("volume to 40%", "brightness = 30")."""

    name: str = "level"
    kinds: tuple[str, ...] = ("level",)

    def extract(self, normalized: str, intent: IntentName) -> dict[str, Any]:
        match = _PERCENT.search(normalized)
        if match:
            return {"level": _clamp_percent(int(match.group(1)))}
        if intent in (IntentName.VOLUME_CONTROL, IntentName.BRIGHTNESS_CONTROL):
            bare = _BARE_LEVEL.search(normalized)
            if bare:
                return {"level": _clamp_percent(int(bare.group(1)))}
        return {}


@dataclass(frozen=True, slots=True)
class NumberExtractor:
    """The first bare number, for arithmetic-style requests."""

    name: str = "number"
    kinds: tuple[str, ...] = ("numbers",)

    def extract(self, normalized: str, intent: IntentName) -> dict[str, Any]:
        numbers = [float(match) for match in _NUMBER.findall(normalized)]
        if not numbers:
            return {}
        return {"numbers": numbers}


@dataclass(frozen=True, slots=True)
class QuotedTextExtractor:
    """Explicitly quoted literal text ('type "hello world"')."""

    name: str = "quoted"
    kinds: tuple[str, ...] = ("text",)

    def extract(self, normalized: str, intent: IntentName) -> dict[str, Any]:
        match = _QUOTED.search(normalized)
        if match and match.group(1).strip():
            return {"text": match.group(1).strip()}
        return {}


def default_extractors() -> EntityExtractorRegistry:
    """The registry the NLU engine uses out of the box."""
    registry = EntityExtractorRegistry()
    for extractor in (
        ApplicationExtractor(),
        FileExtractor(),
        ProjectExtractor(),
        UrlExtractor(),
        QueryExtractor(),
        LevelExtractor(),
        QuotedTextExtractor(),
        NumberExtractor(),
    ):
        registry = registry.register(extractor)
    return registry


def _clamp_percent(value: int) -> int:
    return max(0, min(100, value))


def merge_entities(*groups: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    """Merge entity groups, first writer winning per kind."""
    merged: dict[str, Any] = {}
    for group in groups:
        for kind, value in group:
            if value in (None, "", (), [], {}):
                continue
            merged.setdefault(kind, value)
    return merged
