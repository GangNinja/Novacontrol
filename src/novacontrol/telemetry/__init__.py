"""Real-time system telemetry: real metrics, or an honest "Unavailable".

See :mod:`novacontrol.telemetry.hardware` for where each number comes from and
why some are sampled on a slow cadence instead of read per request.
"""

from novacontrol.telemetry.hardware import HardwareTelemetry
from novacontrol.telemetry.service import DEFAULT_CADENCE_SECONDS, SystemTelemetry

__all__ = ["DEFAULT_CADENCE_SECONDS", "HardwareTelemetry", "SystemTelemetry"]
