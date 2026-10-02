"""Resource estimation: what a configuration is expected to cost, before it runs.

Training on a 16 GB machine is a question about arithmetic, and the arithmetic
should happen BEFORE a run starts. This module turns a :class:`TrainingConfig`
into a :class:`ResourceEstimate`: the model's footprint, the optimizer and
activation cost of the chosen method, the dataset's token cache, and the
checkpoint reserve — each one a named component, each one summed into a
``required_bytes`` figure that is compared against what is actually free.

The comparison is deliberately conservative in three ways:

  * **an unknown is never a yes.** If the base model's size is unknown, or free
    memory could not be measured, the verdict cannot be ``SAFE``.
  * **the machine's own thresholds are reused.** When a ``ResourceGovernor`` is
    available its headroom is subtracted from free memory; the same reading the
    model loader uses, not a second opinion.
  * **the formula is written down.** Every constant below is a documented
    assumption, and every number it produces is named in ``components`` so an
    operator can see the arithmetic rather than trust a grade.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from novacontrol.training.config import TrainingConfig
from novacontrol.training.hardware import (
    BackendChoice,
    HardwareCapabilities,
    detect_hardware,
    resolve_backend,
)
from novacontrol.training.models import (
    PREPROCESSING_VERSION,
    DatasetType,
    ResourceEstimate,
    ResourceVerdict,
    TrainingMethod,
)

#: Activation memory assumed per token of a micro-batch, in bytes. Conservative
#: for a small transformer; the point is a defensible order of magnitude, not a
#: fake precision. Gradient checkpointing recomputes activations instead of
#: storing them, so it scales this down.
DEFAULT_ACTIVATION_BYTES_PER_TOKEN = 4096
GRADIENT_CHECKPOINTING_FACTOR = 0.35

#: Full fine-tuning keeps an optimizer state (Adam: two fp32 moments = 8 bytes
#: per trainable parameter) plus one gradient copy per parameter.
FULL_GRADIENT_BYTES_PER_PARAM = 2.0
FULL_OPTIMIZER_BYTES_PER_PARAM = 8.0

#: LoRA trains a small adapter: the trainable fraction is an assumption about
#: rank and target modules, not a measurement, and is documented as such.
LORA_TRAINABLE_FRACTION = 0.02
LORA_ADAPTER_MIN_BYTES = 64 * 1024 * 1024
LORA_OPTIMIZER_BYTES_PER_TRAINABLE = 16.0

#: Bytes of tokenised data assumed per dataset token (input ids + labels).
DATASET_BYTES_PER_TOKEN = 2.0

#: Above this fraction of usable memory, the estimate warns instead of passing.
WARNING_FRACTION = 0.75

DEFAULT_PRECISION_BYTES: Mapping[str, float] = {
    "fp32": 4.0,
    "fp16": 2.0,
    "bf16": 2.0,
    "int8": 1.0,
    "int4": 0.5,
}


def precision_bytes(precision: str) -> float:
    return DEFAULT_PRECISION_BYTES.get(str(precision), 4.0)


def _whole(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _length(value: Any) -> int:
    """A defensive ``len`` for duck-typed datasets."""
    try:
        return len(value)
    except TypeError:
        return 0


@dataclass(frozen=True, slots=True)
class DatasetSizeHint:
    """The two dataset facts an estimate needs, with their source explained."""

    example_count: int = 0
    estimated_tokens: int = 0
    dataset_version: str = ""
    dataset_type: str = DatasetType.NLU.value


class ResourceEstimator:
    """Builds a :class:`ResourceEstimate` from a configuration and this machine.

    ``governor`` is anything with an ``assess()`` returning an object with
    ``available_ram_bytes``/``headroom_bytes`` (the application's
    ``ResourceGovernor``); ``monitor`` is anything with
    ``total_ram_bytes()``/``available_ram_bytes()`` (``HardwareMonitor``);
    ``model_size_lookup`` answers a model's footprint (``ModelManager``). All
    three are optional: without them the estimate still computes, and says what
    it could not measure.
    """

    def __init__(
        self,
        *,
        hardware: HardwareCapabilities | None = None,
        governor: Any = None,
        monitor: Any = None,
        model_size_lookup: Callable[[str], int | None] | None = None,
        activation_bytes_per_token: int = DEFAULT_ACTIVATION_BYTES_PER_TOKEN,
        warning_fraction: float = WARNING_FRACTION,
    ) -> None:
        self._hardware = hardware
        self._governor = governor
        self._monitor = monitor
        self._model_size_lookup = model_size_lookup
        self._activation_bytes_per_token = max(1, int(activation_bytes_per_token))
        self._warning_fraction = min(1.0, max(0.0, float(warning_fraction)))

    # -- machine readings ------------------------------------------------------

    def capabilities(self, *, probe_runtime: bool = False) -> HardwareCapabilities:
        """This machine's abilities; an injected reading wins over detection."""
        if self._hardware is not None and not probe_runtime:
            return self._hardware
        detected = detect_hardware(monitor=self._monitor, probe_runtime=probe_runtime)
        if self._hardware is None:
            return detected
        return detected

    def _free_memory(self) -> tuple[int | None, int, str]:
        """(available_bytes, headroom_bytes, source).

        The governor comes first because it knows the headroom it holds back and
        the monitor second because the application already samples it; the
        detected reading (psutil) is the last resort. That order matters for
        honesty in both directions: a figure that WAS measured must not be
        reported as unmeasured — otherwise every estimate on a working machine
        is a WARNING and an operator learns to ignore the verdict — while a
        machine where nothing can measure memory still gets no safe verdict.
        """
        if self._governor is not None:
            try:
                assessment = self._governor.assess()
                available = _whole(getattr(assessment, "available_ram_bytes", None))
                headroom = _whole(getattr(assessment, "headroom_bytes", None)) or 0
                if available is not None:
                    return (available, headroom, "the resource governor's last reading")
            except Exception:  # noqa: BLE001 - an estimate must not break a status call
                pass
        if self._monitor is not None:
            try:
                available = _whole(self._monitor.available_ram_bytes())
                if available is not None:
                    return (available, 0, "the hardware monitor's reading")
            except Exception:  # noqa: BLE001 - an estimate must not break a status call
                pass
        detected = self.capabilities()
        if detected.available_ram_bytes is not None:
            return (detected.available_ram_bytes, 0, "the machine's own memory reading")
        return (None, 0, "free memory could not be measured")

    def _model_size(self, config: TrainingConfig) -> tuple[int | None, str]:
        if config.base_model_size_bytes > 0:
            return (config.base_model_size_bytes, "the configured base_model_size_bytes")
        if self._model_size_lookup is not None and config.base_model:
            try:
                found = self._model_size_lookup(config.base_model)
            except Exception:  # noqa: BLE001 - a lookup must not break an estimate
                found = None
            if isinstance(found, (int, float)) and found > 0:
                return (int(found), "the model manager's reading of the base model")
        return (None, "the base model's size is unknown")

    # -- the estimate ----------------------------------------------------------

    def estimate(
        self,
        config: TrainingConfig,
        *,
        dataset: DatasetSizeHint | Any | None = None,
        example_count: int = 0,
        estimated_tokens: int = 0,
        probe_runtime: bool = False,
        extra_components: Mapping[str, int] | None = None,
        extra_reasons: Sequence[str] = (),
    ) -> ResourceEstimate:
        """Compute every component and a verdict, with the reasons attached.

        ``extra_components`` exists so an objective with a cost this function does
        not know about (a preference run keeps a frozen REFERENCE model beside the
        policy, and feeds two outputs per row instead of one) is counted by the
        same verdict logic instead of by a second estimator with its own comfort
        lines. The extra bytes are summed into the requirement and land in the
        component breakdown, so a report shows where they went.
        """
        if dataset is not None:
            statistics = getattr(dataset, "statistics", None)
            example_count = example_count or int(
                getattr(statistics, "total", 0) or _length(dataset) or 0
            )
            estimated_tokens = estimated_tokens or int(
                getattr(statistics, "estimated_tokens", 0) or 0
            )
        capabilities = self.capabilities(probe_runtime=probe_runtime)
        choice = resolve_backend(config.hardware_policy, capabilities)
        method = config.effective_method
        backend = (
            "dry_run"
            if config.dry_run
            else ("peft_lora" if capabilities.training_dependencies_ready else "unavailable")
        )
        backend_available = bool(
            config.dry_run or capabilities.training_dependencies_ready
        )

        reasons: list[str] = [
            f"backend {backend!r}; device policy resolved to {choice.device or 'none'!r} "
            f"({choice.reason})"
        ]
        if config.dry_run:
            reasons.append(
                "dry_run is on: nothing will be trained, and the estimate is advisory"
            )

        size_bytes, size_source = self._model_size(config)
        parameters = config.base_model_parameters or (
            size_bytes // 2 if size_bytes else None
        )
        if parameters is None:
            reasons.append("parameter count unknown; the estimate is a lower bound")
        elif config.base_model_parameters:
            reasons.append("parameter count taken from the configured base_model_parameters")
        else:
            reasons.append("parameter count inferred from the model size (fp16 file convention)")

        if size_bytes is None and parameters is None:
            weights = 0
            trainable: int | None = None
        else:
            weights = int(
                size_bytes
                if size_bytes
                else int(parameters or 0) * precision_bytes(config.precision)
            )
            if method == TrainingMethod.FULL.value:
                trainable = int(parameters or 0)
            else:
                trainable = int((parameters or 0) * LORA_TRAINABLE_FRACTION)

        components: dict[str, int] = {"weights": int(weights)}
        if method == TrainingMethod.FULL.value:
            trainable_params = trainable or 0
            components["gradients"] = int(trainable_params * FULL_GRADIENT_BYTES_PER_PARAM)
            components["optimizer"] = int(trainable_params * FULL_OPTIMIZER_BYTES_PER_PARAM)
            components["checkpoints"] = int(weights)
            reasons.append(
                "full fine-tuning: gradients + Adam moments are included; this is the "
                "expensive choice and usually the wrong one on a 16 GB machine"
            )
        else:
            adapter = max(
                LORA_ADAPTER_MIN_BYTES,
                int((trainable or 0) * LORA_OPTIMIZER_BYTES_PER_TRAINABLE),
            )
            components["adapter"] = adapter
            components["checkpoints"] = adapter
            reasons.append(
                "LoRA: only the adapter's optimizer state is kept, so the cost is the "
                "base model's footprint plus a small adapter"
            )
        micro_tokens = config.batch_size * config.max_sequence_length
        activation_factor = (
            GRADIENT_CHECKPOINTING_FACTOR if config.gradient_checkpointing else 1.0
        )
        components["activations"] = int(
            micro_tokens * self._activation_bytes_per_token * activation_factor
        )
        if not config.gradient_checkpointing:
            reasons.append("gradient checkpointing is off: activations are kept in full")
        components["dataset"] = int(max(0, estimated_tokens) * DATASET_BYTES_PER_TOKEN)
        for name, value in dict(extra_components or {}).items():
            components[str(name)] = int(value)
        reasons.extend(str(reason) for reason in extra_reasons)
        required = sum(components.values())

        available, headroom, source = self._free_memory()
        usable = None if available is None else max(0, available - headroom)
        reasons.append(
            f"free memory: {available if available is not None else 'unmeasured'} bytes "
            f"({source}); headroom {headroom} bytes"
        )

        level = ResourceVerdict.SAFE.value
        if usable is None:
            level = ResourceVerdict.WARNING.value
            reasons.append(
                "free memory could not be measured, so a safe verdict cannot be given"
            )
        elif required > usable:
            level = ResourceVerdict.UNSAFE.value
            reasons.append(
                f"the estimate needs {required} bytes but only {usable} bytes are usable: "
                "starting this configuration could exhaust the machine"
            )
        elif required > usable * self._warning_fraction:
            level = ResourceVerdict.WARNING.value
            reasons.append(
                f"the estimate needs {required} of {usable} usable bytes, above the "
                f"{self._warning_fraction:.0%} comfort line"
            )
        elif size_bytes is None and parameters is None:
            level = ResourceVerdict.WARNING.value
            reasons.append(
                "the base model's size is unknown, so the requirement is a lower bound"
            )
        else:
            reasons.append(f"the estimate fits comfortably: {required} of {usable} bytes")

        if not backend_available:
            reasons.append(
                "a real training backend is unavailable (missing optional dependencies); "
                "dry-run and dataset validation still work"
            )

        hardware_block: dict[str, Any] = dict(capabilities.to_dict())
        hardware_block["device"] = choice.to_dict()
        hardware_block["preprocessing_version"] = PREPROCESSING_VERSION
        return ResourceEstimate(
            level=level,
            reasons=tuple(reasons),
            backend=backend,
            policy=config.hardware_policy,
            dry_run=config.dry_run,
            model_size_bytes=size_bytes,
            required_bytes=int(required),
            available_bytes=available,
            usable_bytes=usable,
            headroom_bytes=headroom,
            example_count=max(0, int(example_count or 0)),
            estimated_tokens=max(0, int(estimated_tokens or 0)),
            trainable_parameters=trainable,
            components=components,
            hardware=hardware_block,
            backend_available=backend_available,
        )

    def with_capabilities(self, capabilities: HardwareCapabilities) -> ResourceEstimator:
        """A copy of this estimator pinned to a known machine (tests, reports)."""
        return ResourceEstimator(
            hardware=capabilities,
            governor=self._governor,
            monitor=self._monitor,
            model_size_lookup=self._model_size_lookup,
            activation_bytes_per_token=self._activation_bytes_per_token,
            warning_fraction=self._warning_fraction,
        )

    # -- convenience for a status surface --------------------------------------

    def summary(self, config: TrainingConfig | None = None) -> dict[str, Any]:
        """The machine's abilities as a mapping, optionally with a draft estimate."""
        capabilities = self.capabilities()
        choice: BackendChoice = resolve_backend(
            (config or TrainingConfig()).hardware_policy, capabilities
        )
        payload: dict[str, Any] = {
            "hardware": capabilities.to_dict(),
            "device": choice.to_dict(),
            "dependencies": {
                "training_ready": capabilities.training_dependencies_ready,
                "missing": list(capabilities.missing_dependencies()),
            },
        }
        if config is not None:
            payload["estimate"] = self.estimate(config).to_dict()
        return payload


__all__ = [
    "DATASET_BYTES_PER_TOKEN",
    "DEFAULT_ACTIVATION_BYTES_PER_TOKEN",
    "DEFAULT_PRECISION_BYTES",
    "FULL_GRADIENT_BYTES_PER_PARAM",
    "FULL_OPTIMIZER_BYTES_PER_PARAM",
    "GRADIENT_CHECKPOINTING_FACTOR",
    "LORA_ADAPTER_MIN_BYTES",
    "LORA_OPTIMIZER_BYTES_PER_TRAINABLE",
    "LORA_TRAINABLE_FRACTION",
    "WARNING_FRACTION",
    "DatasetSizeHint",
    "ResourceEstimator",
    "precision_bytes",
]
