"""NovaForge NF-05 — the controlled game/environment compatibility registry.

This module answers one question before anything runs: **may this environment be
evaluated, in which mode, and on what evidence?** It contains no runner and no
I/O. An evaluation is bounded by the existing NF-01 session manager and measured
by the existing NF-04 benchmark lab; the registry only classifies what is
actually available in this build.

The honesty rules the phase depends on live here:

* **Capture is not control.** An already-open window can be observed; that says
  nothing about whether an input path exists. Every runnable entry is
  ``OBSERVATION_ONLY`` because NF-05 sends no gameplay input.
* **A completed evaluation is data completeness, not a won game.** No objective
  verifier is attached to a NovaForge session, so ``objective.verified`` is
  false with a reason everywhere it appears.
* **Compatibility is separate from readiness.** A supported environment still
  has to pass preflight against the window, the Guardian controls, the
  benchmark configuration and evidence persistence before it may run.
* **Nothing is launched.** Every entry requires an operator-opened window; a
  record that claimed launch support would be a capability this build lacks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .benchmark import BenchmarkScenario, resolve_scenario

GAME_SCHEMA_VERSION = 1
EVALUATION_VERSION = 1

#: The compatibility vocabulary. The order is the escalation the UI shows.
GAME_STATUSES = (
    "SUPPORTED",
    "SUPPORTED_WITH_LIMITATIONS",
    "OBSERVATION_ONLY",
    "UNSUPPORTED",
    "NOT_EVALUATED",
)

#: Evaluation modes. ``control`` requires BOTH an input path and an objective
#: verifier; neither exists in this build.
EVALUATION_MODES = ("observation_only", "control")

#: Check statuses. ``unavailable`` is a real answer: the capability does not
#: exist, which is different from a check that failed.
CHECK_STATUSES = ("pass", "warn", "fail", "unavailable")

CONTROL_CAPABLE = frozenset({"SUPPORTED", "SUPPORTED_WITH_LIMITATIONS"})
RUNNABLE_STATUSES = frozenset({"SUPPORTED", "SUPPORTED_WITH_LIMITATIONS", "OBSERVATION_ONLY"})


class GameError(ValueError):
    """Base class for registry and evaluation errors the API maps to a status."""


class GameNotFoundError(GameError):
    """An unknown game/environment id or version."""


class EvaluationError(ValueError):
    """An evaluation request that cannot run as asked."""

    def __init__(self, reasons: Sequence[str]) -> None:
        self.reasons = [str(item) for item in reasons]
        super().__init__(" ".join(self.reasons))


class EvaluationConflictError(EvaluationError):
    """A start that would overlap another evaluation or a live session."""


def _capability(
    available: bool,
    *,
    state: str = "",
    reason: str = "",
    detail: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """One capability row: available is a fact, and a refusal states why."""
    return {
        "available": bool(available),
        "state": state or ("available" if available else "unavailable"),
        "reason": "" if available else (reason or "This capability is not implemented."),
        "detail": dict(detail or {}),
    }


@dataclass(frozen=True, slots=True)
class GameRecord:
    """A validated, versioned environment record.

    ``window_detection`` and ``capture`` describe seams this build really has
    (the NF-01 manager's own validation and the desktop capture path).
    ``input`` and ``objective_verification`` describe what it does not.
    """

    game_id: str
    version: int
    display_name: str
    status: str
    status_reason: str
    supported_systems: tuple[str, ...]
    evaluation_modes: tuple[str, ...]
    launch: Mapping[str, Any]
    window_detection: Mapping[str, Any]
    capture: Mapping[str, Any]
    input: Mapping[str, Any]
    display_requirements: Mapping[str, Any]
    required_interfaces: tuple[str, ...]
    objective_verification: Mapping[str, Any]
    benchmark: Mapping[str, Any]
    offline: Mapping[str, Any]
    known_limitations: tuple[str, ...]

    @property
    def key(self) -> str:
        return f"{self.game_id}@{self.version}"

    @property
    def runnable(self) -> bool:
        return self.status in RUNNABLE_STATUSES

    @property
    def control_capable(self) -> bool:
        return self.status in CONTROL_CAPABLE and "control" in self.evaluation_modes

    @property
    def offline_confirmation_required(self) -> bool:
        return bool(self.offline.get("confirmation_required"))

    def supports_scenario(self, scenario_id: str) -> bool:
        return scenario_id in tuple(self.benchmark.get("scenarios") or ())

    def validate(self) -> list[str]:
        """Reasons this record is not internally consistent; empty means valid."""
        problems: list[str] = []
        if not self.game_id.strip():
            problems.append("A game id is required.")
        if not self.display_name.strip():
            problems.append("A display name is required.")
        if isinstance(self.version, bool) or not isinstance(self.version, int):
            problems.append("The version must be an integer.")
        elif self.version < 1:
            problems.append("The version must be at least 1.")
        if self.status not in GAME_STATUSES:
            problems.append(f"Unknown compatibility status {self.status!r}.")
        if self.status in {"UNSUPPORTED", "NOT_EVALUATED"} and not self.status_reason.strip():
            problems.append(f"A {self.status} record must state its reason.")
        if not self.supported_systems:
            problems.append("At least one supported operating system is required.")
        unknown_modes = [mode for mode in self.evaluation_modes if mode not in EVALUATION_MODES]
        if unknown_modes:
            problems.append(f"Unknown evaluation mode(s): {', '.join(unknown_modes)}.")
        if self.status in CONTROL_CAPABLE:
            if not self.control_capable:
                problems.append("A supported status requires the control evaluation mode.")
            if not self.input.get("available"):
                problems.append("A controllable entry requires an available input capability.")
            if not self.objective_verification.get("available"):
                problems.append(
                    "A controllable entry requires an objective verifier; capture alone is "
                    "not control."
                )
        elif self.status == "OBSERVATION_ONLY":
            if "control" in self.evaluation_modes:
                problems.append(
                    "An observation-only entry cannot declare the control mode: there is no "
                    "gameplay input path."
                )
            if not self.evaluation_modes:
                problems.append("An observation-only entry must declare observation_only.")
        elif self.evaluation_modes:
            problems.append(f"A {self.status} entry cannot declare an evaluation mode.")
        if self.launch.get("available"):
            problems.append(
                "NF-05 never launches a target; a record cannot claim launch support."
            )
        if not self.window_detection.get("available"):
            problems.append("Window detection is required for every observation entry.")
        if not self.capture.get("available"):
            problems.append("Capture support is required for every observation entry.")
        if self.runnable and not tuple(self.benchmark.get("scenarios") or ()):
            problems.append("A runnable entry must declare at least one benchmark scenario.")
        else:
            for scenario_id in self.benchmark.get("scenarios") or ():
                scenario = resolve_scenario(str(scenario_id))
                if scenario is None:
                    problems.append(f"Unknown benchmark scenario {scenario_id!r}.")
                elif not scenario.supported:
                    problems.append(
                        f"Benchmark scenario {scenario_id!r} is not runnable in this build."
                    )
        if self.offline_confirmation_required and not str(self.offline.get("evidence") or "").strip():
            problems.append("An offline-confirmation requirement must state its evidence basis.")
        if not self.objective_verification.get("available") and not str(
            self.objective_verification.get("reason") or ""
        ).strip():
            problems.append("An unavailable objective verifier must state why.")
        return problems

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": GAME_SCHEMA_VERSION,
            "game_id": self.game_id,
            "version": self.version,
            "key": self.key,
            "display_name": self.display_name,
            "status": self.status,
            "status_reason": self.status_reason,
            "supported_systems": list(self.supported_systems),
            "evaluation_modes": list(self.evaluation_modes),
            "launch": dict(self.launch),
            "window_detection": dict(self.window_detection),
            "capture": dict(self.capture),
            "input": dict(self.input),
            "display_requirements": dict(self.display_requirements),
            "required_interfaces": list(self.required_interfaces),
            "objective_verification": dict(self.objective_verification),
            "benchmark": dict(self.benchmark),
            "offline": dict(self.offline),
            "known_limitations": list(self.known_limitations),
            "runnable": self.runnable,
            "control_capable": self.control_capable,
        }


_NO_LAUNCH = _capability(
    False,
    state="not_implemented",
    reason=(
        "NF-05 never launches or installs a target. The operator opens the window; "
        "automatic launch is a separately authorized phase."
    ),
)

_WINDOW_DETECTION = _capability(
    True,
    detail={
        "method": (
            "The NF-01 manager lists open windows, requires exactly one title match, and "
            "pins the window handle and process id for the session."
        ),
        "identity": "title match plus handle/pid pinning",
        "ambiguity": "two windows matching the same title are refused, never guessed",
    },
)

_CAPTURE = _capability(
    True,
    detail={
        "source": "NovaForge window-cropped primary-display capture",
        "freshness": "each frame is verified fresh before it is recorded",
        "limits": "primary display only; the window must be visible",
    },
)

_NO_INPUT = _capability(
    False,
    state="not_implemented",
    reason=(
        "No gameplay input path exists in this build. Guardian's own input boundary "
        "refuses any future caller that has not validated a running target."
    ),
)

_NO_VERIFIER = _capability(
    False,
    state="not_implemented",
    reason=(
        "No objective verifier is attached to a NovaForge session. A time limit ending "
        "is data completeness, not a verified objective."
    ),
)


SCENARIOS: tuple[GameRecord, ...] = (
    GameRecord(
        game_id="disposable-window",
        version=1,
        display_name="Disposable Test Window",
        status="OBSERVATION_ONLY",
        status_reason=(
            "A deterministic, operator-opened window used to exercise the evaluation "
            "pipeline. It is observable; it is not a game or a controllable input target."
        ),
        supported_systems=("Windows", "Linux", "Darwin"),
        evaluation_modes=("observation_only",),
        launch=_NO_LAUNCH,
        window_detection=_WINDOW_DETECTION,
        capture=_CAPTURE,
        input=_NO_INPUT,
        display_requirements={
            "mode": "windowed",
            "visibility": "fully visible on the primary display",
            "minimum_size": "any measurable window",
        },
        required_interfaces=("NF-01 session manager", "NF-04 benchmark lab"),
        objective_verification=_NO_VERIFIER,
        benchmark={
            "scenarios": ("observation-baseline", "capture-latency", "observation-continuity"),
            "baseline_scenario": "observation-baseline",
            "mode": "observation_only",
        },
        offline={
            "classification": "not_applicable",
            "confirmation_required": False,
            "evidence": "A disposable test window makes no network or multiplayer claim.",
        },
        known_limitations=(
            "Observation only: no decision pipeline and no gameplay input.",
            "No objective verifier, so a completed evaluation is data completeness.",
            "Capture is primary-display and window-cropped.",
        ),
    ),
    GameRecord(
        game_id="offline-game-window",
        version=1,
        display_name="Operator-Provided Offline Game Window",
        status="OBSERVATION_ONLY",
        status_reason=(
            "An already-open, operator-declared offline/single-player game window can be "
            "observed and measured, but not controlled and not scored."
        ),
        supported_systems=("Windows", "Linux", "Darwin"),
        evaluation_modes=("observation_only",),
        launch=_NO_LAUNCH,
        window_detection=_WINDOW_DETECTION,
        capture=_CAPTURE,
        input=_NO_INPUT,
        display_requirements={
            "mode": "windowed",
            "visibility": "fully visible on the primary display",
            "minimum_size": "any measurable window",
        },
        required_interfaces=("NF-01 session manager", "NF-04 benchmark lab"),
        objective_verification=_NO_VERIFIER,
        benchmark={
            "scenarios": ("observation-baseline", "capture-latency", "observation-continuity"),
            "baseline_scenario": "observation-baseline",
            "mode": "observation_only",
        },
        offline={
            "classification": "operator_declared_offline_single_player",
            "confirmation_required": True,
            "evidence": (
                "The operator must confirm the window belongs to an offline or "
                "single-player session. NF-05 does not probe network traffic, so this "
                "classification is a declaration, not a measurement."
            ),
        },
        known_limitations=(
            "Observation only: no action path exists, so gameplay capability is not measured.",
            "The offline/single-player classification is operator-declared, not probed.",
            "No objective verifier, so a completed evaluation is data completeness.",
            "The target must be opened and prepared by the operator; NF-05 never launches it.",
        ),
    ),
    GameRecord(
        game_id="gameplay-objective",
        version=1,
        display_name="Gameplay Objective Evaluation (unsupported)",
        status="UNSUPPORTED",
        status_reason=(
            "Scoring an in-game objective needs a decision pipeline, a gameplay input path "
            "and an objective verifier. None of the three exists in this build, so running "
            "this entry would fabricate both the actions and the score."
        ),
        supported_systems=("Windows", "Linux", "Darwin"),
        evaluation_modes=(),
        launch=_NO_LAUNCH,
        window_detection=_WINDOW_DETECTION,
        capture=_CAPTURE,
        input=_NO_INPUT,
        display_requirements={
            "mode": "windowed",
            "visibility": "fully visible on the primary display",
        },
        required_interfaces=(
            "decision pipeline",
            "Guardian-enforced input path",
            "objective verifier",
        ),
        objective_verification=_NO_VERIFIER,
        benchmark={"scenarios": (), "baseline_scenario": None, "mode": "objective_verified"},
        offline={
            "classification": "not_applicable",
            "confirmation_required": False,
            "evidence": "Unsupported entries cannot run, so no offline claim is made.",
        },
        known_limitations=(
            "Catalog record only: this entry states a missing capability, not a runnable plan.",
            "No game name is claimed: no specific game can be evaluated by this build.",
        ),
    ),
)

_GAMES_BY_ID: dict[str, list[GameRecord]] = {}
for _game in SCENARIOS:
    _GAMES_BY_ID.setdefault(_game.game_id, []).append(_game)
for _versions in _GAMES_BY_ID.values():
    _versions.sort(key=lambda item: item.version)


def latest_game_version(game_id: str) -> int | None:
    versions = _GAMES_BY_ID.get(game_id)
    return versions[-1].version if versions else None


def resolve_game(game_id: str, version: Any = None) -> GameRecord | None:
    """Resolve an exact registry entry; ``None`` means unknown, never a guess."""
    versions = _GAMES_BY_ID.get(str(game_id or "").strip())
    if not versions:
        return None
    if version in (None, ""):
        return versions[-1]
    if isinstance(version, bool):
        return None
    try:
        wanted = int(version)
    except (TypeError, ValueError):
        return None
    return next((item for item in versions if item.version == wanted), None)


def game_catalog() -> dict[str, Any]:
    return {
        "version": EVALUATION_VERSION,
        "schema_version": GAME_SCHEMA_VERSION,
        "games": [item.to_dict() for item in SCENARIOS],
        "runnable_count": sum(1 for item in SCENARIOS if item.runnable),
        "observation_only_count": sum(
            1 for item in SCENARIOS if item.status == "OBSERVATION_ONLY"
        ),
        "controllable_count": sum(1 for item in SCENARIOS if item.control_capable),
        "modes": list(EVALUATION_MODES),
        "note": (
            "No entry is controllable or objectively verifiable in this build: every "
            "runnable environment is observation-only, and a completed evaluation means "
            "its data was recorded, never that a game objective was achieved."
        ),
    }


# ── Preflight readiness ─────────────────────────────────────────────────────


def _check(
    check_id: str,
    label: str,
    status: str,
    *,
    required: bool = True,
    evidence: str = "",
    reason: str = "",
) -> dict[str, Any]:
    if status not in CHECK_STATUSES:  # pragma: no cover - internal misuse
        raise ValueError(f"Unknown readiness status {status!r}.")
    if status in {"fail", "unavailable"} and not reason:
        raise ValueError(f"Readiness check {check_id!r} needs a reason when it is {status}.")
    return {
        "id": check_id,
        "label": label,
        "status": status,
        "required": bool(required),
        "evidence": evidence,
        "reason": reason,
    }


def readiness_checks(
    *,
    game: GameRecord,
    mode: str,
    scenario: BenchmarkScenario,
    target: Mapping[str, Any] | None,
    benchmark_valid: bool,
    benchmark_errors: Sequence[str],
    benchmark_warnings: Sequence[str],
    capture_supported: bool,
    stop_available: bool,
    cleanup_available: bool,
    store_available: bool,
    confirm_offline: bool,
    conflicts: Sequence[str],
    expected_handle: Any = None,
    expected_pid: Any = None,
) -> list[dict[str, Any]]:
    """One row per required condition, with the evidence it was judged on.

    A check that cannot be satisfied is ``fail``; a capability that does not
    exist (an action path, an objective verifier) is reported honestly and is
    not required for an observation-only evaluation. A required failure means
    the evaluation is refused — the caller never bypasses it to obtain a result.
    """
    checks: list[dict[str, Any]] = []

    compatibility = game.runnable and game.supports_scenario(scenario.scenario_id)
    checks.append(
        _check(
            "compatibility",
            "Environment compatibility",
            "pass" if compatibility else "fail",
            evidence=f"{game.key} · status {game.status}",
            reason=(
                ""
                if compatibility
                else (
                    game.status_reason
                    if not game.runnable
                    else f"The environment does not declare scenario {scenario.scenario_id!r}."
                )
            ),
        )
    )

    mode_ok = mode == "observation_only" and mode in game.evaluation_modes
    checks.append(
        _check(
            "evaluation_mode",
            "Evaluation mode",
            "pass" if mode_ok else "fail",
            evidence=f"requested {mode!r}; declared {list(game.evaluation_modes)}",
            reason=(
                ""
                if mode_ok
                else (
                    "Only observation_only is available: no gameplay input path or "
                    "objective verifier exists, so a control evaluation cannot run."
                )
            ),
        )
    )

    identity_ok = bool(
        target
        and str(target.get("title") or "").strip()
        and target.get("handle") not in (None, 0, "")
        and target.get("pid") not in (None, 0, "")
    )
    identity_matches = True
    if target and expected_handle not in (None, ""):
        identity_matches = identity_matches and str(expected_handle) == str(target.get("handle"))
    if target and expected_pid not in (None, ""):
        identity_matches = identity_matches and str(expected_pid) == str(target.get("pid"))
    checks.append(
        _check(
            "target_identity",
            "Target identity",
            "pass" if identity_ok and identity_matches else "fail",
            evidence=(
                f"{target.get('title')} · handle {target.get('handle')} · pid {target.get('pid')}"
                if identity_ok and target
                else ""
            ),
            reason=(
                ""
                if identity_ok and identity_matches
                else (
                    "The target window changed since it was validated; validate again before "
                    "starting."
                    if identity_ok
                    else (
                        "The target could not be measured while another run is active; stop "
                        "it first."
                        if conflicts and not target
                        else (
                            # The measurement did not happen because the rest of the
                            # request was refused first; say THAT instead of blaming
                            # the window, which is still perfectly identifiable.
                            "The target could not be measured because the request was "
                            f"refused first: {benchmark_errors[0]}"
                            if benchmark_errors
                            else "The target window was not uniquely identified with a "
                            "handle and process id."
                        )
                    )
                )
            ),
        )
    )

    ambiguous = any("windows match" in item for item in benchmark_errors)
    # Uniqueness is only known once the target was enumerated. With no target and
    # no ambiguity report, the check is UNAVAILABLE rather than a pass: claiming
    # "one title match" for a window that was never listed would be a green row
    # about a test that did not run.
    uniqueness_judged = ambiguous or target is not None
    checks.append(
        _check(
            "target_uniqueness",
            "Target uniqueness",
            "fail" if ambiguous else ("pass" if uniqueness_judged else "unavailable"),
            evidence=(
                "one title match required; the manager refuses duplicates"
                if uniqueness_judged
                else ""
            ),
            reason=(
                "More than one window matches the target title, so the session could "
                "address the wrong window."
                if ambiguous
                else (
                    ""
                    if uniqueness_judged
                    else "Window uniqueness was not judged: this request did not reach "
                    "the target window."
                )
            ),
        )
    )

    checks.append(
        _check(
            "capture",
            "Fresh-frame capture",
            "pass" if capture_supported else "fail",
            evidence="window-cropped primary-display capture" if capture_supported else "",
            reason=(
                ""
                if capture_supported
                else "Screen capture is not implemented on this platform."
            ),
        )
    )

    observation_ok = scenario.supported and game.supports_scenario(scenario.scenario_id)
    checks.append(
        _check(
            "observation_source",
            "Observation source",
            "pass" if observation_ok else "fail",
            evidence=f"{scenario.key} · {scenario.observation_source}",
            reason=(
                ""
                if observation_ok
                else "The selected scenario is not supported for this environment."
            ),
        )
    )

    verifier_available = bool(game.objective_verification.get("available"))
    checks.append(
        _check(
            "objective_verification",
            "Objective verifier",
            "pass" if verifier_available else "unavailable",
            required=False,
            evidence="verifier attached" if verifier_available else "",
            reason=(
                ""
                if verifier_available
                else str(
                    game.objective_verification.get("reason")
                    or "No objective verifier is available; the result is data completeness."
                )
            ),
        )
    )

    if mode == "control":
        input_ok = bool(game.input.get("available"))
        checks.append(
            _check(
                "input_capability",
                "Gameplay input capability",
                "pass" if input_ok else "fail",
                evidence="input path available" if input_ok else "",
                reason=(
                    ""
                    if input_ok
                    else str(
                        game.input.get("reason")
                        or "No gameplay input path exists in this build."
                    )
                ),
            )
        )
    else:
        checks.append(
            _check(
                "input_capability",
                "Gameplay input capability",
                "pass",
                required=False,
                evidence="observation-only: no input is sent, so none is required",
            )
        )

    checks.append(
        _check(
            "guardian_controls",
            "Guardian stop and cancellation",
            "pass" if stop_available else "fail",
            evidence="stop, cancel and emergency-stop paths are callable" if stop_available else "",
            reason=(
                ""
                if stop_available
                else "The Guardian stop/cancellation path is unavailable, so the run could "
                "not be ended safely."
            ),
        )
    )

    checks.append(
        _check(
            "input_cleanup",
            "Input cleanup",
            "pass" if cleanup_available or mode == "observation_only" else "fail",
            required=mode == "control",
            evidence=(
                "the manager's release path is callable"
                if cleanup_available
                else "observation-only: no input is produced; cleanup still runs on close"
            ),
            reason=(
                ""
                if cleanup_available or mode == "observation_only"
                else "No input-cleanup path is available before an input-producing evaluation."
            ),
        )
    )

    checks.append(
        _check(
            "benchmark_configuration",
            "Benchmark configuration",
            "pass" if benchmark_valid else "fail",
            evidence="; ".join(benchmark_warnings[:1]),
            reason=(
                ""
                if benchmark_valid
                else "; ".join(benchmark_errors) or "The benchmark configuration is invalid."
            ),
        )
    )

    checks.append(
        _check(
            "evidence_persistence",
            "Evidence persistence",
            "pass" if store_available else "fail",
            evidence="the shared state store is attached" if store_available else "",
            reason=(
                ""
                if store_available
                else "No state store is attached, so an evaluation result would not survive "
                "the process."
            ),
        )
    )

    if game.offline_confirmation_required:
        checks.append(
            _check(
                "offline_eligibility",
                "Offline / single-player eligibility",
                "pass" if confirm_offline else "fail",
                evidence=(
                    "the operator confirmed an offline/single-player session"
                    if confirm_offline
                    else str(game.offline.get("evidence") or "")
                ),
                reason=(
                    ""
                    if confirm_offline
                    else "This environment requires an explicit offline/single-player "
                    "confirmation before any evaluation."
                ),
            )
        )
    else:
        checks.append(
            _check(
                "offline_eligibility",
                "Offline / single-player eligibility",
                "pass",
                evidence=str(game.offline.get("evidence") or "not applicable"),
            )
        )

    checks.append(
        _check(
            "conflicts",
            "Exclusive session slot",
            "pass" if not conflicts else "fail",
            evidence="no active benchmark run or session" if not conflicts else "",
            reason="; ".join(conflicts),
        )
    )
    return checks


def readiness_summary(checks: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The ready verdict, its blocking checks, and an honest count per status."""
    blocking = [
        str(check["id"])
        for check in checks
        if check.get("required") and check.get("status") != "pass"
    ]
    counts = {status: 0 for status in CHECK_STATUSES}
    for check in checks:
        status = str(check.get("status") or "unavailable")
        counts[status if status in counts else "unavailable"] += 1
    return {
        "ready": not blocking,
        "blocking": blocking,
        "counts": counts,
        "checks": [dict(check) for check in checks],
        "note": (
            "A required check that is not pass refuses the evaluation. An unavailable "
            "verifier is reported, not treated as success."
        ),
    }


def evaluation_limitations(game: GameRecord, scenario: BenchmarkScenario) -> list[str]:
    """Limitations that apply to every evaluation of this environment."""
    limitations = [
        "A COMPLETED evaluation means the observation recorded its required data; it "
        "never means a game objective was achieved.",
        "No decision pipeline, gameplay input path or objective verifier exists in this "
        "build, so no gameplay capability is demonstrated.",
        "The target must already be open; NF-05 never launches, installs or restarts it.",
        f"Environment compatibility: {game.status} — {game.status_reason}",
        *game.known_limitations,
        *scenario.limitations,
    ]
    return list(dict.fromkeys(limitations))


def missing_evidence(
    *,
    benchmark: Mapping[str, Any] | None,
    timeline: Mapping[str, Any] | None,
    baseline: Mapping[str, Any] | None,
    session_id: str = "",
) -> list[str]:
    """Evidence links that were expected and are absent, each with the reason.

    This is a reading of records that already exist; it never claims evidence it
    did not see.
    """
    gaps: list[str] = []
    if benchmark is None:
        gaps.append("The benchmark run record for this evaluation is unavailable.")
        return gaps
    evidence = dict(benchmark.get("evidence") or {})
    if not evidence.get("frames"):
        gaps.append("No captured frame reference was retained for this run.")
    if not evidence.get("timeline_endpoint"):
        gaps.append("No evidence-timeline endpoint was recorded.")
    if str(evidence.get("timeline_source") or "") != "recorded":
        gaps.append(
            "The timeline is derived from the session record rather than an NF-03 event log."
        )
    persistence = dict(evidence.get("persistence") or {})
    if persistence.get("ok") is not True:
        gaps.append(
            "Timeline persistence is unverified: "
            + str(persistence.get("reason") or "no reason reported")
        )
    if session_id and timeline is None:
        # The session exists (or existed) but its timeline could not be read
        # back. That is missing evidence, and naming it is the point of this
        # function: a report must not imply it saw a log it never loaded.
        gaps.append(
            f"The evidence timeline for session {session_id} could not be read; the "
            "recorded timeline endpoint may be stale or the log may be gone."
        )
    elif timeline is not None:
        for gap in timeline.get("limitations") or []:
            gaps.append(str(gap))
    if baseline is not None and not baseline.get("benchmark_run_id"):
        gaps.append(
            "No compatible baseline exists yet; this evaluation cannot be compared."
        )
    return list(dict.fromkeys(gaps))


def usable_scenarios(game: GameRecord) -> list[BenchmarkScenario]:
    found: list[BenchmarkScenario] = []
    for scenario_id in game.benchmark.get("scenarios") or ():
        scenario = resolve_scenario(str(scenario_id))
        if scenario is not None and scenario.supported:
            found.append(scenario)
    return found


def supported_systems(records: Iterable[GameRecord] | None = None) -> list[str]:
    systems: list[str] = []
    for record in records or SCENARIOS:
        for system in record.supported_systems:
            if system not in systems:
                systems.append(system)
    return systems
