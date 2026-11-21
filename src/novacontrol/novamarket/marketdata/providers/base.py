"""Provider adapter seam: transport, retries, rate limits, circuit breakers.

Every provider integration goes through this module. An adapter's job is narrow:
turn one provider's HTTP/JSON response into NovaMarket's provider-neutral
``ProviderQuote`` / ``ProviderBar`` types and report a typed failure when it
cannot. It never writes to the store, never decides freshness, and never marks
its own data valid — those belong to the engine, quality and freshness layers, so
a provider cannot certify itself.

The shared machinery here is equally deliberate:

* **HTTPS only, bounded responses.** A transport refuses a non-HTTPS URL, a URL
  carrying credentials, and a body over the configured ceiling.
* **Bounded retry with backoff.** Only failures an adapter marked retryable are
  retried; a rejected symbol or a bad key is not. ``Retry-After`` wins over the
  computed delay, capped by ``RetryPolicy.max_delay``.
* **A real circuit breaker.** After repeated failures a provider is skipped
  without a request until its cooldown expires, then one half-open probe is
  allowed. This is how a dead provider stops consuming every cycle.
* **Credentials are resolved, never stored.** An adapter receives a reference
  (``env:NAME`` / ``store:NAME``), resolves it through the injected resolver at
  call time, and never includes it in an exception or a log line.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from novacontrol.novamarket.contracts import (
    CircuitOpenError,
    ProviderAuthenticationError,
    ProviderError,
    ProviderInvalidResponseError,
    ProviderRateLimitedError,
    ProviderSymbolNotFoundError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    ProviderUnsupportedIntervalError,
)
from novacontrol.novamarket.marketdata.models import (
    AssetClass,
    CircuitState,
    DataInterval,
    ProviderCapabilities,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids a config import cycle
    from novacontrol.novamarket.config import SecretReference

#: A defensive ceiling for one provider response. A malformed or hostile
#: endpoint must not be able to exhaust memory before validation sees it.
DEFAULT_MAX_BYTES = 2_000_000

#: The user agent identifies this client rather than impersonating a browser.
#: Providers that require a browser identity are not integrated.
_USER_AGENT = "NovaMarket/0.1 (NovaControl market-data client)"


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """A transport response, including non-2xx statuses."""

    status: int
    body: str
    headers: Mapping[str, str]
    url: str

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def header(self, name: str) -> str:
        return self.headers.get(name.lower(), "")


@runtime_checkable
class HttpTransport(Protocol):
    """The one outbound HTTP seam; tests inject a fake implementation."""

    async def get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResponse: ...


class UrllibTransport:
    """Standard-library HTTPS transport with bounded reads and no retries.

    Retries belong to the adapter policy, not the transport; a transport that
    retried on its own would multiply an adapter's retry budget invisibly.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = 15.0,
        max_bytes: int = DEFAULT_MAX_BYTES,
        user_agent: str = _USER_AGENT,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes
        self.user_agent = user_agent

    async def get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        return await asyncio.to_thread(
            self._blocking_get, url, dict(params or {}), dict(headers or {})
        )

    def _blocking_get(
        self, url: str, params: Mapping[str, str], headers: Mapping[str, str]
    ) -> HttpResponse:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme.lower() != "https":
            raise ProviderUnavailableError(
                "only HTTPS provider requests are allowed"
            )
        if parsed.username or parsed.password:
            raise ProviderUnavailableError(
                "provider URLs must not carry credentials"
            )
        query = urllib.parse.urlencode(params)
        target = url + (("&" if "?" in url else "?") + query if query else "")
        request = urllib.request.Request(
            target,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "application/json, text/csv;q=0.9, */*;q=0.1",
                **headers,
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read(self.max_bytes + 1)
                if len(body) > self.max_bytes:
                    raise ProviderInvalidResponseError(
                        "provider response exceeded the size ceiling",
                        provider="",
                    )
                return HttpResponse(
                    status=int(getattr(response, "status", 200)),
                    body=body.decode("utf-8", errors="replace"),
                    headers={
                        str(key).lower(): str(value)
                        for key, value in response.headers.items()
                    },
                    url=target,
                )
        except urllib.error.HTTPError as exc:
            body = exc.read(self.max_bytes + 1)
            return HttpResponse(
                status=int(exc.code),
                body=body.decode("utf-8", errors="replace"),
                headers={
                    str(key).lower(): str(value)
                    for key, value in (exc.headers or {}).items()
                },
                url=target,
            )
        except (TimeoutError, urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
                raise ProviderTimeoutError(
                    "the provider did not answer within the timeout"
                ) from exc
            raise ProviderUnavailableError(
                f"the provider could not be reached: {type(reason).__name__}"
            ) from exc


class SecretResolver(Protocol):
    """Resolves a secret REFERENCE; never returns or logs a stored value."""

    def resolve(self, reference: SecretReference) -> str | None: ...


class EnvironmentSecretResolver:
    """Resolve ``env:NAME`` references from the process environment.

    ``store:NAME`` returns ``None`` unless an operator wires a store resolver;
    pretending the environment is a secret store would hide a missing key.
    The environment mapping is the exact one supplied, so an explicitly empty
    mapping is authoritative rather than falling back to the host environment.
    """

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ = os.environ if environ is None else environ

    def resolve(self, reference: SecretReference) -> str | None:
        if reference.scheme != "env":
            return None
        value = self._environ.get(reference.name, "")
        return value or None


def resolve_credential(
    resolver: SecretResolver | None, reference: str
) -> str | None:
    """Resolve a configured reference, returning ``None`` when unavailable."""
    text = str(reference or "").strip()
    if not text or resolver is None:
        return None
    # Imported here, not at module scope: ``config`` imports the freshness
    # settings, whose package imports the engine and therefore this module. A
    # top-level import would close that cycle and break `import novacontrol`.
    from novacontrol.novamarket.config import SecretReference

    try:
        parsed = SecretReference.parse(text)
    except ValueError:
        return None
    return resolver.resolve(parsed)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Bounded exponential backoff with jitter."""

    max_attempts: int = 3
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 10.0
    jitter_ratio: float = 0.1

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.base_delay_seconds < 0 or self.max_delay_seconds < 0:
            raise ValueError("retry delays must not be negative")
        if not 0 <= self.jitter_ratio <= 1:
            raise ValueError("jitter_ratio must be between 0 and 1")

    def delay_for(self, attempt: int, retry_after: float | None = None) -> float:
        if retry_after is not None and retry_after >= 0:
            return min(float(retry_after), self.max_delay_seconds)
        base = self.base_delay_seconds * (2 ** max(attempt - 1, 0))
        delay: float = min(base, self.max_delay_seconds)
        if self.jitter_ratio:
            delay *= 1 + random.uniform(0, self.jitter_ratio)
        return delay


class RateLimiter:
    """A per-provider minimum interval between requests."""

    def __init__(
        self,
        min_interval_seconds: float,
        *,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], Any] | None = None,
    ) -> None:
        if min_interval_seconds < 0:
            raise ValueError("min_interval_seconds must not be negative")
        self.min_interval_seconds = float(min_interval_seconds)
        self._clock = clock or time.monotonic
        self._sleeper = sleeper or asyncio.sleep
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        if self.min_interval_seconds <= 0:
            return
        async with self._lock:
            now = self._clock()
            wait = self.min_interval_seconds - (now - self._last)
            if wait > 0:
                result = self._sleeper(wait)
                if hasattr(result, "__await__"):
                    await result
            self._last = self._clock()


class DailyQuota:
    """A process-local daily request ceiling, reset at UTC midnight.

    A provider that publishes a 25-requests/day free tier must not be driven
    until it returns a rate-limit error every day. This counter makes the limit
    explicit; it is process-local, so a restart resets it, and that limitation is
    documented rather than hidden.
    """

    def __init__(
        self,
        limit: int | None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if limit is not None and limit < 1:
            raise ValueError("daily quota must be positive")
        self.limit = limit
        self._clock = clock or (lambda: datetime.now(UTC))
        self._day = self._clock().date()
        self._used = 0
        self._lock = asyncio.Lock()

    @property
    def used(self) -> int:
        return self._used

    def _roll(self) -> None:
        today = self._clock().date()
        if today != self._day:
            self._day = today
            self._used = 0

    async def take(self, provider: str) -> None:
        if self.limit is None:
            return
        async with self._lock:
            self._roll()
            if self._used >= self.limit:
                tomorrow = datetime.combine(
                    self._day, datetime.min.time(), tzinfo=UTC
                ).replace(hour=0)
                from datetime import timedelta

                remaining = (
                    tomorrow + timedelta(days=1) - self._clock()
                ).total_seconds()
                raise ProviderRateLimitedError(
                    f"the local daily request ceiling ({self.limit}) is exhausted",
                    provider=provider,
                    retry_after=max(remaining, 1.0),
                )
            self._used += 1


class CircuitBreaker:
    """One circuit per provider: closed, open, or a single half-open probe."""

    def __init__(
        self,
        provider: str,
        *,
        failure_threshold: int = 3,
        cooldown_seconds: float = 60.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be at least 1")
        if cooldown_seconds < 0:
            raise ValueError("cooldown_seconds must not be negative")
        self.provider = provider
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self._clock = clock or time.monotonic
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._half_open_in_flight = False
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        if self._state is CircuitState.OPEN:
            if self._clock() - self._opened_at >= self.cooldown_seconds:
                return CircuitState.HALF_OPEN
        return self._state

    async def before_call(self) -> None:
        async with self._lock:
            current = self.state
            if current is CircuitState.OPEN:
                remaining = self.cooldown_seconds - (self._clock() - self._opened_at)
                raise CircuitOpenError(
                    f"circuit is open for another {max(remaining, 0):.1f}s",
                    provider=self.provider,
                    retry_after=max(remaining, 0.0),
                )
            if current is CircuitState.HALF_OPEN:
                self._state = CircuitState.HALF_OPEN
                if self._half_open_in_flight:
                    raise CircuitOpenError(
                        "a half-open probe is already in flight",
                        provider=self.provider,
                        retry_after=self.cooldown_seconds,
                    )
                self._half_open_in_flight = True

    async def record_success(self) -> None:
        async with self._lock:
            self._state = CircuitState.CLOSED
            self._failures = 0
            self._half_open_in_flight = False

    async def record_failure(self) -> None:
        async with self._lock:
            self._failures += 1
            self._half_open_in_flight = False
            if (
                self._state is CircuitState.HALF_OPEN
                or self._failures >= self.failure_threshold
            ):
                self._state = CircuitState.OPEN
                self._opened_at = self._clock()

    @property
    def failures(self) -> int:
        return self._failures


async def with_retries(
    operation: Callable[[], Any],
    *,
    provider: str,
    policy: RetryPolicy | None = None,
    sleeper: Callable[[float], Any] | None = None,
    observer: Callable[[str, int, float, ProviderError], None] | None = None,
) -> Any:
    """Run an operation with bounded retries for retryable provider failures."""
    retry = policy or RetryPolicy()
    pause = sleeper or asyncio.sleep
    last: ProviderError | None = None
    for attempt in range(1, retry.max_attempts + 1):
        try:
            return await operation()
        except ProviderError as exc:
            last = exc
            if not exc.retryable or attempt >= retry.max_attempts:
                raise
            delay = retry.delay_for(attempt, exc.retry_after)
            if observer is not None:
                try:
                    observer(provider, attempt, delay, exc)
                except Exception:  # noqa: BLE001 - telemetry must not change retry policy
                    pass
            result = pause(delay)
            if hasattr(result, "__await__"):
                await result
    assert last is not None  # loop always raises or returns
    raise last


def _decimal(value: object, *, field_name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise ProviderInvalidResponseError(
            f"provider returned {field_name} as {type(value).__name__}, not a "
            "decimal"
        )
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ProviderInvalidResponseError(
            f"provider returned a non-numeric {field_name}"
        ) from exc
    if not result.is_finite():
        raise ProviderInvalidResponseError(
            f"provider returned a non-finite {field_name}"
        )
    return result


def _aware(value: object, *, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ProviderInvalidResponseError(
            f"provider returned an invalid {field_name}; a timezone-aware "
            "datetime is required"
        )
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class ProviderQuote:
    """A provider's parsed spot observation, before NovaMarket classification."""

    price: Decimal
    currency: str
    observed_at: datetime
    source_timezone: str
    provider_metadata: Mapping[str, Any] = field(default_factory=dict)
    raw: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "price", _decimal(self.price, field_name="price"))
        object.__setattr__(self, "observed_at", _aware(self.observed_at, field_name="observed_at"))
        currency = str(self.currency or "").strip().upper()
        if not currency:
            raise ProviderInvalidResponseError("provider returned no currency")
        object.__setattr__(self, "currency", currency)
        if not str(self.source_timezone or "").strip():
            raise ProviderInvalidResponseError(
                "provider returned no source timezone"
            )


@dataclass(frozen=True, slots=True)
class ProviderBar:
    """A provider's parsed OHLCV bar, before structural validation."""

    interval: DataInterval
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None
    currency: str
    observed_at: datetime
    source_timezone: str
    partial: bool = False
    provider_metadata: Mapping[str, Any] = field(default_factory=dict)
    raw: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("open", "high", "low", "close"):
            object.__setattr__(
                self, name, _decimal(getattr(self, name), field_name=name)
            )
        if self.volume is not None:
            object.__setattr__(
                self, "volume", _decimal(self.volume, field_name="volume")
            )
        object.__setattr__(self, "interval", DataInterval(self.interval))
        object.__setattr__(self, "observed_at", _aware(self.observed_at, field_name="observed_at"))
        currency = str(self.currency or "").strip().upper()
        if not currency:
            raise ProviderInvalidResponseError("provider returned no currency")
        object.__setattr__(self, "currency", currency)
        if not str(self.source_timezone or "").strip():
            raise ProviderInvalidResponseError(
                "provider returned no source timezone"
            )


@dataclass(frozen=True, slots=True)
class ProviderInstrument:
    """A provider's instrument lookup result."""

    provider_symbol: str
    symbol: str
    name: str
    asset_class: AssetClass
    exchange: str
    mic: str
    currency: str
    timezone: str
    metadata: Mapping[str, Any] = field(default_factory=dict)


class MarketDataAdapter:
    """Base class for a provider adapter; subclasses implement the fetches."""

    name: str = ""
    #: Whether a standalone liveness probe exists. A provider without one is not
    #: unhealthy; its health comes from real quote/OHLCV calls.
    supports_probe: bool = False

    def __init__(
        self,
        *,
        transport: HttpTransport,
        capabilities: ProviderCapabilities,
        limiter: RateLimiter | None = None,
        retry_policy: RetryPolicy | None = None,
        secret_resolver: SecretResolver | None = None,
        daily_quota: DailyQuota | None = None,
    ) -> None:
        if not self.name:
            raise ValueError("an adapter must name itself")
        self._transport = transport
        self.capabilities = capabilities
        self._limiter = limiter or RateLimiter(0.0)
        self._retry = retry_policy or RetryPolicy()
        self._secrets = secret_resolver
        self._quota = daily_quota
        self._retry_observer: Callable[[str, int, float, ProviderError], None] | None = None

    def set_retry_observer(
        self, observer: Callable[[str, int, float, ProviderError], None] | None
    ) -> None:
        """Attach the engine's retry counter without exposing provider internals."""
        self._retry_observer = observer

    async def _get_json(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        await self._limiter.acquire()
        if self._quota is not None:
            await self._quota.take(self.name)

        async def attempt() -> Any:
            response = await self._transport.get(url, params=params, headers=headers)
            return self._parse_response(response)

        return await with_retries(
            attempt,
            provider=self.name,
            policy=self._retry,
            observer=self._retry_observer,
        )

    def _parse_response(self, response: HttpResponse) -> Any:
        if response.status in (401, 403):
            raise ProviderAuthenticationError(
                "the provider rejected the credentials", provider=self.name
            )
        if response.status == 404:
            raise ProviderSymbolNotFoundError(
                "the provider did not find the requested symbol",
                provider=self.name,
            )
        if response.status == 429:
            raise ProviderRateLimitedError(
                "the provider's rate limit refused the request",
                provider=self.name,
                retry_after=_retry_after(response),
            )
        if response.status >= 500:
            raise ProviderUnavailableError(
                f"the provider returned HTTP {response.status}",
                provider=self.name,
                retry_after=_retry_after(response),
            )
        if not response.ok:
            raise ProviderInvalidResponseError(
                f"the provider returned HTTP {response.status}",
                provider=self.name,
            )
        text = response.body.strip()
        if not text:
            raise ProviderInvalidResponseError(
                "the provider returned an empty response", provider=self.name
            )
        try:
            return json.loads(text, parse_float=Decimal)
        except ValueError as exc:
            raise ProviderInvalidResponseError(
                "the provider response is not valid JSON", provider=self.name
            ) from exc

    # -- extension points -----------------------------------------------------

    async def fetch_quote(self, instrument: Any) -> ProviderQuote:
        raise ProviderError(
            f"{self.name} does not implement latest-price reads",
            provider=self.name,
        )

    async def fetch_ohlcv(
        self,
        instrument: Any,
        interval: DataInterval | str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 250,
    ) -> tuple[ProviderBar, ...]:
        raise ProviderUnsupportedIntervalError(
            f"{self.name} does not implement OHLCV reads",
            provider=self.name,
        )

    async def lookup(self, provider_symbol: str) -> ProviderInstrument:
        raise ProviderError(
            f"{self.name} does not implement instrument lookup",
            provider=self.name,
        )

    async def probe(self) -> str:
        """A cheap health probe; the default says none is implemented."""
        raise ProviderError(
            f"{self.name} has no standalone health probe",
            provider=self.name,
        )


def _retry_after(response: HttpResponse) -> float | None:
    raw = response.header("retry-after")
    if not raw:
        return None
    try:
        return max(float(raw), 0.0)
    except ValueError:
        return None


__all__ = [
    "CircuitBreaker",
    "DailyQuota",
    "DEFAULT_MAX_BYTES",
    "EnvironmentSecretResolver",
    "HttpResponse",
    "HttpTransport",
    "MarketDataAdapter",
    "ProviderBar",
    "ProviderInstrument",
    "ProviderQuote",
    "RateLimiter",
    "RetryPolicy",
    "SecretResolver",
    "UrllibTransport",
    "resolve_credential",
    "with_retries",
]
