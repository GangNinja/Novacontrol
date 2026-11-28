"""Public Phase 39 service facade."""

from __future__ import annotations

from novacontrol.portfolio.intelligence import PortfolioIntelligenceMixin


class PortfolioService(PortfolioIntelligenceMixin):
    """Portfolio registry plus deterministic portfolio intelligence."""


__all__ = ["PortfolioService"]
