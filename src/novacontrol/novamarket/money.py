"""Deterministic money, timestamped quotes and the append-only price contract.

Three invariants live here, and they are implemented rather than documented:

* **NovaCoin (NC) is the internal accounting unit and 1 NC = 1 INR.** One type
  carries the amount and the currency, one conversion is defined, and portfolio
  reporting uses the same ``INR`` currency everywhere.
* **Money uses decimal arithmetic.** A binary float is refused at the door
  instead of being silently coerced, because ``0.1 + 0.2`` is not ``0.3`` in the
  arithmetic that a ledger must be able to reproduce. Amounts are quantized to
  paise with ``ROUND_HALF_EVEN`` so every operation has the same rounding rule.
* **A market price carries its provenance and its time.** ``MarketQuote``
  cannot be built without a timezone-aware observation time, a retrieval time
  and a source, and ``PriceHistory`` only appends: there is no update or delete,
  and appending the same identity twice is an error rather than a silent
  overwrite.

NM-02 will implement collection and durable storage. It will implement them
*behind* :class:`MarketDataProvider` and the append-only contract here, so the
contract does not need to be redesigned when the data starts arriving.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from enum import StrEnum
from typing import Protocol, runtime_checkable

from novacontrol.novamarket.contracts import (
    DuplicatePriceError,
    HistoryLimitError,
)

#: One paisa. Every amount is quantized to this unit with one rounding rule.
_PAISE = Decimal("0.01")

#: The fixed conversion: 1 NovaCoin is 1 Indian rupee. It is a constant rather
#: than a rate because it is an accounting identity, not a market price.
NOVA_COIN_INR_RATE = Decimal("1")


class Currency(StrEnum):
    """The currencies a :class:`Money` amount may carry.

    NM-01 carried INR only. NM-02 reads prices from providers that quote in
    their own currency (a US equity is USD, an NSE equity is INR), so a quote
    must be able to say which one it is. Carrying a currency is NOT converting
    it: no exchange rate is applied here, mixed-currency arithmetic is still
    refused, and NovaCoin remains defined only for INR. A conversion belongs to
    the layer that has a timestamped, attributable exchange rate.
    """

    INR = "INR"
    USD = "USD"
    EUR = "EUR"
    GBP = "GBP"
    JPY = "JPY"
    HKD = "HKD"
    CAD = "CAD"
    AUD = "AUD"
    CHF = "CHF"
    SGD = "SGD"


#: Display symbols. A currency without an entry is shown by its code, never by
#: the rupee sign: mislabelling a USD amount as INR is the exact confusion the
#: currency field exists to prevent.
_CURRENCY_SYMBOLS: dict[Currency, str] = {
    Currency.INR: "₹",
    Currency.USD: "$",
    Currency.EUR: "€",
    Currency.GBP: "£",
    Currency.JPY: "¥",
}


def _coerce_decimal(value: object, *, field: str = "amount") -> Decimal:
    """A finite Decimal from a Decimal, int, or decimal string — never a float."""
    if isinstance(value, bool):
        raise TypeError(f"{field} must be a decimal value, not bool")
    if isinstance(value, float):
        raise TypeError(
            f"{field} must not be a binary float; use Decimal/int/decimal string "
            "so monetary arithmetic stays deterministic"
        )
    if isinstance(value, Decimal):
        amount = value
    elif isinstance(value, int):
        amount = Decimal(value)
    elif isinstance(value, str):
        try:
            amount = Decimal(value.strip())
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"{field} is not a decimal value: {value!r}") from exc
    else:
        raise TypeError(
            f"{field} must be Decimal, int or a decimal string, not "
            f"{type(value).__name__}"
        )
    if not amount.is_finite():
        raise ValueError(f"{field} must be finite, got {amount}")
    return amount


def _require_aware(value: object, *, field: str) -> datetime:
    """A timezone-aware datetime, normalized to UTC (naive times are refused)."""
    if not isinstance(value, datetime):
        raise TypeError(f"{field} must be a datetime, not {type(value).__name__}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(
            f"{field} must be timezone-aware; a naive time cannot be ordered "
            "against market observations"
        )
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class Money:
    """A deterministic monetary amount in INR, with NovaCoin as its unit.

    ``Money.nc("12.50")`` and ``Money.inr("12.50")`` are the same value because
    1 NC is 1 INR; the two constructors exist so call sites can say what they
    mean. Arithmetic keeps the currency, refuses mixed currencies and refuses
    floats, and every result is quantized to paise.
    """

    amount: Decimal
    currency: Currency = Currency.INR

    def __post_init__(self) -> None:
        amount = _coerce_decimal(self.amount).quantize(_PAISE, rounding=ROUND_HALF_EVEN)
        try:
            currency = Currency(self.currency)
        except ValueError as exc:
            raise ValueError(f"unsupported currency: {self.currency!r}") from exc
        object.__setattr__(self, "amount", amount)
        object.__setattr__(self, "currency", currency)

    # -- construction ---------------------------------------------------------

    @classmethod
    def inr(cls, amount: object) -> Money:
        """An amount of Indian rupees."""
        return cls(_coerce_decimal(amount))

    @classmethod
    def nc(cls, amount: object) -> Money:
        """An amount of NovaCoin, which is the same number of rupees."""
        return cls(_coerce_decimal(amount))

    # -- representation -------------------------------------------------------

    @property
    def nova_coin(self) -> Decimal:
        """The amount in NC (1 NC = ₹1, exactly). INR-only by definition."""
        if self.currency is not Currency.INR:
            raise ValueError(
                f"NovaCoin is defined only for INR; this amount is "
                f"{self.currency.value} and must be converted with a timestamped "
                "exchange rate first"
            )
        return self.amount / NOVA_COIN_INR_RATE

    def format(self) -> str:
        """The amount with its own currency's symbol (or its code)."""
        symbol = _CURRENCY_SYMBOLS.get(self.currency)
        if symbol:
            return f"{symbol}{self.amount:,.2f}"
        return f"{self.amount:,.2f} {self.currency.value}"

    def format_inr(self) -> str:
        """Format an INR amount; refuse to put a rupee sign on another currency."""
        if self.currency is not Currency.INR:
            raise ValueError(
                f"format_inr() received {self.currency.value}; use format() for a "
                "non-INR amount"
            )
        return self.format()

    def to_dict(self) -> dict[str, str]:
        payload = {
            "amount": format(self.amount, "f"),
            "currency": self.currency.value,
        }
        if self.currency is Currency.INR:
            # NC is an INR accounting identity; a USD amount has no NC value
            # until a timestamped exchange rate converts it.
            payload["nova_coin"] = format(self.nova_coin, "f")
        return payload

    def __str__(self) -> str:
        return self.format()

    # -- arithmetic -----------------------------------------------------------

    def _other(self, other: object) -> Money:
        if not isinstance(other, Money):
            raise TypeError(f"expected Money, got {type(other).__name__}")
        if other.currency is not self.currency:
            raise ValueError(
                f"cannot combine {self.currency.value} with {other.currency.value}"
            )
        return other

    def __add__(self, other: object) -> Money:
        return Money(self.amount + self._other(other).amount, self.currency)

    def __radd__(self, other: object) -> Money:
        return self.__add__(other)

    def __sub__(self, other: object) -> Money:
        return Money(self.amount - self._other(other).amount, self.currency)

    def __rsub__(self, other: object) -> Money:
        return Money(self._other(other).amount - self.amount, self.currency)

    def __mul__(self, factor: object) -> Money:
        multiplier = _coerce_decimal(factor, field="factor")
        return Money(self.amount * multiplier, self.currency)

    def __rmul__(self, factor: object) -> Money:
        return self.__mul__(factor)

    def __truediv__(self, divisor: object) -> Money:
        value = _coerce_decimal(divisor, field="divisor")
        if value == 0:
            raise ZeroDivisionError("cannot divide money by zero")
        return Money(self.amount / value, self.currency)

    def __neg__(self) -> Money:
        return Money(-self.amount, self.currency)

    def __abs__(self) -> Money:
        return Money(abs(self.amount), self.currency)

    # -- ordering -------------------------------------------------------------

    def _compare(self, other: object, operator: str) -> bool:
        other_money = self._other(other)
        left, right = self.amount, other_money.amount
        if operator == "lt":
            return left < right
        if operator == "le":
            return left <= right
        if operator == "gt":
            return left > right
        return left >= right

    def __lt__(self, other: object) -> bool:
        return self._compare(other, "lt")

    def __le__(self, other: object) -> bool:
        return self._compare(other, "le")

    def __gt__(self, other: object) -> bool:
        return self._compare(other, "gt")

    def __ge__(self, other: object) -> bool:
        return self._compare(other, "ge")


@dataclass(frozen=True, slots=True)
class MarketQuote:
    """One observed price, with where it came from and when.

    Both timestamps are required and must be timezone-aware: ``observed_at`` is
    the source's own time for the price, ``retrieved_at`` is when this process
    obtained it. Keeping both is what makes staleness and provenance answerable
    without guessing. The value is a :class:`Money`, so the quote cannot carry a
    float.
    """

    asset: str
    price: Money
    observed_at: datetime
    source: str
    retrieved_at: datetime

    def __post_init__(self) -> None:
        asset = str(self.asset).strip().upper()
        source = str(self.source).strip()
        if not asset:
            raise ValueError("a market quote needs an asset identifier")
        if not source:
            raise ValueError("a market quote needs a price source")
        if not isinstance(self.price, Money):
            raise TypeError("a market quote's price must be Money")
        object.__setattr__(self, "asset", asset)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "observed_at", _require_aware(self.observed_at, field="observed_at"))
        object.__setattr__(self, "retrieved_at", _require_aware(self.retrieved_at, field="retrieved_at"))

    @property
    def identity(self) -> tuple[str, str, datetime]:
        """What makes a quote the same observation (used to refuse overwrites)."""
        return (self.asset, self.source, self.observed_at)

    def age(self, *, now: datetime | None = None) -> timedelta:
        """How old the observation is; a future timestamp reports zero age."""
        moment = _require_aware(now, field="now") if now is not None else datetime.now(UTC)
        return max(moment - self.observed_at, timedelta(0))

    def is_stale(self, *, max_age: timedelta, now: datetime | None = None) -> bool:
        if max_age.total_seconds() <= 0:
            raise ValueError("max_age must be positive")
        return self.age(now=now) > max_age

    def to_dict(self) -> dict[str, object]:
        return {
            "asset": self.asset,
            "price": self.price.to_dict(),
            "observed_at": self.observed_at.isoformat(),
            "source": self.source,
            "retrieved_at": self.retrieved_at.isoformat(),
        }


@runtime_checkable
class MarketDataProvider(Protocol):
    """The only seam through which market prices may enter NovaMarket.

    NM-02 implements this; NM-01 declares it so the collection phase plugs into
    a contract rather than reaching into the subsystem. ``fetch_quotes`` returns
    timestamped quotes and must not synthesize a price it did not observe.
    """

    @property
    def name(self) -> str:
        """Stable provider name, used as the quote's provenance."""

    async def fetch_quotes(
        self,
        assets: Sequence[str],
        *,
        observed_after: datetime | None = None,
    ) -> tuple[MarketQuote, ...]:
        """Quotes for these assets, optionally only after a known observation."""


class PriceHistory:
    """An append-only reference price history.

    There is no update and no delete, so a newer price can never overwrite an
    older one: both are retained and ``latest`` reports the most recently
    *observed* quote. Appending the same (asset, source, observed-at) twice is
    refused, because a duplicate append is either a bug or an attempt to rewrite
    history in place. ``max_quotes_per_asset`` is a hard bound; reaching it is an
    error that asks for an archive rather than silently dropping the oldest
    observation.

    This is the NM-01 reference implementation of the contract, not the NM-02
    storage pipeline. A durable repository replaces the storage and keeps this
    interface and these invariants.
    """

    def __init__(self, *, max_quotes_per_asset: int = 500) -> None:
        if isinstance(max_quotes_per_asset, bool) or not isinstance(max_quotes_per_asset, int):
            raise TypeError("max_quotes_per_asset must be an int")
        if max_quotes_per_asset < 1:
            raise ValueError("max_quotes_per_asset must be at least 1")
        self.max_quotes_per_asset = max_quotes_per_asset
        self._by_asset: dict[str, list[MarketQuote]] = {}
        self._seen: set[tuple[str, str, datetime]] = set()

    def append(self, quote: MarketQuote) -> MarketQuote:
        """Retain one observation; never replace an earlier one."""
        if not isinstance(quote, MarketQuote):
            raise TypeError("PriceHistory.append expects a MarketQuote")
        if quote.identity in self._seen:
            raise DuplicatePriceError(
                f"{quote.asset} from {quote.source!r} at "
                f"{quote.observed_at.isoformat()} is already retained; "
                "historical prices are never overwritten"
            )
        bucket = self._by_asset.setdefault(quote.asset, [])
        if len(bucket) >= self.max_quotes_per_asset:
            raise HistoryLimitError(
                f"{quote.asset} already holds {len(bucket)} quotes "
                f"(limit {self.max_quotes_per_asset}); archive the history rather "
                "than discarding an observation"
            )
        bucket.append(quote)
        self._seen.add(quote.identity)
        return quote

    def quotes(self, asset: str) -> tuple[MarketQuote, ...]:
        """Every retained quote for an asset, in append order."""
        return tuple(self._by_asset.get(str(asset).strip().upper(), ()))

    def latest(self, asset: str) -> MarketQuote | None:
        """The most recently OBSERVED quote, not merely the last appended."""
        bucket = self._by_asset.get(str(asset).strip().upper())
        if not bucket:
            return None
        return max(bucket, key=lambda quote: (quote.observed_at, quote.retrieved_at))

    def assets(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_asset))

    def __len__(self) -> int:
        return sum(len(bucket) for bucket in self._by_asset.values())


__all__ = [
    "Currency",
    "MarketDataProvider",
    "MarketQuote",
    "Money",
    "NOVA_COIN_INR_RATE",
    "PriceHistory",
]
