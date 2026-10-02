"""What a preference run would cost on THIS machine, before it starts.

The estimator is Phase 16's — the same component walk, the same free-memory
reading, the same 75% comfort line, the same SAFE / WARNING / UNSAFE vocabulary
and the same refusal when the verdict is UNSAFE. What this module adds is the
part a preference objective costs that a supervised run does not:

  * **the preference pairs themselves.** A pair feeds a model a prompt AND two
    outputs, so the data component is roughly twice a supervised example's, and it
    is priced from the dataset's own token estimate rather than a guess.
  * **the reference model, for DPO.** The reference is a frozen second copy of the
    weights; on a machine where the base model barely fits, that copy is the
    difference between a safe run and one that starts swapping. ORPO keeps no
    reference, and this is the only place the difference reaches the numbers.

Nothing here is a gate: it returns a verdict with its reasons, and the manager is
what refuses to start an UNSAFE run.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from novacontrol.preference.config import PreferenceTrainingConfig
from novacontrol.preference.models import PreferenceAlgorithm, PreferenceDatasetVersion
from novacontrol.training.hardware import (
    BackendChoice,
    HardwareCapabilities,
    detect_hardware,
    resolve_backend,
)
from novacontrol.training.models import ResourceEstimate
from novacontrol.training.resources import (
    DATASET_BYTES_PER_TOKEN,
    ResourceEstimator,
    precision_bytes,
)

#: Bytes per token for the pairs themselves. Twice a supervised example's, because
#: the chosen and the rejected output are both carried through the run.
PREFERENCE_PAIR_BYTES_PER_TOKEN = DATASET_BYTES_PER_TOKEN * 2

#: How many pairs one build will price in a single estimate before it reports a
#: figure as a lower bound instead of reading every row.
ESTIMATE_PAIR_LIMIT = 1_000_000


class PreferenceResourceEstimator:
    """Estimates one preference configuration's cost, reusing the Phase 16 walk."""

    def __init__(
        self,
        *,
        estimator: ResourceEstimator | None = None,
        capabilities: HardwareCapabilities | None = None,
    ) -> None:
        self._estimator = estimator if estimator is not None else ResourceEstimator()
        self._capabilities = capabilities

    # -- the machine -----------------------------------------------------------

    def capabilities(self, *, probe_runtime: bool = False) -> HardwareCapabilities:
        if self._capabilities is not None and not probe_runtime:
            return self._capabilities
        return detect_hardware(probe_runtime=probe_runtime)

    def with_capabilities(self, capabilities: HardwareCapabilities) -> PreferenceResourceEstimator:
        """A copy pinned to a known machine (tests, reports)."""
        return PreferenceResourceEstimator(
            estimator=self._estimator, capabilities=capabilities
        )

    # -- the estimate ----------------------------------------------------------

    def _reference_cost(self, config: PreferenceTrainingConfig) -> tuple[int, str]:
        """The frozen reference model's footprint, or a note that it is unknown."""
        if not config.needs_reference_model:
            return (
                0,
                f"algorithm={config.algorithm} keeps no reference model, so no second "
                "copy of the weights is counted",
            )
        size = max(0, int(config.base_model_size_bytes))
        parameters = max(0, int(config.base_model_parameters))
        if size:
            return (size, f"the DPO reference model is a second copy of {size} bytes")
        if parameters:
            computed = int(parameters * precision_bytes(config.precision))
            return (
                computed,
                f"the DPO reference model is a second copy of {parameters} parameters "
                f"at {config.precision}",
            )
        return (
            0,
            "the base model's size is unknown, so the DPO reference model is NOT "
            "counted: the estimate is a lower bound",
        )

    def estimate(
        self,
        config: PreferenceTrainingConfig,
        *,
        dataset: PreferenceDatasetVersion | Any | None = None,
        pair_count: int = 0,
        estimated_tokens: int = 0,
        probe_runtime: bool = False,
    ) -> ResourceEstimate:
        """Every component and a verdict, with the preference extras counted."""
        pairs = max(0, int(pair_count))
        tokens = max(0, int(estimated_tokens))
        if dataset is not None:
            statistics = getattr(dataset, "statistics", None)
            pairs = pairs or int(getattr(statistics, "total", 0) or len(dataset) or 0)
            tokens = tokens or int(getattr(statistics, "estimated_tokens", 0) or 0)
        pairs = min(pairs, ESTIMATE_PAIR_LIMIT)

        reference_bytes, reference_note = self._reference_cost(config)
        extras: dict[str, int] = {
            "preference_pairs": int(tokens * PREFERENCE_PAIR_BYTES_PER_TOKEN),
        }
        if reference_bytes:
            extras["reference_model"] = int(reference_bytes)
        reasons = [
            reference_note,
            (
                f"{pairs} preference pair(s) priced at {PREFERENCE_PAIR_BYTES_PER_TOKEN} "
                f"bytes/token over {tokens} estimated tokens"
            ),
        ]
        return self._estimator.estimate(
            config.as_training_config(),
            dataset=dataset,
            example_count=pairs,
            estimated_tokens=tokens,
            probe_runtime=probe_runtime,
            extra_components=extras,
            extra_reasons=reasons,
        )

    # -- reporting -------------------------------------------------------------

    def summary(
        self,
        config: PreferenceTrainingConfig | None = None,
        dataset: PreferenceDatasetVersion | None = None,
    ) -> dict[str, Any]:
        """The machine's abilities, plus what the preference objective adds."""
        capabilities = self.capabilities()
        policy = config.hardware_policy if config is not None else "auto"
        choice: BackendChoice = resolve_backend(policy, capabilities)
        payload: dict[str, Any] = {
            "hardware": capabilities.to_dict(),
            "device": choice.to_dict(),
            "dependencies": {
                "training_ready": capabilities.training_dependencies_ready,
                "missing": list(capabilities.missing_dependencies()),
            },
            "algorithms": {
                member.value: {
                    "needs_reference_model": member is PreferenceAlgorithm.DPO,
                }
                for member in PreferenceAlgorithm
            },
        }
        if config is not None:
            payload["algorithm"] = config.algorithm
            payload["needs_reference_model"] = config.needs_reference_model
            payload["estimate"] = self.estimate(config, dataset=dataset).to_dict()
        return payload


def preference_components(estimate: Mapping[str, Any]) -> dict[str, int]:
    """The component breakdown of an estimate, as whole bytes (a report helper)."""
    components = estimate.get("components")
    if not isinstance(components, Mapping):
        return {}
    return {
        str(name): int(value)
        for name, value in components.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }


__all__ = [
    "ESTIMATE_PAIR_LIMIT",
    "PREFERENCE_PAIR_BYTES_PER_TOKEN",
    "PreferenceResourceEstimator",
    "preference_components",
]
