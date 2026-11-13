"""NovaMarket NM-01 contracts: versioning, errors and the lifecycle machine.

NovaMarket is a subsystem of NovaControl, not a second application. Everything
in this module is therefore a *contract* rather than a policy: the version this
build speaks, the states it may be in, and the one machine that is allowed to
change that state. The service in :mod:`novacontrol.novamarket.service` uses
these names; it does not keep a second copy of them.

Why a machine instead of a string field
---------------------------------------
A lifecycle that is only a string can be set to anything from anywhere, which is
how a subsystem ends up reporting ``ready`` after a failed check. Here a
transition that the table does not allow raises, the same state twice is a
no-op rather than a duplicate history entry, and every accepted change is
recorded with a reason and a timestamp. The history is what the status surface
and the tests read; it is bounded so a long-lived process cannot grow it
without limit.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

#: The whole subsystem lives under this name: the runtime module name, the
#: event ``source``, the state-store namespace and the capability namespace.
SUBSYSTEM_NAME = "novamarket"

#: The integration contract between NovaMarket and NovaControl. Bumped with a
#: breaking change to the payload shapes in :mod:`novacontrol.novamarket.events`
#: or to the status/config dictionaries the API serves — never for an addition.
MARKET_CONTRACT_VERSION = "1.0.0"

#: The on-disk shape of the persisted configuration. A stored payload written by
#: a newer build is refused rather than guessed at.
MARKET_SCHEMA_VERSION = 1

#: The current implemented phase. NM-14 integrates the existing phases behind
#: the workspace and audit surfaces; it does not add live execution. The
#: roadmap is complete and no later phase exists.
IMPLEMENTED_PHASE = "NM-14"

#: How many lifecycle transitions the machine keeps. A transition history is
#: observability, not an audit trail: the durable record belongs to the audit
#: subsystem, and the newest entries are the ones anyone reads.
LIFECYCLE_HISTORY_LIMIT = 100


class NovaMarketError(RuntimeError):
    """Base class for every refusal NovaMarket raises.

    One root makes the API's error mapping honest: a caller can catch this and
    return a 4xx/5xx that names the subsystem, while an unrelated bug in the
    process is not swallowed by an overly broad handler.
    """


class ConfigurationError(NovaMarketError, ValueError):
    """A configuration payload is invalid, unsafe or from another schema."""


class DependencyError(NovaMarketError):
    """A mandatory dependency refused to initialise the subsystem."""


class LifecycleError(NovaMarketError):
    """A lifecycle transition is not part of the documented machine."""


class NovaMarketInitializationError(NovaMarketError):
    """Initialisation failed after the subsystem entered ERROR.

    Raised after the state, health, lifecycle event and cleanup are already in
    place, so an API caller can report the failure and a status read can explain
    it. It carries the underlying reason in its message.
    """


class NotOperationalError(NovaMarketError):
    """An operation needs a running subsystem and NovaMarket is not running."""


class DuplicatePriceError(NovaMarketError, ValueError):
    """The same quote (asset, source, observation time) was appended twice."""


class HistoryLimitError(NovaMarketError):
    """An append-only price history reached its configured bound."""


class MarketDataError(NovaMarketError):
    """Base class for a market-data refusal or failure.

    Market data has more failure modes than a lifecycle: a provider may be
    unreachable, refuse a symbol, change its response shape, or return a price
    that fails validation. They are named here so the API can map the user-fixable
    ones to 4xx and an outage to 503 without inspecting exception text.
    """


class MarketDataStoreError(MarketDataError):
    """The durable market-data store is unusable or refuses an operation."""


class InstrumentNotFoundError(MarketDataError, LookupError):
    """No instrument is registered under that identity."""


class UnsupportedIntervalError(MarketDataError, ValueError):
    """No configured provider supplies this interval for this instrument."""


class MarketDataUnavailableError(MarketDataError):
    """No provider could supply validated data; nothing may be invented."""


class ProviderError(MarketDataError):
    """A provider operation failed, with a stable machine-readable code.

    ``retryable`` is the retry decision, made once at the adapter boundary: a
    timeout or a 5xx may succeed on another attempt, while a rejected symbol or
    an authentication failure cannot. ``retry_after`` carries the provider's own
    instruction when it sent one.
    """

    code = "provider_error"
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        provider: str = "",
        retry_after: float | None = None,
    ) -> None:
        self.provider = provider
        self.retry_after = retry_after
        super().__init__(f"{provider}: {message}" if provider else message)


class ProviderUnavailableError(ProviderError):
    """The provider could not be reached or is temporarily failing."""

    code = "provider_unavailable"
    retryable = True


class ProviderTimeoutError(ProviderUnavailableError):
    """The provider did not answer within the configured timeout."""

    code = "provider_timeout"


class ProviderRateLimitedError(ProviderError):
    """The provider refused the request because of its rate limit."""

    code = "provider_rate_limited"
    retryable = True


class ProviderAuthenticationError(ProviderError):
    """The provider rejected or did not receive usable credentials."""

    code = "provider_authentication_failed"


class ProviderSymbolNotFoundError(ProviderError):
    """The provider does not recognise the requested symbol."""

    code = "provider_symbol_not_found"


class ProviderUnsupportedIntervalError(ProviderError, UnsupportedIntervalError):
    """The provider does not supply this interval for this instrument."""

    code = "provider_interval_unsupported"


class ProviderInvalidResponseError(ProviderError):
    """The provider's response is malformed, incomplete or changed shape."""

    code = "provider_invalid_response"


class CircuitOpenError(ProviderUnavailableError):
    """The provider's circuit breaker is open; no request was attempted."""

    code = "provider_circuit_open"
    # Retrying a request that was deliberately not made only burns the caller's
    # retry budget; the breaker's cooldown is the retry clock.
    retryable = False


class WalletError(NovaMarketError):
    """Base for NovaCoin wallet and ledger failures."""


class WalletStoreError(WalletError):
    """The ledger repository could not be opened, read or written."""


class WalletNotFoundError(WalletError, LookupError):
    """No wallet exists with the requested id."""


class WalletConflictError(WalletError, ValueError):
    """A wallet already exists, or its state forbids the requested change."""


class WalletValidationError(WalletError, ValueError):
    """A wallet request was malformed: amount, direction, owner or reference."""


class WalletNotActiveError(WalletError, ValueError):
    """The wallet is frozen or closed, so money may not move in or out of it."""


class InsufficientFundsError(WalletError):
    """A debit would have taken a wallet below zero."""

    def __init__(self, wallet_id: str, available: str, required: str) -> None:
        self.wallet_id = wallet_id
        self.available = available
        self.required = required
        super().__init__(
            f"wallet {wallet_id} holds {available} NC but {required} NC was "
            "requested; a wallet balance is never allowed to go negative"
        )


class IdempotencyConflictError(WalletConflictError):
    """An idempotency key was reused for a genuinely different request."""


class LedgerIntegrityError(WalletError):
    """The ledger would be left unbalanced or inconsistent by this write."""



class EventRadarError(NovaMarketError):
    """Base error for the NM-04 event radar."""


class EventStoreError(EventRadarError):
    """The event store could not be read or written."""


class EventNotFoundError(EventRadarError, LookupError):
    """No event (or evidence, or source) with that identifier."""


class EventValidationError(EventRadarError, ValueError):
    """An event, evidence record or query failed validation."""


class EventStateError(EventRadarError, ValueError):
    """An invalid lifecycle or verification transition was requested."""


class EventSourceError(EventRadarError):
    """A source could not be retrieved or parsed.

    ``code`` is a stable machine-readable reason and ``retryable`` says whether a
    second attempt could plausibly succeed. A malformed feed is not retryable; a
    timeout is.
    """

    code = "source_error"
    retryable = False

    def __init__(self, message: str, *, source_id: str = "") -> None:
        super().__init__(message)
        self.source_id = source_id

    def to_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "code": self.code,
            "retryable": self.retryable,
            "message": str(self),
        }


class SourceUnavailableError(EventSourceError):
    """The source could not be reached."""

    code = "source_unavailable"
    retryable = True


class SourceTimeoutError(SourceUnavailableError):
    """The source did not answer in time."""

    code = "source_timeout"


class SourceRateLimitedError(EventSourceError):
    """The source (or a local quota) refused more requests.

    Deliberately not retryable: answering a 429 with more requests is the one
    thing a rate limit forbids, so the run records the refusal and stops.
    """

    code = "source_rate_limited"
    retryable = False


class SourceMalformedError(EventSourceError):
    """The source answered with content this build cannot parse."""

    code = "source_malformed"


class DiscoveryError(NovaMarketError):
    """Base error for NM-05 asset discovery and watchlists."""


class DiscoveryNotFoundError(DiscoveryError, LookupError):
    """No candidate, watchlist or membership with that identifier."""


class DiscoveryValidationError(DiscoveryError, ValueError):
    """A discovery request or watchlist operation failed validation."""


class DiscoveryStateError(DiscoveryError, ValueError):
    """An illegal watchlist lifecycle transition was requested."""


class LifecycleState(StrEnum):
    """The states NovaMarket may occupy.

    ``READY`` and ``RUNNING`` are deliberately different: READY means the
    configuration and dependencies were validated and the capabilities were
    registered; RUNNING means the subsystem is additionally attached to the
    event bus and accepting control events. A build that initialised but was
    never started must not claim to be running.
    """

    DISABLED = "disabled"
    INITIALIZING = "initializing"
    READY = "ready"
    RUNNING = "running"
    DEGRADED = "degraded"
    ERROR = "error"
    SHUTTING_DOWN = "shutting_down"


#: The documented transition table. Anything absent is a bug in the caller.
ALLOWED_TRANSITIONS: Mapping[LifecycleState, frozenset[LifecycleState]] = {
    LifecycleState.DISABLED: frozenset({LifecycleState.INITIALIZING}),
    LifecycleState.INITIALIZING: frozenset(
        {LifecycleState.READY, LifecycleState.ERROR}
    ),
    # READY can be enabled (RUNNING) or stood down (DISABLED); an ERROR can be
    # entered from READY for a failed health probe rather than an init failure.
    LifecycleState.READY: frozenset(
        {
            LifecycleState.RUNNING,
            # A validated subsystem with no control channel is operational but
            # degraded; that is a real READY outcome, not a fake RUNNING.
            LifecycleState.DEGRADED,
            LifecycleState.DISABLED,
            LifecycleState.ERROR,
            LifecycleState.SHUTTING_DOWN,
        }
    ),
    LifecycleState.RUNNING: frozenset(
        {
            LifecycleState.DEGRADED,
            LifecycleState.DISABLED,
            LifecycleState.ERROR,
            LifecycleState.SHUTTING_DOWN,
        }
    ),
    LifecycleState.DEGRADED: frozenset(
        {
            LifecycleState.RUNNING,
            LifecycleState.DISABLED,
            LifecycleState.ERROR,
            LifecycleState.SHUTTING_DOWN,
        }
    ),
    LifecycleState.ERROR: frozenset(
        {
            LifecycleState.INITIALIZING,
            LifecycleState.DISABLED,
            LifecycleState.SHUTTING_DOWN,
        }
    ),
    LifecycleState.SHUTTING_DOWN: frozenset(
        {LifecycleState.DISABLED, LifecycleState.ERROR}
    ),
}


@dataclass(frozen=True, slots=True)
class LifecycleTransition:
    """One accepted (or attempted-but-unchanged) lifecycle change."""

    from_state: LifecycleState | None
    to_state: LifecycleState
    reason: str
    at: datetime
    changed: bool = True

    def to_dict(self) -> dict[str, object]:
        return {
            "from": self.from_state.value if self.from_state else None,
            "to": self.to_state.value,
            "reason": self.reason,
            "at": self.at.isoformat(),
            "changed": self.changed,
        }


class LifecycleMachine:
    """The only way NovaMarket's state changes.

    ``transition`` is strict about order and forgiving about repetition: asking
    for the state you are already in returns an unchanged entry instead of
    raising, so an idempotent ``enable`` does not need a guard at every call
    site, while a genuinely illegal jump (say ``DISABLED`` to ``RUNNING``) is
    refused with both states named.
    """

    def __init__(
        self,
        *,
        initial: LifecycleState = LifecycleState.DISABLED,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._state = initial
        self._clock = clock or (lambda: datetime.now(UTC))
        self._history: list[LifecycleTransition] = [
            LifecycleTransition(None, initial, "subsystem constructed", self._clock())
        ]

    @property
    def state(self) -> LifecycleState:
        return self._state

    def can_transition(self, target: LifecycleState | str) -> bool:
        wanted = LifecycleState(target)
        return wanted is self._state or wanted in ALLOWED_TRANSITIONS[self._state]

    def transition(
        self, target: LifecycleState | str, *, reason: str = ""
    ) -> LifecycleTransition:
        wanted = LifecycleState(target)
        now = self._clock()
        if wanted is self._state:
            return LifecycleTransition(self._state, wanted, reason, now, changed=False)
        allowed = ALLOWED_TRANSITIONS[self._state]
        if wanted not in allowed:
            raise LifecycleError(
                f"invalid NovaMarket lifecycle transition {self._state.value!r} -> "
                f"{wanted.value!r}; allowed: "
                f"{', '.join(sorted(item.value for item in allowed))}"
            )
        previous = self._state
        self._state = wanted
        entry = LifecycleTransition(previous, wanted, reason, now)
        self._history.append(entry)
        if len(self._history) > LIFECYCLE_HISTORY_LIMIT:
            del self._history[: len(self._history) - LIFECYCLE_HISTORY_LIMIT]
        return entry

    def history(self, *, limit: int = 20) -> tuple[LifecycleTransition, ...]:
        """The newest transitions, oldest first (an empty limit means all)."""
        if limit <= 0:
            return tuple(self._history)
        return tuple(self._history[-limit:])

    @staticmethod
    def transition_table() -> dict[str, tuple[str, ...]]:
        """The machine as data, for the contract endpoint and documentation."""
        return {
            state.value: tuple(sorted(item.value for item in targets))
            for state, targets in ALLOWED_TRANSITIONS.items()
        }

    @staticmethod
    def states() -> tuple[str, ...]:
        return tuple(state.value for state in LifecycleState)


__all__ = [
    "ALLOWED_TRANSITIONS",
    "CircuitOpenError",
    "ConfigurationError",
    "DependencyError",
    "DuplicatePriceError",
    "HistoryLimitError",
    "InstrumentNotFoundError",
    "MarketDataError",
    "MarketDataStoreError",
    "MarketDataUnavailableError",
    "ProviderAuthenticationError",
    "ProviderError",
    "ProviderInvalidResponseError",
    "ProviderRateLimitedError",
    "ProviderSymbolNotFoundError",
    "ProviderTimeoutError",
    "ProviderUnavailableError",
    "ProviderUnsupportedIntervalError",
    "UnsupportedIntervalError",
    "IMPLEMENTED_PHASE",
    "LIFECYCLE_HISTORY_LIMIT",
    "LifecycleError",
    "LifecycleMachine",
    "LifecycleState",
    "LifecycleTransition",
    "MARKET_CONTRACT_VERSION",
    "MARKET_SCHEMA_VERSION",
    "NotOperationalError",
    "NovaMarketError",
    "NovaMarketInitializationError",
    "SUBSYSTEM_NAME",
    "ForecastDataError",
    "ForecastError",
    "ForecastNotFoundError",
    "ForecastStateError",
    "ForecastValidationError",
    "PortfolioDataError",
    "PortfolioError",
    "PortfolioNotFoundError",
    "PortfolioStateError",
    "PortfolioValidationError",
    "RiskValidationError",
    "RiskNotFoundError",
    "RiskStateError",
    "RiskDataError",
]


class ForecastError(NovaMarketError):
    """Any NM-06 failure the caller can name."""


class ForecastNotFoundError(ForecastError, LookupError):
    """A forecast, instrument or model the store does not hold."""


class ForecastValidationError(ForecastError, ValueError):
    """An input the forecasting contract cannot accept."""


class ForecastStateError(ForecastError, ValueError):
    """A lifecycle move that the model state machine refuses."""


class ForecastDataError(ForecastError, ValueError):
    """Insufficient, stale or unusable data for a forecast."""


class PortfolioError(NovaMarketError):
    """Any NM-07 failure the caller can name."""


class PortfolioNotFoundError(PortfolioError, LookupError):
    """A portfolio, proposal or holding the store does not hold."""


class PortfolioValidationError(PortfolioError, ValueError):
    """An input the portfolio contract cannot accept."""


class PortfolioStateError(PortfolioError, ValueError):
    """A lifecycle or application step the current state refuses."""


class PortfolioDataError(PortfolioError, ValueError):
    """A price or dependency a portfolio decision needs is unavailable."""


class RiskValidationError(ValueError):
    """A risk request or policy update is malformed or out of range."""


class RiskNotFoundError(LookupError):
    """A risk decision, alert, policy or proposal does not exist."""


class RiskStateError(RuntimeError):
    """A risk action is not allowed in the current state."""


class RiskDataError(RuntimeError):
    """Mandatory risk data is missing or unusable."""


class StrategyValidationError(ValueError):
    """A strategy definition, configuration or request is invalid."""


class StrategyNotFoundError(LookupError):
    """A strategy, signal, proposal or conflict does not exist."""


class StrategyStateError(RuntimeError):
    """A strategy lifecycle or proposal action is not allowed now."""
