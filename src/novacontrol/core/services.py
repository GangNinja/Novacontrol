"""Runtime service container."""

from __future__ import annotations

from collections.abc import Hashable
from inspect import isawaitable
from typing import Any, TypeVar, cast

T = TypeVar("T")


class ServiceContainer:
    """Small explicit registry for core services and adapters."""

    def __init__(self) -> None:
        self._services: dict[Hashable, Any] = {}

    def register(self, key: Hashable, service: Any, *, replace: bool = False) -> None:
        if key in self._services and not replace:
            raise ValueError(f"Service already registered: {key!r}")
        self._services[key] = service

    def resolve(self, key: Hashable, expected_type: type[T] | None = None) -> T:
        if key not in self._services:
            raise KeyError(f"Service is not registered: {key!r}")
        service = self._services[key]
        if expected_type is not None and not isinstance(service, expected_type):
            raise TypeError(f"Service {key!r} is not a {expected_type.__name__}.")
        return cast(T, service)

    def has(self, key: Hashable) -> bool:
        return key in self._services

    def keys(self) -> tuple[Hashable, ...]:
        return tuple(self._services)

    async def dispose(self) -> None:
        """Dispose registered services that expose `aclose`, `close`, or `stop`."""
        for service in reversed(tuple(self._services.values())):
            for method_name in ("aclose", "close", "stop"):
                method = getattr(service, method_name, None)
                if method is None:
                    continue
                result = method()
                if isawaitable(result):
                    await result
                break
