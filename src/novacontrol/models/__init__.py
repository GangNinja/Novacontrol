"""Phase 7: which model, and is there room for it.

The package answers two questions that used to be spread across the codebase —
*what can each model do?* (``profiles.py``) and *what does this machine have?*
(``hardware.py``) — and puts one decision layer on top of both (``manager.py``)
so a request is routed to a model by CAPABILITY and by measured resources rather
than by a name or an assumption.
"""

from novacontrol.models.hardware import (
    DEFAULT_HEADROOM_BYTES,
    HardwareMonitor,
    HardwareSnapshot,
    gpu_memory_bytes,
)
from novacontrol.models.manager import (
    KeepAlivePolicy,
    KeepAliveSettings,
    ModelLoadOutcome,
    ModelManager,
    ModelProvider,
    ModelRuntimeStatus,
    ModelSelection,
    ModelTelemetry,
)
from novacontrol.models.profiles import (
    BASELINE,
    KNOWN_MODELS,
    REPORTED_CAPABILITIES,
    CapabilityMatch,
    ModelCapability,
    ModelProfile,
    ModelRegistry,
    apply_reported,
    infer_profile,
    select_profile,
)

__all__ = [
    "BASELINE",
    "DEFAULT_HEADROOM_BYTES",
    "KNOWN_MODELS",
    "REPORTED_CAPABILITIES",
    "CapabilityMatch",
    "HardwareMonitor",
    "HardwareSnapshot",
    "KeepAlivePolicy",
    "KeepAliveSettings",
    "ModelCapability",
    "ModelLoadOutcome",
    "ModelManager",
    "ModelProfile",
    "ModelProvider",
    "ModelRegistry",
    "ModelRuntimeStatus",
    "ModelSelection",
    "ModelTelemetry",
    "apply_reported",
    "gpu_memory_bytes",
    "infer_profile",
    "select_profile",
]
