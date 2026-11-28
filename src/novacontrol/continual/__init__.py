"""Phase 27 — continual learning and self-improvement.

The package is a learning LAYER over work NovaControl already does. It captures
execution evidence, evaluates outcomes from verification rather than from
appearances, retrieves comparable experience, and turns repeatedly successful
plan shapes into versioned, validated, reversible strategy records.

What it deliberately is not:

* not a planner — the existing planner still plans;
* not an executor — the existing controllers still run;
* not a verifier — the reliability layer still verifies, and its verdict is the
  evidence this phase learns from;
* not a memory system — it stores bounded learning evidence, not conversation;
* not a model — every path here is deterministic and offline.
"""

from __future__ import annotations

from novacontrol.continual.engine import ContinualLearningEngine
from novacontrol.continual.evaluate import OutcomeEvaluator
from novacontrol.continual.models import (
    ExecutionEvidence,
    LearningProposal,
    LearningSignal,
    OutcomeClass,
    Recommendation,
    StepEvidence,
    StrategyRecord,
    StrategyStatus,
)
from novacontrol.continual.retrieval import ExperienceRetriever
from novacontrol.continual.signals import SignalExtractor
from novacontrol.continual.store import (
    ContinualLearningStore,
    InMemoryContinualLearningStore,
    JsonContinualLearningStore,
    LearningState,
)
from novacontrol.continual.strategies import StrategyBook

__all__ = [
    "ContinualLearningEngine",
    "ContinualLearningStore",
    "ExecutionEvidence",
    "ExperienceRetriever",
    "InMemoryContinualLearningStore",
    "JsonContinualLearningStore",
    "LearningProposal",
    "LearningSignal",
    "LearningState",
    "OutcomeClass",
    "OutcomeEvaluator",
    "Recommendation",
    "SignalExtractor",
    "StepEvidence",
    "StrategyBook",
    "StrategyRecord",
    "StrategyStatus",
]
