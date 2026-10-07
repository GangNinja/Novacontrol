"""The curriculum: which tasks this run is allowed to learn from, in what order.

The rule the specification states — and the one this module enforces — is that a
run must not move on to complex tasks while simpler ones are failing. So the
curriculum is a STATE MACHINE over the six levels with three transitions that can
all be checked from the record:

    HOLD      not enough episodes at this level to judge it, or it is doing fine
    ADVANCE   the success criteria were met, and there is a level above
    REGRESS   the current level is demonstrably failing and a level below exists

Advancing and regressing both need EVIDENCE: a minimum number of episodes at the
level, and a success rate above (or a failure rate below) a configured line. A
single good episode cannot promote the run, and a single bad one cannot demote it
without the configured patience.

The manager owns no environment: it decides WHICH level's tasks are next, and the
trainer asks it. That keeps the ordering rule in one place instead of being
re-derived by whoever happens to be collecting episodes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from novacontrol.agentic.config import CurriculumConfig
from novacontrol.agentic.environments import ENVIRONMENT_LEVELS, environment_names
from novacontrol.agentic.models import (
    MAX_CURRICULUM_LEVEL,
    MIN_CURRICULUM_LEVEL,
    Episode,
    TaskDifficultyLevel,
    as_int,
    as_mapping,
    as_mappings,
    as_real,
    as_text,
    as_texts,
)
from novacontrol.agentic.rewards import episode_success

#: The three transitions a curriculum evaluation can produce.
CURRICULUM_HOLD = "hold"
CURRICULUM_ADVANCE = "advance"
CURRICULUM_REGRESS = "regress"

CURRICULUM_ACTIONS: tuple[str, ...] = (
    CURRICULUM_HOLD,
    CURRICULUM_ADVANCE,
    CURRICULUM_REGRESS,
)

#: Which environments belong to each level by default. One environment per level,
#: so a level's task set is unambiguous; a caller may override this mapping.
DEFAULT_LEVEL_ENVIRONMENTS: Mapping[int, tuple[str, ...]] = {
    level: tuple(
        name for name in environment_names() if ENVIRONMENT_LEVELS.get(name) == level
    )
    for level in range(MIN_CURRICULUM_LEVEL, MAX_CURRICULUM_LEVEL + 1)
}


@dataclass(frozen=True, slots=True)
class CurriculumStats:
    """What happened at one level, from which the transition is decided."""

    level: int = MIN_CURRICULUM_LEVEL
    episodes: int = 0
    successes: int = 0
    failures: int = 0
    undecided: int = 0
    reward_total: float = 0.0
    steps: int = 0
    safety_stops: int = 0

    @property
    def decided(self) -> int:
        return self.successes + self.failures

    @property
    def success_rate(self) -> float:
        return (self.successes / self.decided) if self.decided else 0.0

    @property
    def failure_rate(self) -> float:
        return (self.failures / self.decided) if self.decided else 0.0

    @property
    def mean_reward(self) -> float:
        return (self.reward_total / self.episodes) if self.episodes else 0.0

    @property
    def mean_steps(self) -> float:
        return (self.steps / self.episodes) if self.episodes else 0.0

    def with_episode(self, episode: Episode) -> CurriculumStats:
        success = episode_success(episode)
        return CurriculumStats(
            level=self.level,
            episodes=self.episodes + 1,
            successes=self.successes + (1 if success is True else 0),
            failures=self.failures + (1 if success is False else 0),
            undecided=self.undecided + (1 if success is None else 0),
            reward_total=self.reward_total + float(episode.total_reward),
            steps=self.steps + episode.length,
            safety_stops=self.safety_stops + (1 if episode.safety_stopped else 0),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "episodes": self.episodes,
            "successes": self.successes,
            "failures": self.failures,
            "undecided": self.undecided,
            "success_rate": round(self.success_rate, 6),
            "failure_rate": round(self.failure_rate, 6),
            "mean_reward": round(self.mean_reward, 6),
            "mean_steps": round(self.mean_steps, 6),
            "safety_stops": self.safety_stops,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CurriculumStats:
        rows = as_mapping(data)
        return cls(
            level=as_int(rows.get("level"), MIN_CURRICULUM_LEVEL),
            episodes=as_int(rows.get("episodes")),
            successes=as_int(rows.get("successes")),
            failures=as_int(rows.get("failures")),
            undecided=as_int(rows.get("undecided")),
            reward_total=as_real(rows.get("reward_total"), 0.0) or 0.0,
            steps=as_int(rows.get("steps")),
            safety_stops=as_int(rows.get("safety_stops")),
        )


@dataclass(frozen=True, slots=True)
class CurriculumDecision:
    """What the curriculum decided after an evaluation, and the evidence for it."""

    action: str = CURRICULUM_HOLD
    from_level: int = MIN_CURRICULUM_LEVEL
    to_level: int = MIN_CURRICULUM_LEVEL
    reason: str = ""
    stats: CurriculumStats = field(default_factory=CurriculumStats)
    criteria: Mapping[str, Any] = field(default_factory=dict)

    @property
    def moved(self) -> bool:
        return self.to_level != self.from_level

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "from_level": self.from_level,
            "to_level": self.to_level,
            "moved": self.moved,
            "reason": self.reason,
            "stats": self.stats.to_dict(),
            "criteria": dict(self.criteria),
        }


class CurriculumManager:
    """Tracks progress per level and decides the next one."""

    def __init__(
        self,
        config: CurriculumConfig | None = None,
        *,
        level_environments: Mapping[int, Sequence[str]] | None = None,
        version: str = "",
    ) -> None:
        self.config = config if config is not None else CurriculumConfig()
        self.level_environments: dict[int, tuple[str, ...]] = {
            int(level): tuple(as_texts(names))
            for level, names in (level_environments or DEFAULT_LEVEL_ENVIRONMENTS).items()
        }
        self.version = version or self.config.version
        self.level = max(
            MIN_CURRICULUM_LEVEL, min(MAX_CURRICULUM_LEVEL, int(self.config.start_level))
        )
        self.start_level = self.level
        self.stats: dict[int, CurriculumStats] = {
            level: CurriculumStats(level=level)
            for level in range(MIN_CURRICULUM_LEVEL, MAX_CURRICULUM_LEVEL + 1)
        }
        self.fail_streak = 0
        self.history: list[Mapping[str, Any]] = []

    # -- recording --------------------------------------------------------------

    def record(self, episode: Episode) -> CurriculumStats:
        """Record one episode at the current level."""
        current = self.stats[self.level]
        updated = current.with_episode(episode)
        self.stats[self.level] = updated
        return updated

    def record_many(self, episodes: Sequence[Episode]) -> CurriculumStats:
        for episode in episodes:
            self.record(episode)
        return self.stats[self.level]

    # -- the decision -----------------------------------------------------------

    def evaluate(self) -> CurriculumDecision:
        """Judge the current level and move only on evidence."""
        config = self.config
        criteria = {
            "min_episodes_per_level": config.min_episodes_per_level,
            "min_success_rate": config.min_success_rate,
            "max_failure_rate": config.max_failure_rate,
            "allow_regression": config.allow_regression,
            "regression_patience": config.regression_patience,
            "max_level": config.max_level,
        }
        stats = self.stats[self.level]
        if not config.enabled:
            return self._decide(
                CURRICULUM_HOLD,
                "the curriculum is disabled, so the level does not change",
                stats,
                criteria,
            )
        if stats.decided < config.min_episodes_per_level:
            return self._decide(
                CURRICULUM_HOLD,
                f"{stats.decided} decided episode(s) at level {self.level} is fewer than "
                f"the {config.min_episodes_per_level} required to judge it",
                stats,
                criteria,
            )
        if stats.success_rate >= config.min_success_rate:
            return self._maybe_advance(stats, criteria)
        if stats.failure_rate > config.max_failure_rate:
            self.fail_streak += 1
            return self._maybe_regress(stats, criteria)
        self.fail_streak = 0
        return self._decide(
            CURRICULUM_HOLD,
            f"level {self.level} is between the success and failure thresholds "
            f"({stats.success_rate:.2f} success), so it is neither passed nor failed",
            stats,
            criteria,
        )

    def _maybe_advance(
        self, stats: CurriculumStats, criteria: Mapping[str, Any]
    ) -> CurriculumDecision:
        if self.level >= self.config.max_level:
            self.fail_streak = 0
            return self._decide(
                CURRICULUM_HOLD,
                f"level {self.level} met its criteria and is the highest configured level",
                stats,
                criteria,
            )
        self.fail_streak = 0
        decision = self._decide(
            CURRICULUM_ADVANCE,
            f"level {self.level} met its criteria with a {stats.success_rate:.2f} success rate",
            stats,
            criteria,
        )
        self.level = decision.to_level
        return decision

    def _maybe_regress(
        self, stats: CurriculumStats, criteria: Mapping[str, Any]
    ) -> CurriculumDecision:
        if not self.config.allow_regression:
            return self._decide(
                CURRICULUM_HOLD,
                f"level {self.level} is failing but regression is disabled",
                stats,
                criteria,
            )
        if self.fail_streak < self.config.regression_patience:
            return self._decide(
                CURRICULUM_HOLD,
                f"level {self.level} is failing ({stats.failure_rate:.2f}) but the "
                f"patience is {self.config.regression_patience} evaluation(s)",
                stats,
                criteria,
            )
        if self.level <= self.start_level:
            return self._decide(
                CURRICULUM_HOLD,
                f"level {self.level} is failing but it is the starting level: there is "
                "nothing simpler to fall back to, so the run should stop rather than "
                "pretend a harder task is progress",
                stats,
                criteria,
            )
        decision = self._decide(
            CURRICULUM_REGRESS,
            f"level {self.level} failed {stats.failure_rate:.2f} of its decided episodes "
            f"for {self.fail_streak} evaluation(s)",
            stats,
            criteria,
        )
        self.level = decision.to_level
        self.fail_streak = 0
        return decision

    def _decide(
        self,
        action: str,
        reason: str,
        stats: CurriculumStats,
        criteria: Mapping[str, Any],
    ) -> CurriculumDecision:
        target = self.level
        if action == CURRICULUM_ADVANCE:
            target = min(self.config.max_level, self.level + 1)
        elif action == CURRICULUM_REGRESS:
            target = max(self.start_level, self.level - 1)
        decision = CurriculumDecision(
            action=action,
            from_level=self.level,
            to_level=target,
            reason=reason,
            stats=stats,
            criteria=criteria,
        )
        self.history.append(decision.to_dict())
        return decision

    # -- the task set -----------------------------------------------------------

    def environments_for(self, level: int | None = None) -> tuple[str, ...]:
        """The environments at one level (the current one by default)."""
        wanted = self.level if level is None else int(level)
        return tuple(self.level_environments.get(wanted, ()))

    def next_environment(self) -> str:
        """The next environment to collect an episode in, at the current level."""
        names = self.environments_for()
        if not names:
            raise KeyError(
                f"no environment is registered for curriculum level {self.level}; "
                "the curriculum cannot advance into a level with no tasks"
            )
        # Deterministic rotation: episodes alternate across a level's tasks so a
        # level is judged on more than one task when it has more than one.
        index = self.stats[self.level].episodes % len(names)
        return names[index]

    @property
    def level_label(self) -> str:
        return TaskDifficultyLevel(min(self.level, MAX_CURRICULUM_LEVEL)).label

    def level_report(self) -> dict[int, Any]:
        return {
            level: stats.to_dict() for level, stats in sorted(self.stats.items())
        }

    def summary(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "level": self.level,
            "level_label": self.level_label,
            "start_level": self.start_level,
            "max_level": self.config.max_level,
            "enabled": self.config.enabled,
            "fail_streak": self.fail_streak,
            "environments": list(self.environments_for()),
            "levels": self.level_report(),
            "note": (
                "the curriculum advances only on its configured criteria and never "
                "moves to a harder level while the current one is failing"
            ),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "config": self.config.to_mapping(),
            "level": self.level,
            "start_level": self.start_level,
            "fail_streak": self.fail_streak,
            "stats": {str(level): item.to_dict() for level, item in sorted(self.stats.items())},
            "history": [dict(item) for item in self.history],
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CurriculumManager:
        rows = as_mapping(data)
        manager = cls(CurriculumConfig.from_mapping(rows.get("config")))
        manager.level = max(
            MIN_CURRICULUM_LEVEL,
            min(MAX_CURRICULUM_LEVEL, as_int(rows.get("level"), manager.level)),
        )
        manager.start_level = max(
            MIN_CURRICULUM_LEVEL,
            min(manager.level, as_int(rows.get("start_level"), manager.level)),
        )
        manager.fail_streak = max(0, as_int(rows.get("fail_streak")))
        for key, value in as_mapping(rows.get("stats")).items():
            try:
                level = int(key)
            except (TypeError, ValueError):
                continue
            manager.stats[level] = CurriculumStats.from_dict(value)
        manager.history = [dict(item) for item in as_mappings(rows.get("history"))]
        manager.version = as_text(rows.get("version"), manager.version)
        return manager


__all__ = [
    "CURRICULUM_ACTIONS",
    "CURRICULUM_ADVANCE",
    "CURRICULUM_HOLD",
    "CURRICULUM_REGRESS",
    "DEFAULT_LEVEL_ENVIRONMENTS",
    "CurriculumDecision",
    "CurriculumManager",
    "CurriculumStats",
]
