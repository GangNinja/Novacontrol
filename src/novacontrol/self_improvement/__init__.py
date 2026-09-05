"""Self-coding and improvement intelligence."""

from novacontrol.self_improvement.engine import SelfImprovementEngine
from novacontrol.self_improvement.models import (
    CodeChange,
    CodeChangeResult,
    CodeChangeStatus,
    CodeFinding,
    CodebaseProfile,
    ImprovementAction,
    SelfImprovementPlan,
)

__all__ = [
    "CodeChange",
    "CodeChangeResult",
    "CodeChangeStatus",
    "CodeFinding",
    "CodebaseProfile",
    "ImprovementAction",
    "SelfImprovementEngine",
    "SelfImprovementPlan",
]
