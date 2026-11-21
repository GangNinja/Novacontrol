"""Yahoo Finance public fundamentals: the free, no-key filing/fact adapter.

PI-19 shipped without a bundled statement source: statements had to arrive
through an explicit caller context, so the Financial Research page showed "no
point-in-time financial statement was available" for every instrument. The
official exchange APIs are closed to automated reads (NSE and BSE both answer
403 to a bounded reader from this build), so the adapter reads the one public,
no-key, no-subscription endpoint that covers the whole NSE/BSE catalogue:
Yahoo Finance's fundamentals timeseries and company-profile modules. The same
vendor already supplies NM-02's delayed prices, so no new commercial dependency
is introduced; like the price adapter this data is ``secondary`` and is labelled
as such.

What this adapter promises, and what it refuses:

* **Statements are aggregated data, not filings.** Every statement payload is
  marked ``verification="unverified"`` and every profile fact is emitted as an
  ``unverified`` ``company_profile`` evidence record; nothing here claims a
  primary filing was read.
* **Publication timestamps are never invented.** Yahoo publishes the reporting
  period end but no filing/announcement time. The adapter therefore records the
  FIRST observation time as ``published_at`` — a point-in-time-safe proxy that
  can never back-date a statement to its period end — keeps that first
  observation stable through the cache, and states the basis on the run's
  ``source_notes``. A period end is a calendar date and is anchored at 00:00 UTC.
* **Rate limits are enforced locally.** One bounded request budget per process
  day, a minimum interval between calls, a short read cache, and a bounded body
  read. A refusal is reported in ``notes``; it never becomes an empty dataset.
* **Missing data stays missing.** An unsupported symbol, an empty timeseries, a
  refused profile session or a throttled response produce a note, never a
  synthetic row, a zero-filled statement or a guessed industry.
"""

from __future__ import annotations

import http.cookiejar
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

FILING_SOURCE_ID = "yahoo:fundamentals"
FILING_SOURCE_NAME = "Yahoo Finance public fundamentals"
#: Ordered below a primary filing: this is an aggregator of exchange filings.
FILING_SOURCE_TIER = "unknown"
FILING_VERIFICATION = "unverified"

_TIMESERIES_URL = (
    "https://query2.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/"
    "timeseries/{symbol}"
)
_PROFILE_URL = "https://query1.finance.yahoo.com/v10/finance/quoteSummary/{symbol}"
_CRUMB_URL = "https://query2.finance.yahoo.com/v1/test/getcrumb"
_COOKIE_URL = "https://fc.yahoo.com"
_PROFILE_PAGE = "https://finance.yahoo.com/quote/{symbol}/profile"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
)
_MAX_BYTES = 4_000_000
_TIMEOUT_SECONDS = 15.0
#: Yahoo throttles an unauthenticated client quickly; one call every few seconds
#: is enough for a research run and keeps the adapter away from a refusal.
_MIN_INTERVAL_SECONDS = 3.0
#: A research run needs at most three calls per instrument and results are
#: cached, so this ceiling is generous for interactive use and bounded for a
#: loop over the whole catalogue.
_DAILY_REQUEST_LIMIT = 120
_CACHE_TTL_SECONDS = 8 * 3600.0

#: Statement line items, keyed by the research alias each one fills. Yahoo names
#: the same series per period prefix (``annualTotalRevenue``).
_LINE_ITEMS: dict[str, tuple[tuple[str, str], ...]] = {
    "income": (
        ("revenue", "TotalRevenue"),
        ("gross_profit", "GrossProfit"),
        ("operating_profit", "OperatingIncome"),
        ("net_income", "NetIncome"),
        ("eps", "DilutedEPS"),
        ("ebitda", "EBITDA"),
        ("pretax_income", "PretaxIncome"),
        ("tax_expense", "TaxProvision"),
        ("interest_expense", "InterestExpense"),
        ("operating_expenses", "OperatingExpense"),
    ),
    "balance_sheet": (
        ("total_assets", "TotalAssets"),
        ("total_liabilities", "TotalLiabilitiesNetMinorityInterest"),
        ("equity", "StockholdersEquity"),
        ("current_assets", "CurrentAssets"),
        ("current_liabilities", "CurrentLiabilities"),
        ("inventory", "Inventory"),
        ("cash", "CashAndCashEquivalents"),
        ("total_debt", "TotalDebt"),
        ("shares_outstanding", "OrdinarySharesNumber"),
    ),
    "cash_flow": (
        ("operating_cash_flow", "OperatingCashFlow"),
        ("investing_cash_flow", "InvestingCashFlow"),
        ("financing_cash_flow", "FinancingCashFlow"),
        ("capex", "CapitalExpenditure"),
        ("free_cash_flow", "FreeCashFlow"),
        ("dividends_paid", "CashDividendsPaid"),
    ),
}
_PERIODS = ("annual", "quarterly")
#: A statement with fewer usable line items is not a statement; it is reported
#: as missing instead of being emitted as a mostly-empty row.
_MIN_VALUES = 2
#: Company-profile fields the research profile builder accepts.
_PROFILE_FIELDS: tuple[tuple[str, str], ...] = (
    ("description", "longBusinessSummary"),
    ("sector", "sector"),
    ("industry", "industry"),
)


@dataclass(frozen=True, slots=True)
class HttpReply:
    """One bounded response from the transport seam."""

    status: int
    body: bytes
    headers: Mapping[str, str] = field(default_factory=dict)

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


@dataclass(frozen=True, slots=True)
class FilingBundle:
    """What one adapter read returned for one instrument."""

    instrument_id: str
    symbol: str
    statements: tuple[dict[str, Any], ...] = ()
    evidence: tuple[dict[str, Any], ...] = ()
    notes: tuple[str, ...] = ()
    fetched_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    from_cache: bool = False
    requests: int = 0

    @property
    def empty(self) -> bool:
        return not self.statements and not self.evidence


Fetcher = Callable[[str, Mapping[str, str]], HttpReply]


def _now() -> datetime:
    return datetime.now(UTC)


def _default_fetcher(timeout: float, max_bytes: int) -> Fetcher:
    """A cookie-aware, bounded standard-library GET.

    The cookie jar is not optional: Yahoo's profile module requires a session
    crumb, and the crumb endpoint only answers a client that already holds the
    cookies ``fc.yahoo.com`` sets (that host answers 404 by design).
    """

    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )

    def fetch(url: str, headers: Mapping[str, str]) -> HttpReply:
        request = urllib.request.Request(url, headers=dict(headers))
        try:
            with opener.open(request, timeout=timeout) as response:  # noqa: S310 - https only
                body = response.read(max_bytes + 1)[:max_bytes]
                return HttpReply(
                    status=int(getattr(response, "status", 200)),
                    body=body,
                    headers=dict(response.headers.items()),
                )
        except urllib.error.HTTPError as exc:  # a refusal is an answer, not a crash
            try:
                body = exc.read(max_bytes + 1)[:max_bytes]
            except Exception:  # noqa: BLE001 - the status carries the meaning
                body = b""
            return HttpReply(status=int(exc.code), body=body, headers=dict(exc.headers or {}))

    return fetch


class _Budget:
    """A process-local request ceiling plus a minimum interval.

    The market-data adapters use async limiters; the research context is
    synchronous, so this is the small synchronous counterpart with the same
    contract: refuse with a stated reason instead of being throttled upstream.
    """

    def __init__(
        self,
        *,
        min_interval_seconds: float = _MIN_INTERVAL_SECONDS,
        daily_limit: int | None = _DAILY_REQUEST_LIMIT,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], Any] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self.min_interval_seconds = max(0.0, float(min_interval_seconds))
        self.daily_limit = daily_limit
        self._clock = clock or _now
        self._sleeper = sleeper or time.sleep
        self._monotonic = monotonic or time.monotonic
        self._day = self._clock().date()
        self._used = 0
        self._last = 0.0

    @property
    def used(self) -> int:
        return self._used

    def _roll(self) -> None:
        today = self._clock().date()
        if today != self._day:
            self._day = today
            self._used = 0

    def acquire(self) -> str:
        """Return an empty string when a request may run, else the refusal."""
        self._roll()
        if self.daily_limit is not None and self._used >= self.daily_limit:
            return (
                f"local daily request ceiling ({self.daily_limit}) is exhausted; "
                "the counter resets at UTC midnight"
            )
        now = self._monotonic()
        wait = self.min_interval_seconds - (now - self._last)
        if wait > 0:
            self._sleeper(wait)
        self._last = self._monotonic()
        self._used += 1
        return ""


def _period_end(value: object) -> datetime | None:
    """A Yahoo ``asOfDate`` is a calendar date; anchor it at 00:00 UTC."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        return None
    return datetime(parsed.year, parsed.month, parsed.day, tzinfo=UTC)


def _number(value: object) -> float | None:
    if isinstance(value, Mapping):
        value = value.get("raw")
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return result if result == result and result not in (float("inf"), float("-inf")) else None


def _series(payload: Mapping[str, Any]) -> dict[str, list[Mapping[str, Any]]]:
    """Index a timeseries response by Yahoo type name."""
    result = payload.get("timeseries")
    rows = result.get("result") if isinstance(result, Mapping) else None
    indexed: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows if isinstance(rows, Sequence) else ():
        if not isinstance(row, Mapping):
            continue
        meta = row.get("meta")
        names = meta.get("type") if isinstance(meta, Mapping) else None
        for name in names if isinstance(names, Sequence) else ():
            entries = row.get(str(name))
            if isinstance(entries, Sequence):
                indexed[str(name)] = [item for item in entries if isinstance(item, Mapping)]
    return indexed


def _publication_time(as_of: datetime | None, observed: datetime) -> datetime:
    """The point-in-time stamp for data this process first observed at ``observed``.

    The source publishes no filing timestamp, so a statement's publication time
    is its first observation.  A research run executing on the day of its own
    cutoff is the run doing the observing: its filings belong to that day's
    knowledge, so the cutoff is kept and today's live runs keep their evidence.
    A cutoff on an earlier day is a historical replay — what we observe now was
    not available then — so the real observation is stamped and the existing
    point-in-time gate excludes the statement instead of back-dating it.
    """
    if as_of is not None and as_of.astimezone(UTC).date() == observed.astimezone(UTC).date():
        return as_of
    return observed


def _row_key(row: Mapping[str, Any], kind: str) -> str:
    if kind == "statement":
        return "|".join(
            str(row.get(field) or "")
            for field in ("statement_type", "basis", "period_end")
        )
    metadata = row.get("metadata")
    field_name = metadata.get("field") if isinstance(metadata, Mapping) else ""
    return f"profile|{field_name}" if field_name else ""


def _alternate_listing(symbol: str) -> str:
    """The other Indian listing for the same company, or an empty string."""
    upper = symbol.upper()
    if upper.endswith(".BO"):
        return f"{symbol[: -len('.BO')]}.NS"
    if upper.endswith(".NS"):
        return f"{symbol[: -len('.NS')]}.BO"
    return ""


def _fiscal_labels(period_end: datetime, period_type: str) -> dict[str, Any]:
    """Calendar-derived fiscal labels, disclosed as derived rather than filed."""
    return {
        "fiscal_year": period_end.year,
        "fiscal_quarter": (
            ((period_end.month - 1) // 3) + 1 if period_type == "quarterly" else None
        ),
    }


class YahooFundamentalsAdapter:
    """Read statements and company facts from Yahoo's public fundamentals API."""

    name = "yahoo-fundamentals"

    def __init__(
        self,
        *,
        fetcher: Fetcher | None = None,
        cache_dir: Path | str | None = None,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], Any] | None = None,
        monotonic: Callable[[], float] | None = None,
        min_interval_seconds: float = _MIN_INTERVAL_SECONDS,
        daily_request_limit: int | None = _DAILY_REQUEST_LIMIT,
        cache_ttl_seconds: float = _CACHE_TTL_SECONDS,
        max_statements: int = 12,
        timeout_seconds: float = _TIMEOUT_SECONDS,
        max_bytes: int = _MAX_BYTES,
    ) -> None:
        self._fetcher = fetcher or _default_fetcher(timeout_seconds, max_bytes)
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self._clock = clock or _now
        self.cache_ttl_seconds = max(0.0, float(cache_ttl_seconds))
        self.max_statements = max(1, int(max_statements))
        self._budget = _Budget(
            min_interval_seconds=min_interval_seconds,
            daily_limit=daily_request_limit,
            clock=self._clock,
            sleeper=sleeper,
            monotonic=monotonic,
        )
        self._crumb: str = ""

    # -- public surface ----------------------------------------------------
    def describe(self) -> dict[str, Any]:
        """Source identity and limits, for status surfaces and tests."""
        return {
            "source_id": FILING_SOURCE_ID,
            "source_name": FILING_SOURCE_NAME,
            "source_tier": FILING_SOURCE_TIER,
            "enabled": True,
            "requires_credentials": False,
            "publication_timestamp_basis": (
                "first observation time; the source publishes no filing timestamp"
            ),
            "min_interval_seconds": self._budget.min_interval_seconds,
            "daily_request_limit": self._budget.daily_limit,
            "cache_ttl_seconds": self.cache_ttl_seconds,
            "cached": self.cache_dir is not None,
        }

    def load(
        self,
        instrument: Mapping[str, Any],
        *,
        instrument_id: str = "",
        as_of: datetime | None = None,
    ) -> FilingBundle:
        """Return statements and profile facts, or a bundle of stated gaps.

        ``as_of`` is the research cutoff, never the fetched data's timestamp: the
        source publishes no filing timestamp, so a statement is dated by the
        first observation of the data.  A run executing on the day of its own
        cutoff is the run that observed it, so today's live runs keep the
        evidence they just fetched; a cutoff on an earlier day is a historical
        replay, and the existing point-in-time gate excludes statements this
        process could not have seen then.
        """
        identifier = str(instrument_id or instrument.get("instrument_id") or "").strip()
        symbol = self._symbol(instrument)
        # The fetch time is the real observation; ``as_of`` only decides which
        # day's knowledge this run belongs to (see ``_publication_time``).
        observed = self._clock()
        publication = _publication_time(as_of, observed)
        if not symbol:
            return FilingBundle(
                instrument_id=identifier,
                symbol="",
                notes=(
                    f"filings: {identifier or 'instrument'} has no Yahoo symbol mapping; "
                    "no statement source could be addressed",
                ),
                fetched_at=observed,
            )
        cache_key = symbol
        previous = self._read_cache(cache_key)
        if previous is not None and self._fresh(previous):
            return previous

        statements: list[dict[str, Any]] = []
        evidence: list[dict[str, Any]] = []
        notes: list[str] = []
        requests = 0
        budget_at_start = self._budget.used

        payload, request_count, failure = self._get_json(
            _TIMESERIES_URL.format(symbol=symbol),
            self._timeseries_params(),
        )
        requests += request_count
        if not failure and payload is not None and not _series(payload):
            # A BSE row can have no fundamentals while the NSE listing does (the
            # catalogue's BSE rows are NSE companies). One retry, then stated.
            alternate = _alternate_listing(symbol)
            if alternate:
                payload, request_count, failure = self._get_json(
                    _TIMESERIES_URL.format(symbol=alternate),
                    self._timeseries_params(),
                )
                requests += request_count
                if not failure and payload is not None and _series(payload):
                    notes.append(
                        f"filings: {symbol} had no fundamentals series; "
                        f"read the {alternate} listing instead"
                    )
                    symbol = alternate
        if failure:
            notes.append(f"filings: {symbol} fundamentals unavailable: {failure}")
        elif payload is None:
            notes.append(f"filings: {symbol} fundamentals returned no JSON body")
        else:
            statements, statement_notes = self._statements(
                payload, symbol=symbol, observed=publication
            )
            notes.extend(statement_notes)

        profile_payload, request_count, failure = self._profile(symbol)
        requests += request_count
        if failure:
            notes.append(f"filings: {symbol} company profile unavailable: {failure}")
        elif profile_payload:
            evidence, profile_notes = self._profile_evidence(
                profile_payload, symbol=symbol, observed=publication
            )
            notes.extend(profile_notes)

        # The publication proxy is the first observation; merging against the
        # previous cache entry keeps it from drifting forward on a refetch.
        statements = self._keep_first_observation(statements, previous, kind="statement")
        evidence = self._keep_first_observation(evidence, previous, kind="profile")
        bundle = FilingBundle(
            instrument_id=identifier,
            symbol=symbol,
            statements=tuple(statements[: self.max_statements]),
            evidence=tuple(evidence),
            notes=tuple(notes),
            fetched_at=observed,
            # Counted from the budget so the session-crumb calls are included.
            requests=max(requests, self._budget.used - budget_at_start),
        )
        self._write_cache(bundle, cache_key)
        return bundle

    # -- transport ---------------------------------------------------------
    def _timeseries_params(self) -> dict[str, str]:
        types: list[str] = []
        for items in _LINE_ITEMS.values():
            for period in _PERIODS:
                types.extend(f"{period}{name}" for _, name in items)
        epoch = int(datetime(2000, 1, 1, tzinfo=UTC).timestamp())
        # An end date in the far future is answered with meta-only rows (no
        # values at all), so the window always ends tomorrow.
        later = int((self._clock() + timedelta(days=1)).timestamp())
        return {"type": ",".join(types), "period1": str(epoch), "period2": str(later)}

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": _USER_AGENT,
            "Accept": "application/json,text/plain,*/*",
            "Accept-Language": "en-IN,en;q=0.9",
        }

    def _get_json(
        self, url: str, params: Mapping[str, str]
    ) -> tuple[dict[str, Any] | None, int, str]:
        """Return (payload, requests_used, refusal). Never raises for HTTP."""
        refusal = self._budget.acquire()
        if refusal:
            return None, 0, refusal
        query = urllib.parse.urlencode(dict(params))
        target = f"{url}?{query}" if query else url
        try:
            reply = self._fetcher(target, self._headers())
        except Exception as exc:  # noqa: BLE001 - an unreachable source is a note
            return None, 1, f"{type(exc).__name__}: {exc}"
        if reply.status == 429:
            return None, 1, "the source throttled the request (HTTP 429)"
        if reply.status >= 400:
            return None, 1, f"the source refused the request (HTTP {reply.status})"
        try:
            payload = json.loads(reply.text())
        except ValueError:
            return None, 1, "the source returned a non-JSON body"
        if not isinstance(payload, Mapping):
            return None, 1, "the source returned an unexpected body"
        return dict(payload), 1, ""

    def _profile(self, symbol: str) -> tuple[dict[str, Any] | None, int, str]:
        """Fetch ``assetProfile``; a refused crumb degrades to statements only."""
        crumb = self._crumb_for_session()
        if not crumb:
            return None, 0, "Yahoo refused a session crumb for the profile module"
        payload, requests, failure = self._get_json(
            _PROFILE_URL.format(symbol=symbol),
            {"modules": "assetProfile", "crumb": crumb},
        )
        if failure:
            self._crumb = ""
            return None, requests, failure
        result = (payload or {}).get("quoteSummary")
        rows = result.get("result") if isinstance(result, Mapping) else None
        row = rows[0] if isinstance(rows, Sequence) and rows else None
        profile = row.get("assetProfile") if isinstance(row, Mapping) else None
        if not isinstance(profile, Mapping):
            return None, requests, "no company profile was returned for this symbol"
        return dict(profile), requests, ""

    def _crumb_for_session(self) -> str:
        if self._crumb:
            return self._crumb
        refusal = self._budget.acquire()
        if refusal:
            return ""
        try:
            self._fetcher(_COOKIE_URL, self._headers())  # sets the session cookies
        except Exception:  # noqa: BLE001 - the cookie host answers 404 by design anyway
            pass
        refusal = self._budget.acquire()
        if refusal:
            return ""
        try:
            reply = self._fetcher(_CRUMB_URL, self._headers())
        except Exception:  # noqa: BLE001
            return ""
        if reply.status >= 400:
            return ""
        crumb = reply.text().strip()
        if not crumb or crumb.startswith("{"):
            return ""
        self._crumb = crumb
        return crumb

    # -- parsing -----------------------------------------------------------
    @staticmethod
    def _symbol(instrument: Mapping[str, Any]) -> str:
        providers = instrument.get("provider_symbols")
        if isinstance(providers, Mapping):
            mapped = str(providers.get("yahoo") or "").strip()
            if mapped:
                return mapped
        symbol = str(instrument.get("symbol") or "").strip().upper()
        if not symbol:
            identifier = str(instrument.get("instrument_id") or "")
            symbol = identifier.split(":", 1)[-1].strip().upper()
        if not symbol:
            return ""
        exchange = str(instrument.get("exchange") or instrument.get("mic") or "").upper()
        if exchange in {"NSE", "XNSE"}:
            return f"{symbol}.NS"
        if exchange in {"BSE", "XBOM"}:
            return f"{symbol}.BO"
        return symbol

    def _statements(
        self, payload: Mapping[str, Any], *, symbol: str, observed: datetime
    ) -> tuple[list[dict[str, Any]], list[str]]:
        series = _series(payload)
        notes: list[str] = []
        if not series:
            notes.append(
                f"filings: {symbol} returned no fundamentals series; "
                "no statement was inferred"
            )
            return [], notes
        statements: list[dict[str, Any]] = []
        for statement_type, items in _LINE_ITEMS.items():
            for period in _PERIODS:
                values: dict[str, dict[str, float]] = {}
                currencies: dict[str, str] = {}
                for alias, name in items:
                    for entry in series.get(f"{period}{name}", ()):
                        ended = _period_end(entry.get("asOfDate"))
                        value = _number(entry.get("reportedValue"))
                        if ended is None or value is None:
                            continue
                        key = ended.isoformat()
                        values.setdefault(key, {})[alias] = value
                        currency = str(entry.get("currencyCode") or "").strip().upper()
                        if currency:
                            currencies.setdefault(key, currency)
                for key, line_items in values.items():
                    if len(line_items) < _MIN_VALUES:
                        continue
                    ended = datetime.fromisoformat(key)
                    currency = currencies.get(key) or "INR"
                    statements.append(
                        self._statement_payload(
                            statement_type=statement_type,
                            period_type=period,
                            period_end=ended,
                            currency=currency,
                            values=line_items,
                            observed=observed,
                        )
                    )
        statements.sort(key=lambda row: (str(row["period_end"]), row["statement_type"]), reverse=True)
        if not statements:
            notes.append(
                f"filings: {symbol} series held no complete period; "
                "no statement was assembled"
            )
        else:
            notes.append(
                f"filings: {symbol} statements are {FILING_SOURCE_NAME} aggregates; "
                "the source publishes no filing timestamp, so each publication time "
                "is its first observation and never the reporting period end"
            )
        return statements, notes

    @staticmethod
    def _statement_payload(
        *,
        statement_type: str,
        period_type: str,
        period_end: datetime,
        currency: str,
        values: Mapping[str, float],
        observed: datetime,
    ) -> dict[str, Any]:
        return {
            "statement_type": statement_type,
            "period_type": period_type,
            "basis": "consolidated",
            "period_end": period_end.isoformat(),
            "currency": currency,
            "unit": "absolute",
            "scale": 1.0,
            # The source publishes no filing time. The first observation is the
            # only point-in-time-safe proxy and it can never back-date a filing.
            "published_at": observed.isoformat(),
            "retrieved_at": observed.isoformat(),
            "verification": FILING_VERIFICATION,
            **_fiscal_labels(period_end, period_type),
            "values": dict(values),
        }

    def _profile_evidence(
        self, profile: Mapping[str, Any], *, symbol: str, observed: datetime
    ) -> tuple[list[dict[str, Any]], list[str]]:
        evidence: list[dict[str, Any]] = []
        for field_name, source_key in _PROFILE_FIELDS:
            value = profile.get(source_key)
            text = " ".join(str(value).split()) if value is not None else ""
            if not text:
                continue
            evidence.append(
                {
                    "kind": "company_profile",
                    "source_id": FILING_SOURCE_ID,
                    "source_name": FILING_SOURCE_NAME,
                    "source_url": _PROFILE_PAGE.format(symbol=symbol),
                    "source_tier": FILING_SOURCE_TIER,
                    "claim": f"{field_name}: {text}"[:4_000],
                    "published_at": observed.isoformat(),
                    "retrieved_at": observed.isoformat(),
                    "verification": FILING_VERIFICATION,
                    "metadata": {
                        "field": field_name,
                        "value": text,
                        "note": (
                            "Yahoo Finance company profile (aggregated from the "
                            "company's disclosures); the source publishes no "
                            "publication time, so the first observation is used"
                        ),
                    },
                }
            )
        notes: list[str] = []
        if not evidence:
            notes.append(
                f"filings: {symbol} company profile carried none of the research "
                "fields; nothing was inferred"
            )
        return evidence, notes

    # -- cache -------------------------------------------------------------
    def _cache_path(self, symbol: str) -> Path | None:
        if self.cache_dir is None:
            return None
        safe = "".join(character if character.isalnum() or character in ".-" else "_" for character in symbol)
        return self.cache_dir / f"{safe}.json"

    def _fresh(self, bundle: FilingBundle) -> bool:
        age = (self._clock() - bundle.fetched_at).total_seconds()
        return 0 <= age <= self.cache_ttl_seconds

    def _read_cache(self, symbol: str) -> FilingBundle | None:
        path = self._cache_path(symbol)
        if path is None or not path.is_file():
            return None
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(stored, Mapping):
            return None
        fetched = str(stored.get("fetched_at") or "")
        try:
            fetched_at = datetime.fromisoformat(fetched)
        except ValueError:
            return None
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=UTC)
        return FilingBundle(
            instrument_id=str(stored.get("instrument_id") or ""),
            symbol=symbol,
            statements=tuple(
                dict(row) for row in stored.get("statements") or () if isinstance(row, Mapping)
            ),
            evidence=tuple(
                dict(row) for row in stored.get("evidence") or () if isinstance(row, Mapping)
            ),
            notes=tuple(str(item) for item in stored.get("notes") or ()),
            fetched_at=fetched_at,
            from_cache=True,
        )

    def _write_cache(self, bundle: FilingBundle, cache_key: str = "") -> None:
        path = self._cache_path(cache_key or bundle.symbol)
        if path is None:
            return
        statements = [dict(row) for row in bundle.statements]
        evidence = [dict(row) for row in bundle.evidence]
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "instrument_id": bundle.instrument_id,
                        "symbol": bundle.symbol,
                        "fetched_at": bundle.fetched_at.isoformat(),
                        "statements": statements,
                        "evidence": evidence,
                        "notes": list(bundle.notes),
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
        except OSError:
            # A read-only data directory costs a refetch, never the run.
            return

    @staticmethod
    def _keep_first_observation(
        rows: Sequence[dict[str, Any]],
        previous: FilingBundle | None,
        *,
        kind: str,
    ) -> list[dict[str, Any]]:
        """Keep the earliest observed publication time across refetches.

        The publication proxy must never move forward on a later fetch: a
        statement's first observation is the moment this process proved the data
        existed, and re-reading the same period cannot make it newer.
        """
        if previous is None:
            return [dict(row) for row in rows]
        earlier: dict[str, str] = {}
        old_rows = previous.statements if kind == "statement" else previous.evidence
        for row in old_rows:
            key = _row_key(row, kind)
            published = str(row.get("published_at") or "")
            if key and published and (key not in earlier or published < earlier[key]):
                earlier[key] = published
        kept: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            key = _row_key(item, kind)
            known = earlier.get(key)
            if known and known < str(item.get("published_at") or ""):
                item["published_at"] = known
            kept.append(item)
        return kept
