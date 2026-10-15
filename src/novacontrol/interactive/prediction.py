"""Predictions, and the honest answer when there is none.

A prediction in this build is always a claim WITH a provenance: which provider made
it, on what basis, and what it is limited by. Three kinds exist:

``ENVIRONMENT_RULE``
    The environment declares the rule for a non-hidden action. This is the strongest
    one available here, and it is still labelled a rule rather than a model.

``RULE_BASED``
    A rule learned earlier in this session (Phase 23's own knowledge store), or Phase
    22's rule projection reached through an injected bridge.

``UNAVAILABLE``
    Nothing justified a prediction. This is the COMMON case and it is not a failure:
    an experiment can still run — a detector simulator's outcome is not something a
    rule in this build knows in advance — and it will simply not be scored for
    agreement, which the comparison says in words.

No provider here invents a value to fill the field (§6). A learned model, if one is
ever wired, enters through ``WorldModelProvider`` and takes its basis from the
provider it actually asked.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

from novacontrol.interactive.environments import Environment
from novacontrol.interactive.models import (
    ConfidenceKind,
    ExperimentSpec,
    Hypothesis,
    Prediction,
    PredictionBasis,
    as_text,
)

__all__ = [
    "EnvironmentRuleProvider",
    "LearnedRuleProvider",
    "NoPredictionProvider",
    "PredictionProvider",
    "PredictionService",
    "WorldModelProvider",
]

#: What a provider is told about the experiment it is predicting for.
RuleLookup = Callable[[str, str], Mapping[str, Any] | None]


class PredictionProvider(Protocol):
    """Something that can say what it expects, and whether it can say anything."""

    name: str

    @property
    def available(self) -> bool:
        """Whether this provider can answer at all right now."""
        ...

    def predict(
        self,
        spec: ExperimentSpec,
        hypotheses: Sequence[Hypothesis],
        *,
        scope: str = "",
    ) -> Prediction:
        """What this provider expects, or an ``UNAVAILABLE`` prediction with a reason."""
        ...


class NoPredictionProvider:
    """The honest default: nothing here predicts, and it says so every time."""

    name = "none"

    @property
    def available(self) -> bool:
        return False

    def predict(
        self, spec: ExperimentSpec, hypotheses: Sequence[Hypothesis], *, scope: str = ""
    ) -> Prediction:
        del spec, hypotheses, scope
        return Prediction(
            basis=PredictionBasis.UNAVAILABLE,
            provider=self.name,
            reason=(
                "no predictive provider is wired, so no prediction was made; the "
                "experiment can still run and will not be scored for agreement"
            ),
            limitations=("a prediction was not available, which is not the same as a wrong one",),
        )


class EnvironmentRuleProvider:
    """The environment's own declared rule, for actions whose rule is NOT hidden."""

    name = "environment_rule"

    def __init__(self, environment: Environment | None) -> None:
        self._environment = environment

    @property
    def available(self) -> bool:
        return self._environment is not None

    def predict(
        self, spec: ExperimentSpec, hypotheses: Sequence[Hypothesis], *, scope: str = ""
    ) -> Prediction:
        del hypotheses, scope
        environment = self._environment
        if environment is None:
            return Prediction(
                basis=PredictionBasis.UNAVAILABLE,
                provider=self.name,
                reason="no environment is registered, so no rule could be read",
            )
        reader = getattr(environment, "declared_rule", None)
        if not callable(reader):
            return Prediction(
                basis=PredictionBasis.UNAVAILABLE,
                provider=self.name,
                reason=(
                    f"environment {environment.environment_id!r} does not declare a rule for "
                    f"{spec.action!r} to the learner"
                ),
            )
        rule: Any = reader(spec.action)
        if not isinstance(rule, Mapping) or not rule:
            return Prediction(
                basis=PredictionBasis.UNAVAILABLE,
                provider=self.name,
                reason=(
                    f"the rule for {spec.action!r} is not declared (it may be hidden), so "
                    "the environment supplied no prediction"
                ),
            )
        return Prediction(
            basis=PredictionBasis.ENVIRONMENT_RULE,
            expected=dict(rule),
            provider=self.name,
            confidence=1.0,
            confidence_kind=ConfidenceKind.STATED,
            reason=f"the environment declares this transition for {spec.action!r}",
            limitations=(
                "the environment's declared rule, not an observation of what will happen",
            ),
        )


class LearnedRuleProvider:
    """Rules this session learned — the few-shot, in-session half of §8.

    The lookup is injected, so the provider does not reach into the knowledge store
    itself: the manager passes a lookup, and ``None`` means the rule says nothing.
    """

    name = "learned_rule"

    def __init__(self, lookup: RuleLookup | None = None) -> None:
        self._lookup = lookup

    @property
    def available(self) -> bool:
        return self._lookup is not None

    def predict(
        self, spec: ExperimentSpec, hypotheses: Sequence[Hypothesis], *, scope: str = ""
    ) -> Prediction:
        del hypotheses
        if self._lookup is None:
            return Prediction(
                basis=PredictionBasis.UNAVAILABLE,
                provider=self.name,
                reason="no learned-rule lookup is wired",
            )
        found = self._lookup(as_text(spec.action), as_text(scope))
        if not found:
            return Prediction(
                basis=PredictionBasis.UNAVAILABLE,
                provider=self.name,
                reason=f"no learned rule covers {spec.action!r} in this scope",
            )
        return Prediction(
            basis=PredictionBasis.RULE_BASED,
            expected=dict(found),
            provider=self.name,
            confidence_kind=ConfidenceKind.HEURISTIC,
            reason=(
                f"a rule learned in scope {scope!r} predicts this transition; it is "
                "rule-based and provisional unless it was confirmed on held-out cases"
            ),
            limitations=(
                "learned from evidence in this scope; a provisional rule may not generalize",
            ),
        )


class WorldModelProvider:
    """Phase 22's prediction boundary, reached through an injected bridge.

    The bridge is duck-typed on ONE method so importing this module never drags the
    world package in, and a bridge that reports itself unavailable produces an
    ``UNAVAILABLE`` prediction rather than a fabricated one.
    """

    name = "world_model"

    def __init__(self, bridge: Any | None = None) -> None:
        self._bridge = bridge

    @property
    def available(self) -> bool:
        bridge = self._bridge
        if bridge is None:
            return False
        availability = getattr(bridge, "availability", None)
        if callable(availability):
            report: Any = availability()
            if isinstance(report, Mapping):
                return bool(report.get("available"))
        return True

    def predict(
        self, spec: ExperimentSpec, hypotheses: Sequence[Hypothesis], *, scope: str = ""
    ) -> Prediction:
        del hypotheses
        bridge = self._bridge
        if bridge is None:
            return Prediction(
                basis=PredictionBasis.UNAVAILABLE,
                provider=self.name,
                reason="no world-model bridge is wired",
            )
        predict = getattr(bridge, "predict", None)
        if not callable(predict):
            return Prediction(
                basis=PredictionBasis.UNAVAILABLE,
                provider=self.name,
                reason="the world-model bridge cannot predict",
            )
        try:
            answer: Any = predict(spec.action, dict(spec.parameters), scope=scope)
        except Exception as exc:  # noqa: BLE001 - a failing provider is not a prediction
            return Prediction(
                basis=PredictionBasis.UNAVAILABLE,
                provider=self.name,
                reason=f"the world-model bridge raised {type(exc).__name__}: {exc}",
            )
        if not isinstance(answer, Mapping):
            return Prediction(
                basis=PredictionBasis.UNAVAILABLE,
                provider=self.name,
                reason="the world-model bridge returned nothing usable",
            )
        basis_text = as_text(answer.get("basis"))
        basis = (
            PredictionBasis.MODEL
            if basis_text == PredictionBasis.MODEL.value
            else PredictionBasis.RULE_BASED
        )
        values = answer.get("predicted")
        expected = dict(values) if isinstance(values, Mapping) else {}
        if not expected:
            return Prediction(
                basis=PredictionBasis.UNAVAILABLE,
                provider=self.name,
                reason="the world-model bridge answered without a predicted value",
            )
        return Prediction(
            basis=basis,
            expected=expected,
            provider=self.name,
            confidence_kind=ConfidenceKind.HEURISTIC,
            reason=as_text(answer.get("reason"), "the world model supplied this expectation"),
            limitations=(
                "a projection, not a measurement; its basis is reported as it was supplied",
            ),
        )


class PredictionService:
    """Asks each provider in order and reports the first justified answer."""

    def __init__(self, providers: Sequence[PredictionProvider] | None = None) -> None:
        self.providers: tuple[PredictionProvider, ...] = tuple(
            providers or (NoPredictionProvider(),)
        )

    def set_providers(self, providers: Sequence[PredictionProvider]) -> None:
        """Replace the chain — the seam a caller (or a test) wires providers through."""
        self.providers = tuple(providers) or (NoPredictionProvider(),)

    def provider_report(self) -> tuple[dict[str, Any], ...]:
        """What each provider is and whether it can answer — for the status surface."""
        rows: list[dict[str, Any]] = []
        for provider in self.providers:
            rows.append(
                {
                    "name": provider.name,
                    "available": bool(provider.available),
                }
            )
        return tuple(rows)

    def predict(
        self,
        spec: ExperimentSpec,
        hypotheses: Sequence[Hypothesis],
        *,
        scope: str = "",
    ) -> Prediction:
        """The first available prediction, or the reasons every provider declined."""
        reasons: list[str] = []
        for provider in self.providers:
            if not provider.available:
                reasons.append(f"{provider.name}: not available")
                continue
            prediction = provider.predict(spec, hypotheses, scope=scope)
            if prediction.available:
                return prediction
            reasons.append(f"{provider.name}: {prediction.reason or 'declined'}")
        return Prediction(
            basis=PredictionBasis.UNAVAILABLE,
            provider="none",
            reason=(
                "no provider supplied a prediction (" + "; ".join(reasons) + ")"
                if reasons
                else "no prediction provider is wired"
            ),
            limitations=(
                "the experiment may still run: an unavailable prediction means it will not "
                "be scored for agreement, not that it cannot be informative",
            ),
        )
