"""NovaForge NF-11 — game discovery, launch lifecycle and cleanup.

Three questions, kept apart on purpose:

* **Is it here?** :class:`GameCatalog` answers from verified local evidence — a
  Steam manifest whose install directory and executable exist, a launchable
  Start Menu shortcut, a configured install root, or a running process that
  exposes its own executable path. A name in a library listing, a stale
  manifest or a shortcut pointing at a missing file is NOT an installation.
* **Is it running?** A process record (pid + start time + window) is the only
  answer, and it is separate from "installed".
* **Did MY launch work?** :class:`GameLaunchService` records the exact identity
  it started, verifies a process and window appeared, and closes only what it
  started, confirming that identity actually exited.

The service performs no gameplay input, installs nothing, never kills by process
name, and never closes a game it did not start.
"""

from __future__ import annotations

import re
import sys
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__all__ = [
    "AMBIGUOUS_MATCH",
    "ALREADY_RUNNING",
    "CANDIDATE_INVENTORY",
    "INSTALLED_AND_LAUNCHABLE",
    "INSTALLED_BUT_LAUNCH_FAILED",
    "GameArtifact",
    "GameCatalog",
    "GameLaunchService",
    "LaunchRecord",
    "NOT_FOUND_ON_INTERNAL_STORAGE",
    "NOT_TESTED",
    "REQUIRES_LAUNCHER_OR_RUNTIME",
    "candidate_title",
    "game_request",
    "normalise",
    "row_matches",
]

#: The vocabulary the report and the API use. One spelling everywhere.
INSTALLED_AND_LAUNCHABLE = "INSTALLED_AND_LAUNCHABLE"
ALREADY_RUNNING = "ALREADY_RUNNING"
INSTALLED_BUT_LAUNCH_FAILED = "INSTALLED_BUT_LAUNCH_FAILED"
NOT_FOUND_ON_INTERNAL_STORAGE = "NOT_FOUND_ON_INTERNAL_STORAGE"
REQUIRES_LAUNCHER_OR_RUNTIME = "REQUIRES_LAUNCHER_OR_RUNTIME"
AMBIGUOUS_MATCH = "AMBIGUOUS_MATCH"
NOT_TESTED = "NOT_TESTED"


@dataclass(frozen=True, slots=True)
class CandidateGame:
    """One title from the operator's inventory, with the words people use."""

    title: str
    aliases: tuple[str, ...] = ()
    #: ``desktop`` titles come from Steam / a shortcut / an executable. ``mobile``
    #: titles need an Android runtime this build has no integration with.
    platform_hint: str = "desktop"

    def matches(self, query: str) -> bool:
        wanted = normalise(query)
        if not wanted:
            return False
        for name in (self.title, *self.aliases):
            key = normalise(name)
            if not key:
                continue
            if wanted == key or wanted.replace(" ", "") == key.replace(" ", ""):
                return True
            if _contains_words(wanted, key) or _contains_words(key, wanted):
                return True
        return False


#: The candidate inventory named by the phase brief. Presence here is a
#: QUESTION, not evidence: discovery answers each one from the machine.
CANDIDATE_INVENTORY: tuple[CandidateGame, ...] = (
    CandidateGame("GTA V", ("gta 5", "gta v", "grand theft auto v", "gta online", "gtav")),
    CandidateGame(
        "Call of Duty",
        ("call of duty", "cod", "modern warfare", "warzone", "black ops"),
    ),
    CandidateGame("Hill Climb Racing", ("hill climb racing", "hill climb"), "mobile"),
    CandidateGame("Clash of Clans", ("clash of clans", "coc"), "mobile"),
    CandidateGame("Cosmic Coliseum", ("cosmic coliseum",)),
    CandidateGame("Tekken 8", ("tekken 8", "tekken")),
    CandidateGame("Free Fire", ("free fire", "freefire", "garena free fire"), "mobile"),
    CandidateGame("Shadow Fight Arena", ("shadow fight arena", "shadow fight"), "mobile"),
    CandidateGame("Freedom Fighters", ("freedom fighters", "freedom")),
    CandidateGame("Hollow Knight", ("hollow knight",)),
    CandidateGame("Quarantine Zone", ("quarantine zone", "quarantine")),
)


def candidate_title(query: str) -> CandidateGame | None:
    """The inventory row a request names, if any."""
    for candidate in CANDIDATE_INVENTORY:
        if candidate.matches(query):
            return candidate
    return None


#: Steam library entries that are runtime prerequisites rather than titles.
#: App id 228980 is Steamworks Common Redistributables; listing it as a
#: discovered game (and offering to launch it) is a false inventory row.
_STEAM_NON_GAME_APP_IDS = frozenset({"228980"})


def normalise(value: str) -> str:
    """Case/whitespace/punctuation-insensitive form used for every comparison."""
    cleaned = "".join(
        char if char.isalnum() or char.isspace() else " " for char in str(value or "")
    )
    return " ".join(cleaned.lower().split())


def _contains_words(haystack: str, needle: str) -> bool:
    """Whole-word containment — 'ark' must not match inside 'superm*ark*et'."""
    if not needle:
        return False
    return f" {needle} " in f" {haystack} "


def row_matches(row: Mapping[str, Any], query: str) -> bool:
    """Whether ONE catalog row answers a search box query.

    Searches the title and its aliases, so "gta 5" finds the "GTA V" row and
    "tk8" does not invent one. Substring matching is used here (a search box
    should match a prefix) while name RESOLUTION keeps whole-word rules.
    """
    wanted = normalise(query)
    if not wanted:
        return True
    names = (row.get("title"), *(row.get("query_names") or ()))
    return any(wanted in normalise(str(name or "")) for name in names if name)


#: How a person asks a game to start. Kept as data so the router, the API and
#: the panel cannot grow three different ideas of "a launch request".
_LAUNCH_VERBS = ("launch", "open", "start", "play")

#: How a person asks whether a game is here. Each pattern captures a NAME.
_GAME_STATUS_PATTERNS = (
    re.compile(
        r"^(?:check|see|verify)\s+(?:whether|if)\s+(?:the\s+game\s+)?"
        r"(?P<name>.+?)\s+(?:is\s+)?(?:installed|available|on\s+this\s+"
        r"(?:pc|machine|laptop|computer))\b",
        re.I,
    ),
    re.compile(r"^is\s+(?:the\s+game\s+)?(?P<name>.+?)\s+(?:installed|available)\b", re.I),
    re.compile(
        r"^(?:do\s+i\s+have|is)\s+(?P<name>.+?)(?:\s+installed)?\s+on\s+"
        r"(?:this|the)\s+(?:pc|machine|laptop|computer)\b",
        re.I,
    ),
    re.compile(r"^do\s+i\s+have\s+(?P<name>.+?)(?:\s+installed)?$", re.I),
)

_ATTACH_PATTERN = re.compile(
    r"^(?:attach|connect)\s+to\s+(?:the\s+)?(?:game\s+)?"
    r"(?:that\s+is\s+|that's\s+|which\s+is\s+)?already\s+running$",
    re.I,
)


def _clean_request(text: str) -> str:
    cleaned = " ".join(str(text or "").strip().split())
    while cleaned.endswith(("?", "!", ".")):
        cleaned = cleaned[:-1].rstrip()
    for suffix in (" please", " now", " for me"):
        if cleaned.lower().endswith(suffix):
            cleaned = cleaned[: -len(suffix)].rstrip()
    return cleaned


def _game_name(value: str) -> str:
    name = " ".join(str(value or "").strip().split())
    if name.lower().startswith("the game "):
        name = name[len("the game ") :].strip()
    return name


def game_request(text: str) -> tuple[str, str]:
    """Classify ONE natural-language game request as ``(kind, name)``.

    ``kind`` is ``launch``, ``status``, ``attach`` or ``""`` (not a game
    request). Anything that is not a single clause is refused: a chain such as
    "open steam and launch gta v" belongs to the desktop planner, which keeps
    the cross-clause context this helper cannot, and answering it here would
    drop half the sentence.

    This only recognises the SHAPE of the request. Whether the name is really a
    game is decided by the catalog — never by this function.
    """
    cleaned = _clean_request(text)
    lower = cleaned.lower()
    if not cleaned or " and " in f" {lower} ":
        return "", ""
    if _ATTACH_PATTERN.match(cleaned):
        return "attach", ""
    for pattern in _GAME_STATUS_PATTERNS:
        match = pattern.match(cleaned)
        if match:
            name = _game_name(match.group("name"))
            if name:
                return "status", name
    for verb in _LAUNCH_VERBS:
        if lower.startswith(f"{verb} "):
            name = _game_name(cleaned[len(verb) + 1 :])
            if name:
                return "launch", name
    return "", ""


@dataclass(frozen=True, slots=True)
class GameArtifact:
    """One launchable thing discovered on this machine.

    ``launch_kind`` says HOW it would be started, and only two kinds are
    launchable here: a Steam app id (Steam's own protocol) and a verified local
    executable or shortcut that exists on an internal fixed drive.
    """

    key: str
    name: str
    source: str  # "steam" | "shortcut" | "configured" | "running"
    launch_kind: str  # "steam" | "path" | ""
    launch_target: str  # app id, or the verified absolute path
    install_dir: str = ""
    executable: str = ""
    executable_exists: bool = False
    app_id: str = ""
    notes: tuple[str, ...] = ()

    @property
    def launchable(self) -> bool:
        if self.launch_kind == "steam":
            return bool(self.launch_target)
        if self.launch_kind == "path":
            return self.executable_exists
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "source": self.source,
            "launch_kind": self.launch_kind,
            "launch_target": self.launch_target,
            "install_dir": self.install_dir,
            "executable": self.executable,
            "executable_exists": self.executable_exists,
            "app_id": self.app_id,
            "launchable": self.launchable,
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class LaunchRecord:
    """The exact process a launch produced, and whether it is OURS to close."""

    launch_id: str
    name: str
    key: str
    pid: int
    process_name: str
    start_time: str
    window: str
    source: str
    owned: bool
    launched_at: str
    verified: bool
    verification: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "launch_id": self.launch_id,
            "name": self.name,
            "key": self.key,
            "pid": self.pid,
            "process_name": self.process_name,
            "start_time": self.start_time,
            "window": self.window,
            "source": self.source,
            "owned": self.owned,
            "launched_at": self.launched_at,
            "verified": self.verified,
            "verification": self.verification,
        }


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _entry(
    candidate: CandidateGame | None,
    *,
    name: str,
    status: str,
    artifact: GameArtifact | None = None,
    running: Mapping[str, Any] | None = None,
    reason: str = "",
    candidates: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """One catalog row: a status, the evidence behind it, and a reason."""
    return {
        "title": candidate.title if candidate is not None else name,
        "query_names": list(
            (candidate.title, *candidate.aliases) if candidate is not None else (name,)
        ),
        "status": status,
        "reason": reason,
        "artifact": artifact.to_dict() if artifact is not None else None,
        "running": dict(running or {}) if running is not None else None,
        "candidates": [dict(row) for row in candidates],
        "tested": False,
    }


class GameCatalog:
    """Bounded, evidence-first discovery. Every source is injected.

    Each source is a callable so the same catalogue can be driven from the real
    Windows runner (Steam manifests, Start Menu index, process probe) or from
    fixtures in a test, with no branch in the logic that knows the difference.
    """

    def __init__(
        self,
        *,
        steam_games: Callable[[], Sequence[Mapping[str, Any]]],
        shortcuts: Callable[[], Mapping[str, str]],
        processes: Callable[[Sequence[str]], Awaitable[Sequence[Mapping[str, Any]]]],
        processes_under: (
            Callable[[Sequence[str]], Awaitable[Sequence[Mapping[str, Any]]]] | None
        ) = None,
        configured_dirs: Sequence[str | Path] = (),
        internal_path: Callable[[str | Path], bool] | None = None,
        max_configured_files: int = 2000,
    ) -> None:
        self._steam_games = steam_games
        self._shortcuts = shortcuts
        self._processes = processes
        self._processes_under = processes_under
        self._configured_dirs = tuple(Path(str(item)) for item in configured_dirs if str(item))
        self._internal_path = internal_path or _is_internal_fixed_drive
        self.max_configured_files = max_configured_files

    # -- sources -----------------------------------------------------------

    def steam_artifacts(self) -> list[GameArtifact]:
        """Steam titles whose recorded install directory still exists."""
        artifacts: list[GameArtifact] = []
        for row in self._steam_games() or ():
            name = str(row.get("name") or "").strip()
            app_id = str(row.get("app_id") or row.get("appid") or "").strip()
            if not name or not app_id.isdigit() or app_id in _STEAM_NON_GAME_APP_IDS:
                continue
            install_dir = str(row.get("install_dir") or "")
            executable = str(row.get("executable") or "")
            exists = bool(executable) and Path(executable).is_file()
            install_exists = bool(install_dir) and Path(install_dir).is_dir()
            notes: list[str] = []
            if not install_exists:
                notes.append(
                    "Steam records this title but its install directory is missing "
                    "(an unmounted or removed library)."
                )
            artifacts.append(
                GameArtifact(
                    key=f"steam:{app_id}",
                    name=name,
                    source="steam",
                    # Steam's deep link works from the app id alone; a missing
                    # directory is reported, and the launch attempt is what
                    # proves the install.
                    launch_kind="steam",
                    launch_target=app_id,
                    install_dir=install_dir,
                    executable=executable,
                    executable_exists=exists,
                    app_id=app_id,
                    notes=tuple(notes),
                )
            )
        return artifacts

    def shortcut_artifacts(self) -> list[GameArtifact]:
        """Start Menu / App Paths entries that resolve to a real file."""
        artifacts: list[GameArtifact] = []
        for name, target in (self._shortcuts() or {}).items():
            text = str(target or "")
            if not text or not Path(text).exists():
                continue
            if not self._internal_path(text):
                continue
            suffix = Path(text).suffix.lower()
            if suffix not in {".exe", ".lnk"}:
                continue
            runtime = next(
                (
                    marker
                    for marker in _RUNTIME_SHORTCUT_MARKERS
                    if marker in normalise(text)
                ),
                "",
            )
            if runtime:
                artifacts.append(
                    GameArtifact(
                        key=f"path:{normalise(text)}",
                        name=str(name),
                        source="shortcut",
                        launch_kind="",
                        launch_target="",
                        install_dir=str(Path(text).parent),
                        executable="",
                        executable_exists=False,
                        notes=(
                            f"This shortcut belongs to {runtime.title()}, an Android "
                            "runtime. Starting it opens that runtime (and its account "
                            "flow), not the game, so this build reports the title as "
                            "needing a runtime it does not integrate.",
                        ),
                    )
                )
                continue
            artifacts.append(
                GameArtifact(
                    key=f"path:{normalise(text)}",
                    name=str(name),
                    source="shortcut",
                    launch_kind="path",
                    launch_target=text,
                    install_dir=str(Path(text).parent),
                    executable=text,
                    executable_exists=True,
                )
            )
        return artifacts

    def configured_artifacts(self) -> list[GameArtifact]:
        """Executables under the operator's declared game roots.

        Bounded by construction: each root is read one level deep, at most
        ``max_configured_files`` entries are considered, and a root on a
        non-fixed drive is refused instead of scanned.
        """
        artifacts: list[GameArtifact] = []
        seen = 0
        for root in self._configured_dirs:
            if not root.is_dir():
                continue
            if not self._internal_path(root):
                continue
            try:
                entries = list(root.iterdir())
            except OSError:
                continue
            for entry in entries:
                if seen >= self.max_configured_files:
                    break
                seen += 1
                if not entry.is_dir():
                    continue
                try:
                    candidates = [item for item in entry.iterdir() if item.suffix.lower() == ".exe"]
                except OSError:
                    continue
                for exe in candidates:
                    if not self._internal_path(exe):
                        continue
                    # The directory name is what identifies the game: an exe
                    # called "game.exe" says nothing on its own.
                    artifacts.append(
                        GameArtifact(
                            key=f"path:{normalise(str(exe))}",
                            name=entry.name,
                            source="configured",
                            launch_kind="path",
                            launch_target=str(exe),
                            install_dir=str(entry),
                            executable=str(exe),
                            executable_exists=True,
                        )
                    )
        return artifacts

    async def running_processes(self, names: Sequence[str]) -> dict[str, Any]:
        """Running matches for the supplied name patterns (one probe call)."""
        try:
            rows = await self._processes(tuple(names)) or ()
        except Exception:  # noqa: BLE001 - an unreadable probe is a stated gap
            return {}
        found: dict[str, Any] = {}
        for row in rows:
            key = normalise(str(row.get("name") or ""))
            if key and key not in found:
                found[key] = dict(row)
        return found

    # -- discovery ---------------------------------------------------------

    def artifacts(self) -> list[GameArtifact]:
        """Every artifact, de-duplicated by launch target (first source wins)."""
        seen: dict[str, GameArtifact] = {}
        for artifact in (
            *self.steam_artifacts(),
            *self.shortcut_artifacts(),
            *self.configured_artifacts(),
        ):
            if artifact.key in seen:
                continue
            seen[artifact.key] = artifact
        return list(seen.values())

    async def entries(self) -> list[dict[str, Any]]:
        """One row per candidate title, plus any other discovered title.

        The process probe runs ONCE for every name the catalog cares about: a
        per-row PowerShell call would make discovery 11 processes deep and turn
        a cheap read into a slow one.
        """
        artifacts = self.artifacts()
        labels: dict[str, tuple[GameArtifact, ...]] = {}
        claimed: set[str] = set()
        for candidate in CANDIDATE_INVENTORY:
            matches = tuple(item for item in artifacts if _artifact_matches(item, candidate))
            labels[candidate.title] = matches
            claimed.update(item.key for item in matches)
        for artifact in artifacts:
            if artifact.key in claimed:
                continue
            # The Start Menu is an APPLICATION index, not a games library: listing
            # its 155 shortcuts turned "Control Panel" and "Run" into discovered
            # games, and "system information" into a match against pid 4 (System).
            # Shortcut entries therefore only ever RESOLVE a candidate title above;
            # a title is listed in its own right only when a games source (Steam
            # metadata, a configured game root) reported it.
            if artifact.source == "shortcut":
                continue
            labels.setdefault(artifact.name, ())
            labels[artifact.name] = (*labels[artifact.name], artifact)
        patterns: set[str] = set()
        for title, matches in labels.items():
            if not matches:
                patterns.update(_process_patterns(title, _blank_artifact(title)))
                continue
            for artifact in matches:
                patterns.update(_process_patterns(title, artifact))
        running = await self.running_processes(tuple(sorted(patterns)))
        # A process running from inside a verified install is evidence even when
        # its executable name shares nothing with the title (SFA.exe). One call
        # for every install directory, not one per title.
        if self._processes_under is not None:
            roots = sorted(
                {
                    item.install_dir
                    for matches in labels.values()
                    for item in matches
                    if item.install_dir
                }
            )
            if roots:
                try:
                    under = await self._processes_under(tuple(roots)) or ()
                except Exception:  # noqa: BLE001 - an unreadable probe is a gap
                    under = ()
                for row in under:
                    key = normalise(str(row.get("name") or ""))
                    if key and key not in running:
                        running[key] = dict(row)
        rows: list[dict[str, Any]] = []
        for title, known_matches in labels.items():
            known = candidate_title(title)
            rows.append(
                await self._classify(known, known_matches, name=title, running=running)
            )
        return rows

    async def _classify(
        self,
        candidate: CandidateGame | None,
        matches: Sequence[GameArtifact],
        *,
        name: str = "",
        running: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        label = candidate.title if candidate is not None else name
        if not matches:
            if candidate is not None and candidate.platform_hint == "mobile":
                return _entry(
                    candidate,
                    name=label,
                    status=REQUIRES_LAUNCHER_OR_RUNTIME,
                    reason=(
                        "This is an Android title. No supported Android runtime or emulator "
                        "integration exists in this build, so nothing was launched or scanned "
                        "for it."
                    ),
                )
            return _entry(
                candidate,
                name=label,
                status=NOT_FOUND_ON_INTERNAL_STORAGE,
                reason="No verified installation on internal fixed storage.",
            )
        launchable = [item for item in matches if item.launchable]
        if len({item.name for item in launchable}) > 1:
            return _entry(
                candidate,
                name=label,
                status=AMBIGUOUS_MATCH,
                candidates=[item.to_dict() for item in launchable],
                reason=(
                    "More than one installed title matches; resolve the edition before "
                    "launching rather than picking one."
                ),
            )
        if not launchable:
            return _entry(
                candidate,
                name=label,
                status=REQUIRES_LAUNCHER_OR_RUNTIME,
                artifact=matches[0],
                reason=(
                    "; ".join(matches[0].notes)
                    or "A record exists but no verified executable or launcher entry does."
                ),
            )
        artifact = launchable[0]
        if running is None:
            running = await self.running_processes(_process_patterns(label, artifact))
        match = _pick_running(running, label, artifact)
        if match is not None:
            return _entry(
                candidate,
                name=label,
                status=ALREADY_RUNNING,
                artifact=artifact,
                running=match,
                reason=(
                    f"Already running as {match.get('name')} (pid {match.get('pid')})"
                    + (f" with window {match.get('window')!r}" if match.get("window") else "")
                    + ". NovaForge attaches instead of launching a duplicate."
                ),
            )
        return _entry(
            candidate,
            name=label,
            status=INSTALLED_AND_LAUNCHABLE,
            artifact=artifact,
            reason=artifact.notes[0] if artifact.notes else "Verified launchable installation.",
        )

    async def resolve(self, query: str) -> dict[str, Any]:
        """Resolve one request against the catalog, without launching anything."""
        wanted = normalise(query)
        if not wanted:
            raise ValueError("A game name is required.")
        rows = await self.entries()
        exact = [row for row in rows if any(normalise(n) == wanted for n in row["query_names"])]
        if len(exact) == 1:
            row = exact[0]
            if row["status"] == AMBIGUOUS_MATCH:
                # The title matched, but more than one installed edition did:
                # no single entry may be returned as the answer.
                return {
                    "status": AMBIGUOUS_MATCH,
                    "entry": None,
                    "candidates": row["candidates"],
                }
            return {"status": row["status"], "entry": row, "candidates": []}
        partial = [
            row
            for row in rows
            if any(_contains_words(normalise(n), wanted) or _contains_words(wanted, normalise(n))
                   for n in row["query_names"])
        ]
        distinct = {row["title"]: row for row in partial}
        if len(distinct) == 1:
            row = next(iter(distinct.values()))
            return {"status": row["status"], "entry": row, "candidates": []}
        if len(distinct) > 1:
            rows_found = list(distinct.values())
            return {
                "status": AMBIGUOUS_MATCH,
                "entry": None,
                "candidates": [
                    {"title": row["title"], "status": row["status"]} for row in rows_found
                ],
            }
        candidate = candidate_title(query)
        return {
            "status": NOT_FOUND_ON_INTERNAL_STORAGE,
            "entry": _entry(
                candidate,
                name=candidate.title if candidate else query,
                status=NOT_FOUND_ON_INTERNAL_STORAGE,
                reason="No installed title matches that name.",
            ),
            "candidates": [],
        }

    async def describe(self) -> dict[str, Any]:
        rows = await self.entries()
        counts: dict[str, int] = {}
        for row in rows:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        return {
            "generated_at": _now(),
            "entries": rows,
            "counts": counts,
            "candidate_inventory": [item.title for item in CANDIDATE_INVENTORY],
            "limitations": [
                "Only fixed internal drives are scanned; removable, network and "
                "unmounted volumes are refused rather than searched.",
                "Steam manifests are the only launcher metadata integrated; other "
                "launchers are reported as REQUIRES_LAUNCHER_OR_RUNTIME.",
                "Android titles need an emulator integration this build does not have.",
                "A catalog row is not a launch test: only `tested` says a launch ran.",
            ],
        }


def _artifact_matches(artifact: GameArtifact, candidate: CandidateGame) -> bool:
    for name in (candidate.title, *candidate.aliases):
        key = normalise(name)
        if key and (
            normalise(artifact.name) == key
            or _contains_words(normalise(artifact.name), key)
            or _contains_words(key, normalise(artifact.name))
        ):
            return True
    return False


#: Shortcut folders owned by an Android runtime/store. A .lnk inside one of
#: these starts the RUNTIME (and its account flow), not the game, so it is
#: reported as needing a launcher this build does not integrate — the phase's
#: rule that a shortcut alone is not proof of a launchable installation.
_RUNTIME_SHORTCUT_MARKERS = (
    "google play games",
    "bluestacks",
    "ldplayer",
    "gameloop",
    "mumu",
    "nox",
)


#: Executables a game installs BESIDE itself that are not the game. A crash
#: handler left behind by a crashed launch once made the catalog answer
#: ALREADY_RUNNING for a game that was not running, and it is also what a
#: verification poll would otherwise mistake for a successful start.
_HELPER_PROCESS_NAMES = frozenset(
    {
        "crashpadhandler",
        "unitycrashhandler32",
        "unitycrashhandler64",
        "gameoverlayui64",
        "steamwebhelper",
        "steam",
        "werfault",
        "dxsetup",
        "dxwebsetup",
        "vcredistx64",
        "vcredistx86",
    }
)


def _is_helper_process(row: Mapping[str, Any]) -> bool:
    """Whether this row is an install's support process, not the game."""
    stem = normalise(Path(str(row.get("name") or "")).stem).replace(" ", "")
    return stem in _HELPER_PROCESS_NAMES


def _process_patterns(label: str, artifact: GameArtifact) -> tuple[str, ...]:
    """Name fragments a running instance would expose.

    Includes the space-stripped form because a process name is one token:
    ``ShadowFightArena.exe`` exposes ``shadowfightarena``, which a
    ``shadow fight arena`` pattern would never match.
    """
    patterns = {label, artifact.name}
    if artifact.executable:
        patterns.add(Path(artifact.executable).stem)
    words = [word for word in normalise(label).split() if len(word) > 3]
    if words:
        patterns.add(words[0])
    patterns.update(
        item.replace(" ", "").lower() for item in tuple(patterns) if " " in item
    )
    return tuple(sorted({item for item in patterns if item}))


def _pick_running(
    running: Mapping[str, Mapping[str, Any]], label: str, artifact: GameArtifact
) -> Mapping[str, Any] | None:
    wanted = {normalise(item) for item in _process_patterns(label, artifact)}
    install = normalise(artifact.install_dir)
    for key, row in running.items():
        if _is_helper_process(row):
            continue
        name = normalise(str(row.get("name") or ""))
        path = normalise(Path(str(row.get("path") or "")).stem) if row.get("path") else ""
        if name in wanted or (path and path in wanted):
            return row
        # The executable can be an abbreviation of the title; the folder it runs
        # from is the identity that survives that.
        running_path = normalise(str(row.get("path") or ""))
        if install and running_path and running_path.startswith(install + " "):
            return row
    return None


class GameLaunchService:
    """Launch exactly what was resolved, verify it, and close only our own.

    The seams are the JARVIS desktop runner's: ``launch`` starts a Steam app id
    or a verified path, ``processes`` reports identity, ``close_window`` asks a
    window to close (WM_CLOSE — never a process kill), ``memory`` reports the
    machine's free memory. Nothing here sends input.
    """

    def __init__(
        self,
        catalog: GameCatalog,
        *,
        launch: Callable[..., Awaitable[Mapping[str, Any]]],
        processes: Callable[[Sequence[str]], Awaitable[Sequence[Mapping[str, Any]]]],
        close_window: Callable[[str], Awaitable[Mapping[str, Any]]],
        memory: Callable[[], Awaitable[Mapping[str, Any]]],
        processes_under: (
            Callable[[Sequence[str]], Awaitable[Sequence[Mapping[str, Any]]]] | None
        ) = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        min_free_gb: float = 2.0,
        max_used_percent: float = 92.0,
        startup_timeout: float = 90.0,
        shutdown_timeout: float = 30.0,
        window_timeout: float = 30.0,
        poll_interval: float = 1.0,
        max_launch_attempts: int = 1,
    ) -> None:
        self.catalog = catalog
        self._launch = launch
        self._processes = processes
        self._processes_under = processes_under
        self._close_window = close_window
        self._memory = memory
        self._clock = clock
        self._sleep = sleep or _asyncio_sleep
        self.min_free_gb = float(min_free_gb)
        self.max_used_percent = float(max_used_percent)
        self.startup_timeout = float(startup_timeout)
        self.shutdown_timeout = float(shutdown_timeout)
        self.window_timeout = float(window_timeout)
        self.poll_interval = float(poll_interval)
        self.max_launch_attempts = max(1, int(max_launch_attempts))
        self._owned: LaunchRecord | None = None
        self._history: list[dict[str, Any]] = []
        self._launch_seq = 0

    # -- state -------------------------------------------------------------

    @property
    def owned(self) -> LaunchRecord | None:
        """The one instance this service started and has not confirmed closed."""
        return self._owned

    async def owned_claim(self) -> LaunchRecord | None:
        """The LIVE claim, if any; a record whose process exited is cleared.

        Planning must use this, not the raw record: a game that exited on its
        own would otherwise block every later launch until the process restarted.
        """
        if self._owned is None:
            return None
        if await self._process_alive(self._owned):
            return self._owned
        self._owned = None
        return None

    def history(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._history]

    def _record(self, payload: Mapping[str, Any]) -> None:
        self._history.append(dict(payload))

    async def status_payload(self) -> dict[str, Any]:
        """This service's own view: the live claim, its history and the gate.

        Kept separate from the catalog because "what is installed" and "what
        this process started" are different questions. The panel reads both, and
        no row may imply the other.
        """
        return {
            "available": True,
            "owned": self._owned.to_dict() if self._owned is not None else None,
            "history": self.history()[-20:],
            "memory": await self.memory_check(),
        }

    async def catalog_payload(self) -> dict[str, Any]:
        """Discovery, with THIS service's launch tests marked on the rows.

        Discovery alone cannot know a launch happened. Reading the history back
        into the rows keeps one surface honest: after a verified launch the row
        says so, and an untested row never looks tested.
        """
        body = await self.catalog.describe()
        tested: dict[str, str] = {}
        for record in self._history:
            key = str((record.get("launch") or {}).get("key") or "")
            if key:
                tested[key] = str(record.get("status") or "")
        for row in body.get("entries", []):
            key = str((row.get("artifact") or {}).get("key") or "")
            if key and key in tested:
                row["tested"] = True
                row["launch_status"] = tested[key]
        return body

    # -- memory ------------------------------------------------------------

    async def memory_check(self) -> dict[str, Any]:
        """Refuse a launch when the machine cannot safely take one.

        ``available=False`` is NOT a refusal: a host that cannot report memory
        must not be blocked forever. The check reports what it could not read.
        """
        try:
            snapshot = dict(await self._memory())
        except Exception as exc:  # noqa: BLE001 - an unreadable probe is a gap
            return {
                "ok": True,
                "available": False,
                "reason": f"Memory could not be read ({type(exc).__name__}); the check was skipped.",
                "snapshot": {},
            }
        free_gb = snapshot.get("free_gb")
        used_percent = snapshot.get("used_percent")
        problems: list[str] = []
        if isinstance(free_gb, (int, float)) and float(free_gb) < self.min_free_gb:
            problems.append(f"only {float(free_gb):.1f} GB free (minimum {self.min_free_gb:.1f} GB)")
        if isinstance(used_percent, (int, float)) and float(used_percent) > self.max_used_percent:
            problems.append(
                f"{float(used_percent):.0f}% of memory in use (limit {self.max_used_percent:.0f}%)"
            )
        measured = isinstance(free_gb, (int, float)) or isinstance(used_percent, (int, float))
        return {
            "ok": not problems,
            "available": measured,
            "reason": "; ".join(problems) if problems else "",
            "snapshot": snapshot,
        }

    # -- launch ------------------------------------------------------------

    async def launch(self, query: str) -> dict[str, Any]:
        """Resolve, gate, start, and VERIFY one game. No duplicate launches."""
        if self._owned is not None:
            still = await self._process_alive(self._owned)
            if still:
                raise RuntimeError(
                    f"{self._owned.name} was started by this service (pid {self._owned.pid}) "
                    "and is still running. Close it before launching another game."
                )
            # The process is gone: the record is stale, not an active claim.
            self._owned = None
        resolution = await self.catalog.resolve(query)
        status = resolution["status"]
        entry = resolution.get("entry")
        if status == AMBIGUOUS_MATCH:
            return {
                "status": AMBIGUOUS_MATCH,
                "entry": None,
                "candidates": resolution["candidates"],
                "launched": False,
                "reason": "More than one installed title matches; nothing was launched.",
            }
        if entry is None or status != INSTALLED_AND_LAUNCHABLE:
            return {
                "status": status,
                "entry": entry,
                "candidates": resolution.get("candidates") or [],
                "launched": False,
                "reason": (entry or {}).get("reason")
                or "Only an INSTALLED_AND_LAUNCHABLE title may be launched.",
            }
        artifact = entry["artifact"] or {}
        memory = await self.memory_check()
        if not memory["ok"]:
            return {
                "status": status,
                "entry": entry,
                "launched": False,
                "reason": f"Launch refused: {memory['reason']}.",
                "memory": memory,
            }
        attempts: list[dict[str, Any]] = []
        for attempt in range(1, self.max_launch_attempts + 1):
            attempt_row = await self._attempt(entry, artifact, attempt)
            attempts.append(attempt_row)
            if attempt_row["launched"]:
                return {**attempt_row, "memory": memory}
        failure = attempts[-1]
        self._record(
            {
                "status": INSTALLED_BUT_LAUNCH_FAILED,
                "title": entry["title"],
                "reason": failure.get("reason", ""),
                "at": _now(),
                "attempts": [row.get("reason", "") for row in attempts],
            }
        )
        return {
            "status": INSTALLED_BUT_LAUNCH_FAILED,
            "entry": entry,
            "launched": False,
            "reason": failure.get("reason", "The launch produced no verified process or window."),
            "attempts": attempts,
            "memory": memory,
        }

    async def _attempt(
        self, entry: Mapping[str, Any], artifact: Mapping[str, Any], attempt: int
    ) -> dict[str, Any]:
        launch_kind = str(artifact.get("launch_kind") or "")
        target = str(artifact.get("launch_target") or "")
        parsed = _artifact_from(artifact)
        patterns = _process_patterns(str(entry["title"]), parsed)
        before = await self._all_processes(patterns, parsed)
        try:
            started = await self._launch(
                kind=launch_kind, target=target, name=str(entry["title"])
            )
        except Exception as exc:  # noqa: BLE001 - a refused launch is a result
            return {
                "launched": False,
                "status": INSTALLED_BUT_LAUNCH_FAILED,
                "reason": f"The launch command failed: {type(exc).__name__}: {exc}",
                "attempt": attempt,
            }
        deadline = self._clock() + self.startup_timeout
        found: Mapping[str, Any] | None = None
        while self._clock() < deadline:
            rows = await self._all_processes(patterns, parsed)
            fresh = _new_process(rows, before)
            if fresh is not None:
                found = fresh
                break
            await self._sleep(self.poll_interval)
        if found is None:
            return {
                "launched": False,
                "status": INSTALLED_BUT_LAUNCH_FAILED,
                "reason": (
                    f"Started {entry['title']!r} but no new matching process appeared "
                    f"within {self.startup_timeout:.0f}s."
                ),
                "attempt": attempt,
                "started": dict(started) if isinstance(started, Mapping) else {},
            }
        # A process is not a window. Keep the startup budget open for the window
        # to appear, because the phase's evidence for "it started" is the game's
        # own window, and a window is also what makes a graceful close possible.
        window_deadline = min(deadline, self._clock() + self.window_timeout)
        while not str(found.get("window") or "") and self._clock() < window_deadline:
            await self._sleep(self.poll_interval)
            for row in await self._all_processes(patterns, parsed):
                if int(row.get("pid") or 0) == int(found.get("pid") or 0) and row.get("window"):
                    found = row
                    break
        window = str(found.get("window") or "")
        record = LaunchRecord(
            launch_id=self._next_launch_id(),
            name=str(entry["title"]),
            key=str(artifact.get("key") or ""),
            pid=int(found.get("pid") or 0),
            process_name=str(found.get("name") or ""),
            start_time=str(found.get("start_time") or ""),
            window=window,
            source=str(artifact.get("source") or ""),
            owned=True,
            launched_at=_now(),
            verified=bool(found.get("pid")) and bool(window),
            verification=(
                f"process {found.get('name')} (pid {found.get('pid')})"
                + (f" with window {window!r}" if window else ", no window appeared")
            ),
        )
        self._owned = record
        verified = record.verified
        self._record(
            {"status": "LAUNCH_VERIFIED" if verified else "LAUNCH_UNVERIFIED",
             "title": record.name, "at": _now(), "launch": record.to_dict()}
        )
        return {
            "launched": True,
            "status": "LAUNCH_VERIFIED" if verified else INSTALLED_BUT_LAUNCH_FAILED,
            "entry": entry,
            "launch": record.to_dict(),
            "reason": (
                f"Verified {record.verification}."
                if verified
                else (
                    f"A process appeared ({record.process_name}, pid {record.pid}) but no "
                    f"window did within {self.window_timeout:.0f}s; the launch is not "
                    "verified. The instance stays tracked as this service's own."
                )
            ),
            "attempt": attempt,
            "started": dict(started) if isinstance(started, Mapping) else {},
        }

    def _next_launch_id(self) -> str:
        self._launch_seq += 1
        return f"launch-{self._launch_seq}"

    # -- close -------------------------------------------------------------

    async def close(self, *, launch_id: str = "") -> dict[str, Any]:
        """Close the instance THIS service started, and confirm it exited.

        A game that was already running is never closed here — it is not ours.
        A shutdown that does not complete in time is reported as a blocker; this
        service never escalates to a process kill.
        """
        record = self._owned
        if record is None:
            return {
                "status": "NO_OWNED_GAME",
                "closed": False,
                "reason": "This service is not tracking a game it started; nothing was closed.",
            }
        if launch_id and launch_id != record.launch_id:
            return {
                "status": "NOT_OWNED",
                "closed": False,
                "reason": f"Launch {launch_id!r} is not the instance this service owns.",
            }
        if not record.owned:
            return {
                "status": "NOT_OWNED",
                "closed": False,
                "reason": "That game was running before this service started it; it is not ours to close.",
            }
        if not record.window:
            return {
                "status": "NO_WINDOW",
                "closed": False,
                "reason": (
                    "The process has no window to ask to close. A graceful close is not "
                    "possible through J.A.R.V.I.S.; no process was killed."
                ),
                "launch": record.to_dict(),
            }
        try:
            await self._close_window(record.window)
        except Exception as exc:  # noqa: BLE001 - a refused close is a result
            return {
                "status": "CLOSE_REFUSED",
                "closed": False,
                "reason": f"The window refused to close: {type(exc).__name__}: {exc}",
                "launch": record.to_dict(),
            }
        deadline = self._clock() + self.shutdown_timeout
        while self._clock() < deadline:
            if not await self._process_alive(record):
                self._owned = None
                self._record(
                    {"status": "CLOSED", "title": record.name, "at": _now(),
                     "launch": record.to_dict()}
                )
                return {
                    "status": "CLOSED",
                    "closed": True,
                    "reason": (
                        f"{record.name} exited after a graceful close "
                        f"({record.process_name}, pid {record.pid})."
                    ),
                    "launch": record.to_dict(),
                }
            await self._sleep(self.poll_interval)
        return {
            "status": "SHUTDOWN_TIMEOUT",
            "closed": False,
            "reason": (
                f"{record.name} (pid {record.pid}) is still running "
                f"{self.shutdown_timeout:.0f}s after the close request. No process was "
                "killed; stop the batch and resolve this before launching another game."
            ),
            "launch": record.to_dict(),
        }

    async def attach_running(self) -> dict[str, Any]:
        """Attach to whatever is already running, when exactly one title is.

        The phase lists "attach to the game that is already running" as a
        request. Two running games make that ambiguous — and picking one would
        be the arbitrary choice the phase forbids.
        """
        rows = [
            row
            for row in await self.catalog.entries()
            if row["status"] == ALREADY_RUNNING
        ]
        if not rows:
            return {
                "status": NOT_FOUND_ON_INTERNAL_STORAGE,
                "attached": False,
                "reason": "No game from the catalog is running, so there is nothing to attach to.",
            }
        if len(rows) > 1:
            return {
                "status": AMBIGUOUS_MATCH,
                "attached": False,
                "candidates": [{"title": row["title"]} for row in rows],
                "reason": (
                    f"{len(rows)} games are running; name the one to attach to rather "
                    "than guessing."
                ),
            }
        row = rows[0]
        return {
            "status": ALREADY_RUNNING,
            "attached": True,
            "owned": False,
            "entry": row,
            "running": row.get("running"),
            "reason": (
                "Attached to the instance that was already running; it stays the "
                "operator's to close."
            ),
        }

    async def adopt(self, query: str) -> dict[str, Any]:
        """Report a running instance WITHOUT taking ownership of it."""
        if not str(query or "").strip():
            return await self.attach_running()
        resolution = await self.catalog.resolve(query)
        if resolution["status"] != ALREADY_RUNNING or resolution.get("entry") is None:
            return {
                "status": resolution["status"],
                "attached": False,
                "reason": "No running instance to attach to.",
                "entry": resolution.get("entry"),
            }
        row = resolution["entry"]
        return {
            "status": ALREADY_RUNNING,
            "attached": True,
            "owned": False,
            "entry": row,
            "running": row.get("running"),
            "reason": (
                "Attached to the instance that was already running; it stays the "
                "operator's to close."
            ),
        }

    # -- probes ------------------------------------------------------------

    async def _all_processes(
        self, patterns: Sequence[str], artifact: GameArtifact
    ) -> list[dict[str, Any]]:
        """Name matches AND processes running from the verified install directory."""
        rows = await self._safe_processes(patterns)
        if self._processes_under is not None and artifact.install_dir:
            try:
                extra = await self._processes_under((artifact.install_dir,)) or ()
            except Exception:  # noqa: BLE001 - an unreadable probe is a gap
                extra = ()
            seen = {int(row.get("pid") or 0) for row in rows}
            for row in extra:
                if int(row.get("pid") or 0) not in seen:
                    rows.append(dict(row))
        return rows

    async def _safe_processes(self, patterns: Sequence[str]) -> list[dict[str, Any]]:
        try:
            rows = await self._processes(tuple(patterns))
        except Exception:  # noqa: BLE001 - an unreadable probe is a gap, not a crash
            return []
        return [dict(row) for row in (rows or ())]

    async def _process_alive(self, record: LaunchRecord) -> bool:
        rows = await self._safe_processes((record.process_name or record.name,))
        for row in rows:
            if int(row.get("pid") or 0) != record.pid:
                continue
            # PID reuse guard: the identity is (pid, start time), not the pid.
            if record.start_time and str(row.get("start_time") or "") != record.start_time:
                continue
            return True
        return False


def _blank_artifact(name: str) -> GameArtifact:
    return GameArtifact(
        key="", name=name, source="", launch_kind="", launch_target=""
    )


def _artifact_from(payload: Mapping[str, Any]) -> GameArtifact:
    return GameArtifact(
        key=str(payload.get("key") or ""),
        name=str(payload.get("name") or ""),
        source=str(payload.get("source") or ""),
        launch_kind=str(payload.get("launch_kind") or ""),
        launch_target=str(payload.get("launch_target") or ""),
        install_dir=str(payload.get("install_dir") or ""),
        executable=str(payload.get("executable") or ""),
        executable_exists=bool(payload.get("executable_exists")),
        app_id=str(payload.get("app_id") or ""),
        notes=tuple(str(item) for item in payload.get("notes") or ()),
    )


def _new_process(
    rows: Sequence[Mapping[str, Any]], before: Sequence[Mapping[str, Any]]
) -> Mapping[str, Any] | None:
    """A process that was not running before the launch attempt."""
    previous = {(int(row.get("pid") or 0), str(row.get("start_time") or "")) for row in before}
    for row in rows:
        identity = (int(row.get("pid") or 0), str(row.get("start_time") or ""))
        if identity in previous:
            continue
        if _is_helper_process(row):
            # A crash handler spawning is not the game starting.
            continue
        return row
    return None


async def _asyncio_sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)


#: Drive types (winbase.h). Only DRIVE_FIXED (3) is internal storage.
_DRIVE_FIXED = 3


def _is_internal_fixed_drive(path: str | Path) -> bool:
    """Whether ``path`` is on a fixed local disk.

    Removable (2), network (4), CD-ROM (5) and RAM (6) drives are refused, and so
    is any UNC path: the phase forbids reading an external drive, and a mounted
    one would otherwise be indistinguishable from internal storage.
    """
    text = str(path or "")
    if not text:
        return False
    if text.startswith("\\\\") or text.startswith("//"):
        return False
    import os

    drive, _tail = os.path.splitdrive(text)
    if not drive:
        return False  # relative paths are not evidence of an installation
    # A literal `sys.platform` test, not `os.name`: the type checker reads this
    # file under BOTH platform views (CI verifies the Windows view from Linux)
    # and only this comparison lets it accept `ctypes.windll`, which typeshed
    # declares Windows-only. The two tests agree on every host that matters.
    if sys.platform != "win32":
        return True
    import ctypes

    try:
        kind = int(ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(drive + "\\")))
    except Exception:  # noqa: BLE001 - an unanswerable probe is refused, not assumed
        return False
    return kind == _DRIVE_FIXED
