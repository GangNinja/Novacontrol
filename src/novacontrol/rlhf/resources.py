"""What an RL run would cost on THIS machine, before anyone starts one.

The estimator is Phase 16's — same component walk, same free-memory reading,
same 75% comfort line, same SAFE / WARNING / UNSAFE vocabulary, same refusal
when the verdict is UNSAFE. What this module adds is what a ROLLOUT-BASED run
costs that a supervised one does not:

  * **the experience buffer.** Rollouts are collected before they are learnt
    from, and each step carries an observation and a reward. The extra memory
    scales with ``rollout_count × max_steps``, which is exactly the shape of the
    configuration.
  * **the reward dataset.** The rows the run learns from, priced at the same
    bytes/token rate Phase 16 uses.
  * **the reference policy, when a KL term is configured.** A frozen second copy
    of the weights is the difference between fitting and swapping on a machine
    where the model barely fits. With ``kl_coefficient = 0`` there is no second
    copy, and the numbers say so.

The machine here has no discrete GPU and no CUDA requirement anywhere in this
path: the estimator reads what the hardware actually reports, the dry-run
backend runs on the CPU, and a mock rollout is what development uses. Nothing
in this module can start a run — it only prices one.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from novacontrol.rlhf.config import RLTrainingConfig
from novacontrol.rlhf.models import (
    IMPLEMENTED_ALGORITHMS,
    PLANNED_ALGORITHMS,
    RLMode,
)
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

#: Bytes per token for one rollout step's retained observation. Small because a
#: step keeps a summary, not a model activation map; the point is that the term
#: exists and grows with rollouts and steps rather than being invisible.
ROLLOUT_BYTES_PER_TOKEN = 4

#: How many rows an estimate will price in one pass before reporting a lower
#: bound instead of walking an enormous dataset.
ESTIMATE_EXAMPLE_LIMIT = 1_000_000


class RLResourceEstimator:
    """Estimates one RL configuration's cost, reusing the Phase 16 walk."""

    def __init__(
        self,
        *,
        estimator: ResourceEstimator | None = None,
        capabilities: HardwareCapabilities | None = None,
    ) -> None:
        if estimator is None:
            estimator = ResourceEstimator()
        if capabilities is not None:
            # A pinned machine has to pin the ESTIMATE, not just the report: the
            # Phase 16 walk reads free memory from its own reading, so a verdict
            # pinned only in summary() would still follow the host — and read
            # WARNING instead of UNSAFE where nothing can measure memory.
            estimator = estimator.with_capabilities(capabilities)
        self._estimator = estimator
        self._capabilities = capabilities

    # -- the machine -----------------------------------------------------------

    def capabilities(self, *, probe_runtime: bool = False) -> HardwareCapabilities:
        if self._capabilities is not None and not probe_runtime:
            return self._capabilities
        return detect_hardware(probe_runtime=probe_runtime)

    def with_capabilities(self, capabilities: HardwareCapabilities) -> RLResourceEstimator:
        """A copy pinned to a known machine (tests, reports)."""
        return RLResourceEstimator(estimator=self._estimator, capabilities=capabilities)

    # -- the estimate ----------------------------------------------------------

    def _reference_cost(self, config: RLTrainingConfig) -> tuple[int, str]:
        """The frozen reference policy's footprint, or a note that it is unknown."""
        if not config.needs_reference_model:
            return (
                0,
                "no KL term is configured, so no frozen reference policy is counted",
            )
        size = max(0, int(config.base_model_size_bytes))
        parameters = max(0, int(config.base_model_parameters))
        if size:
            return (size, f"the frozen reference policy is a second copy of {size} bytes")
        if parameters:
            computed = int(parameters * precision_bytes(config.precision))
            return (
                computed,
                "the frozen reference policy is a second copy of "
                f"{parameters} parameters at {config.precision}",
            )
        return (
            0,
            "the base model's size is unknown, so the reference policy is NOT "
            "counted: the estimate is a lower bound",
        )

    def estimate(
        self,
        config: RLTrainingConfig,
        *,
        dataset: Any | None = None,
        example_count: int = 0,
        estimated_tokens: int = 0,
        rollout_count: int = 0,
        max_steps: int = 0,
        probe_runtime: bool = False,
    ) -> ResourceEstimate:
        """Every component and a verdict, with the RL extras counted."""
        examples = max(0, int(example_count))
        tokens = max(0, int(estimated_tokens))
        rollouts = max(0, int(rollout_count)) or max(0, int(config.rollout_count))
        steps = max(0, int(max_steps)) or max(0, int(config.max_steps))
        if dataset is not None:
            statistics = getattr(dataset, "statistics", None)
            examples = examples or int(getattr(statistics, "total", 0) or len(dataset) or 0)
            tokens = tokens or int(
                getattr(statistics, "estimated_tokens", 0) or 0
            )
        examples = min(examples, ESTIMATE_EXAMPLE_LIMIT)

        buffer_tokens = rollouts * steps * max(1, int(config.max_sequence_length))
        reference_bytes, reference_note = self._reference_cost(config)
        extras: dict[str, int] = {
            "reward_dataset": int(tokens * DATASET_BYTES_PER_TOKEN),
            "experience_buffer": int(buffer_tokens * ROLLOUT_BYTES_PER_TOKEN),
        }
        if reference_bytes:
            extras["reference_model"] = int(reference_bytes)
        reasons = [
            reference_note,
            (
                f"{rollouts} rollout(s) × {steps} step(s) × "
                f"{config.max_sequence_length} token(s) = {buffer_tokens} buffered "
                f"token(s) at {ROLLOUT_BYTES_PER_TOKEN} bytes each"
            ),
            (
                f"{examples} reward example(s) over {tokens} estimated token(s) at "
                f"{DATASET_BYTES_PER_TOKEN} bytes/token"
            ),
        ]
        if not config.dry_run:
            reasons.append(
                "dry_run is off: a real run needs the optional dependencies, a "
                "wired policy optimizer and this installation's permission"
            )
        return self._estimator.estimate(
            config.as_training_config(),
            dataset=dataset,
            example_count=examples,
            estimated_tokens=tokens,
            probe_runtime=probe_runtime,
            extra_components=extras,
            extra_reasons=reasons,
        )

    # -- reporting -------------------------------------------------------------

    def summary(
        self,
        config: RLTrainingConfig | None = None,
        dataset: Any | None = None,
    ) -> dict[str, Any]:
        """The machine's abilities, plus what the RL objective adds."""
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
                "implemented": list(IMPLEMENTED_ALGORITHMS),
                "planned": list(PLANNED_ALGORITHMS),
                "note": (
                    "only the mock policy optimizer runs in this phase; PPO/GRPO "
                    "are named so a configuration can be planned and priced, and "
                    "a run that asks for them is refused rather than faked"
                ),
            },
            "modes": [member.value for member in RLMode],
            "cuda_required": False,
        }
        if config is not None:
            payload["mode"] = config.mode
            payload["algorithm"] = config.algorithm
            payload["needs_reference_model"] = config.needs_reference_model
            payload["estimate"] = self.estimate(config, dataset=dataset).to_dict()
        return payload


def rl_components(estimate: Mapping[str, Any]) -> dict[str, int]:
    """The component breakdown of an estimate, as whole bytes (a report helper)."""
    components = estimate.get("components")
    if not isinstance(components, Mapping):
        return {}
    return {
        str(name): int(value)
        for name, value in components.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }


def rl_estimate_reasons(estimate: Mapping[str, Any]) -> Sequence[str]:
    reasons = estimate.get("reasons")
    if isinstance(reasons, (list, tuple)):
        return tuple(str(item) for item in reasons)
    return ()


__all__ = [
    "ESTIMATE_EXAMPLE_LIMIT",
    "ROLLOUT_BYTES_PER_TOKEN",
    "RLResourceEstimator",
    "rl_components",
    "rl_estimate_reasons",
]
