# PI-19 — Free filing adapter

PI-19 originally shipped with no bundled statement source: statements had to be
supplied through an explicit caller context, so the Financial Research workspace
reported "no point-in-time financial statement was available" for every
instrument. `novacontrol/novamarket/filings/` now ships **one** free adapter —
`YahooFundamentalsAdapter` — that fills the research context without
caller-supplied fixtures.

## 1. Source and licence position

| Field | Value |
| --- | --- |
| `source_id` | `yahoo:fundamentals` |
| Name | Yahoo Finance public fundamentals |
| Tier | `unknown` (an aggregator of exchange filings, never a filing) |
| Cost | free, no key, no subscription |
| Coverage | every NSE/BSE row in the catalogue (`provider_symbols.yahoo`, `.NS` / `.BO`) |
| Endpoints | `fundamentals-timeseries` (statements) and `quoteSummary?modules=assetProfile` (company facts) |

Why this source: the official exchange APIs are closed to a bounded automated
reader from this build (NSE and BSE both answer HTTP 403), and every keyed
provider adds an account the page cannot populate without. Yahoo already supplies
NM-02's delayed prices, so this is the same vendor relationship with the same
"delayed, unofficial, no redistribution" limitation, not a new dependency. The
adapter therefore labels its data `unverified` and `unknown`-tier on every record
and never presents it as a filing.

A BSE row can have no fundamentals series while the NSE listing does (the
catalogue's BSE rows are NSE companies). The adapter retries the sibling listing
once and records the substitution in the run's notes.

## 2. What it produces

* **Statements** — income, balance sheet and cash flow, annual and quarterly,
  mapped onto the research aliases (`revenue`, `net_income`, `total_assets`,
  `operating_cash_flow`, …). A period with fewer than two usable line items is
  not emitted: the run reports "no statement was assembled" instead of a sparse
  row that would distort a ratio.
* **Company facts** — `description`, `sector` and `industry` become
  `company_profile` evidence records, which is what the profile builder consumes;
  the remaining profile fields stay listed as missing.

## 3. Publication timestamps

Yahoo publishes the reporting period **end** and no filing/announcement time.
The adapter therefore records the statement's **first observation** time as
`published_at`:

* it can never back-date a statement to its period end (which would invent
  availability);
* the cache stores the earliest observation and a refetch cannot move it
  forward, so repeated runs agree;
* every statement carries `verification="unverified"`, and each run's warnings
  state the basis explicitly: *"the source publishes no filing timestamp, so each
  publication time is its first observation and never the reporting period
  end"*.

A period end is a calendar date and is anchored at 00:00 UTC.

## 4. Rate limits, cache and missing-data honesty

* One bounded HTTP transport with a 15 s timeout and a 4 MB read cap.
* A minimum interval of 3 s between calls (the source throttles quickly).
* A process-local ceiling of 120 requests per UTC day, counted including the
  profile session crumb; exhaustion is reported, never papered over.
* An 8-hour on-disk cache under `data/cache/filings/` keyed by the resolved
  symbol, so a page refresh does not spend the budget again.
* HTTP refusals, throttling, an empty series, a refused crumb or an instrument
  with no symbol mapping each produce a **note** on the run; none produces a
  synthetic statement, a zero-filled row or a guessed industry.
* `NOVACONTROL_DISABLE_FILINGS=1` disables the adapter process-wide (the test
  suite sets it; adapter tests inject a fake fetcher instead).

## 5. Wiring

`NovaMarketService._research_context` calls the adapter once per run and merges
its output into the context the research service already understands
(`statements`, `evidence`, `statement_source_id`, `statement_source_name`,
`source_notes`). Research keeps its own point-in-time gating: a statement whose
publication time is after the cutoff, or whose period ends after it, is excluded
with a reason.

## 6. Verification

`tests/test_novamarket_filings.py` covers mapping, the first-observation proxy
and its stability, cache reuse, the daily ceiling, the minimum interval, a
refused profile session, an empty series, an incomplete period, symbol
resolution, the sibling-listing retry, HTTP throttling, and acceptance of the
payloads by `FinancialStatement.from_payload`, `analyze_financials` and
`build_company_profile`. All tests are offline.
