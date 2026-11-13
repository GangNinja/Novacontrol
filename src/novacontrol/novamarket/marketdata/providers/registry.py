"""The provider registry: installed adapters, priority order and verification.

A registry is not a capability advertisement. It knows which adapters this build
installed, and it reports each adapter's DECLARED capabilities separately from
the operations that have actually succeeded (the ``verified_operations`` that the
store records). ``/market/providers`` therefore cannot claim OHLCV support that
has never been exercised.

Priority is configuration, not code: the order of ``data_providers`` is the
fallback order, and the same order decides which provider's observation is
selected when several agree within tolerance.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from typing import Any

from novacontrol.novamarket.contracts import MarketDataError
from novacontrol.novamarket.marketdata.models import (
    ProviderCapabilities,
    ProviderHealth,
)
from novacontrol.novamarket.marketdata.providers.alpha_vantage import (
    AlphaVantageAdapter,
)
from novacontrol.novamarket.marketdata.providers.base import (
    HttpTransport,
    SecretResolver,
)
from novacontrol.novamarket.marketdata.providers.coingecko import CoinGeckoAdapter
from novacontrol.novamarket.marketdata.providers.yahoo import YahooAdapter


class ProviderRegistry:
    """Installed adapters, by lower-case provider name."""

    def __init__(self, adapters: Iterable[Any]) -> None:
        installed: dict[str, Any] = {}
        for adapter in adapters:
            name = str(getattr(adapter, "name", "") or "").strip().lower()
            if not name:
                raise MarketDataError("a provider adapter has no name")
            if name in installed:
                raise MarketDataError(f"duplicate provider adapter: {name}")
            if not isinstance(getattr(adapter, "capabilities", None), ProviderCapabilities):
                raise MarketDataError(
                    f"provider {name!r} has no ProviderCapabilities declaration"
                )
            installed[name] = adapter
        self._adapters = installed

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))

    def get(self, name: str) -> Any:
        key = str(name or "").strip().lower()
        try:
            return self._adapters[key]
        except KeyError as exc:
            raise MarketDataError(
                f"unknown market-data provider {name!r}; installed: "
                f"{', '.join(self.names) or 'none'}"
            ) from exc

    def enabled(self, names: Sequence[str]) -> tuple[Any, ...]:
        """Adapters in the configured priority order; every name must exist."""
        ordered: list[Any] = []
        seen: set[str] = set()
        for name in names:
            key = str(name or "").strip().lower()
            if key in seen:
                raise MarketDataError(f"duplicate provider in configuration: {key}")
            seen.add(key)
            ordered.append(self.get(key))
        return tuple(ordered)

    def capabilities(self) -> tuple[ProviderCapabilities, ...]:
        return tuple(
            adapter.capabilities for adapter in self._adapters.values()
        )

    def capability_matrix(
        self, health: Mapping[str, ProviderHealth] | None = None
    ) -> tuple[dict[str, Any], ...]:
        """Declared capabilities merged with what the store has verified."""
        health_map = health or {}
        rows: list[dict[str, Any]] = []
        for name in self.names:
            adapter = self._adapters[name]
            declared: ProviderCapabilities = adapter.capabilities
            state = health_map.get(name)
            verified = (
                tuple(
                    operation
                    for operation in state.verified_operations
                    if declared.supports(operation)
                )
                if state
                else ()
            )
            resolved = replace(declared, verified_operations=verified)
            payload = resolved.to_dict()
            payload["health"] = (
                state.to_dict()
                if state
                else ProviderHealth(provider=name).to_dict()
            )
            rows.append(payload)
        return tuple(rows)


def build_adapters(
    transport: HttpTransport,
    *,
    credentials_ref: str = "",
    secret_resolver: SecretResolver | None = None,
    clock: Any | None = None,
) -> tuple[Any, ...]:
    """The three installed adapters, with their verified-limits defaults."""
    return (
        YahooAdapter(
            transport=transport,
            secret_resolver=secret_resolver,
            clock=clock,
        ),
        CoinGeckoAdapter(
            transport=transport,
            secret_resolver=secret_resolver,
            clock=clock,
        ),
        AlphaVantageAdapter(
            transport=transport,
            credentials_ref=credentials_ref,
            secret_resolver=secret_resolver,
            clock=clock,
        ),
    )


__all__ = ["ProviderRegistry", "build_adapters"]
