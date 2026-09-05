"""Logging setup for NovaControl."""

from __future__ import annotations

import logging
from typing import Any


def configure_logging(level: str = "INFO") -> Any:
    """Configure structured logging when Loguru is available, else stdlib logging."""
    try:
        from loguru import logger
    except ModuleNotFoundError:
        logging.basicConfig(
            level=getattr(logging, level.upper(), logging.INFO),
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
        return logging.getLogger("novacontrol")

    logger.remove()
    logger.add(
        sink=lambda message: print(message, end=""),
        level=level.upper(),
        serialize=True,
    )
    return logger
