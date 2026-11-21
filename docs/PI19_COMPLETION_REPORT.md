# PI-19 Completion Report — Advanced Market Intelligence and Financial Research

## 1. Initial repository assessment

Work was performed on `feature/novamarket` in the existing dirty working tree.
No file was staged, committed, tagged, merged, pushed or published. The PI-01
through PI-18 completion reports were present and recorded scoped PASS verdicts;
no prerequisite dependency failure was found. No PI-20 work was started.

The existing research, market-data, forecasting, risk, execution, portfolio and
frontend conventions were inspected before implementation. The system is a
Python/FastAPI backend with isolated SQLite research stores, a React/Vite web
workspace, generated API documentation and shared authentication/route
registries. The existing NM-02 stored-bar path, PI-04 features/regimes, forecast
readers, NM-04 event radar, NM-07 portfolio detail and PI-17/PI-18 handoff
patterns were reused read-only.

## 2. Existing research capabilities reused

- NovaControl Explore remains the general research engine. PI-19 does not create
  a second general web-research or retrieval engine.
- NM-02 `/workbench` stored bars and instrument identity are the price and
  company-identity source; no provider or price store was added.
- PI-04 feature/regime readers supply candidate prediction context.
- PI-05 through PI-11 forecast readers supply timestamped forecast context; no
  model is retrained or promoted.
- NM-04 event-radar news is treated as untrusted, source-attributed evidence.
- NM-07 portfolio detail supplies read-only exposure context; no accounting
  table is written.
- NM-08 remains the sole risk authority and NM-10 the sole paper-execution
  authority.
- The shared `ApiSurface`, `ROUTE_CONSUMERS`, generated API reference and React
  navigation/design components are used rather than a parallel API or UI stack.

## 3. Research architecture

New isolated package: `src/novacontrol/novamarket/research/`.

| Module | Responsibility |
| --- | --- |
| `models.py` | Versioned requests, evidence, profiles, statements, ratios, events, news, sectors, peers, macro, valuation, thesis, provenance and authority contracts |
| `financials.py` | Unit/currency normalization, point-in-time statement selection, restatements, TTM, ratios, comparisons and peer comparison |
| `evidence.py` | Point-in-time filtering, source-tier verification, conflicting evidence, profile assembly, deterministic sentiment and claims |
| `intelligence.py` | Corporate events, news, macro, sector, peers and forecast context adapters |
| `valuation.py` | Relative valuation and DCF with assumptions and 3x3 sensitivity |
| `thesis.py` | Evidence-linked bullish/bearish factors, stance, uncertainty dimensions and change reasons |
| `store.py` | Isolated `pr_*` SQLite persistence, run status, evidence, claims, sections, theses and audit events |
| `service.py` | Bounded orchestration, task lifecycle, concurrency/timeout/cancellation, refresh, monitoring and handoffs |

Each run preserves research ID, instrument, exchange, company identity,
objective, horizon, requested analysis types, source references, data/config
versions, start/completion timestamps, publication cutoff, status and model
version. The service is synchronous and intended to run from a worker thread;
it has no order, risk-approval, ledger or portfolio-mutation dependency.

## 4. Company intelligence

The profile supports name, symbol, exchange, sector, industry, description,
products, segments, geographies, business model, competitive positioning,
customers/suppliers, management and risks. A field is populated only from
timestamped evidence or the NM-02 registry identity; absent fields appear in
`missing_fields`. Registry-only fields are marked partially verified with an
explicit note. The company section also carries an
`identity_point_in_time_note` stating that name/symbol/exchange/sector/industry
are current NM-02 registry metadata, while sourced profile fields are cutoff
filtered.

Evidence: `test_company_identity_and_profile_are_sourced_with_missing_fields_explicit`
and `test_company_identity_rejects_unqualified_instruments`.

## 5. Financial-statement analysis evidence

Income, balance-sheet and cash-flow statements are accepted with period type,
basis, reporting period, publication/retrieval timestamps, currency, unit,
scale and optional restatement metadata. Original values remain available
alongside normalized values. The analysis exposes selected statements,
normalized statements, TTM, annual/quarterly comparisons, restatements, missing
inputs and warnings.

TTM is produced only from four quarterly observations with 60–130 day spacing.
Consolidated and standalone statements are never combined in a ratio; the
analysis chooses one basis and warns that both were supplied. A later
publication supersedes an earlier record only for the same statement type,
basis and period.

Evidence: `test_financial_statement_ingestion_and_ratios_are_mathematically_correct`,
`test_quarterly_ttm_requires_four_contiguous_quarters`,
`test_consolidated_and_standalone_are_not_silently_mixed` and
`test_restatement_selects_latest_publication_and_preserves_previous`.

## 6. Fundamental metric validation

The deterministic fixture independently checks:

| Metric | Expected | Result |
| --- | ---: | ---: |
| Gross margin | 0.40 | 0.40 |
| Operating margin | 0.25 | 0.25 |
| Net margin | 0.18 | 0.18 |
| ROE | 0.24 | 0.24 |
| ROA | 12/140 ≈ 0.085714 | 0.085714 |
| ROIC | 187,500/950,000 ≈ 0.197368 | 0.197368 |
| Current ratio | 2.40 | 2.40 |
| Quick ratio | 2.00 | 2.00 |
| Debt/equity | 0.375 | 0.375 |
| Interest coverage | 12.50 | 12.50 |
| P/E | 20.00 | 20.00 |
| P/B | 4.50 | 4.50 |
| EV/EBITDA | 12.50 | 12.50 |
| FCF yield | 0.05 | 0.05 |
| Revenue growth | 0.111111 | 0.111111 |
| EPS/earnings/cash-flow growth | 0.20 | 0.20 |

Zero and negative denominators are explicit `None`/`unavailable` values with a
warning, not misleading ratios. Forward P/E is not produced without clearly
identified forecast inputs. Methodology and input provenance are retained in
`formulas` and `inputs`.

Evidence: the ratio test above plus
`test_zero_and_negative_denominators_are_unavailable_not_misleading`.

## 7. Corporate-event intelligence

Corporate events preserve event ID, company/instrument, type, announcement
timestamp, effective date, source, description, verification, financial details
and interpretation. Announcement does not imply occurrence: an event becomes
`effective` only when its effective timestamp is at or before the research
point. Future announcements are excluded. Rumours remain unverified and are not
converted into confirmed events or thesis factors. Verified earnings events can
become a versioned PI-04 candidate feature.

Evidence: `test_corporate_event_announcement_effective_and_rumour_status`.

## 8. News intelligence evidence

News retains source identity, URL, headline, publication/retrieval timestamps,
related instrument, category, relevance, summary, sentiment and verification.
The deterministic sentiment lexicon produces positive/negative/neutral/mixed/
uncertain labels and states explicitly that sentiment is not a stock-price
direction. A single independent article is partially verified; primary official
disclosure may be verified; AI summaries and instruction-bearing text remain
unverified. Stale articles are excluded by the evidence-age limit, and an
article or event without a publication/announcement timestamp is excluded with
an explicit reason rather than being assigned the research cutoff.

Evidence: `test_news_categories_sentiment_and_verification`,
`test_stale_news_is_excluded_by_the_evidence_limit` and
`test_prompt_injection_is_retained_as_untrusted_data_not_executed`.

## 9. Sector and peer-comparison results

Sector snapshots preserve source, period, performance, relative strength,
volatility and verification. Macro observations preserve indicator, value,
unit, period, publication time, revision status and relevance note; relevance is
never assumed for every instrument.

Peer selection is documented and excludes an issuer for industry mismatch,
currency mismatch, missing publication time, missing reporting period or
market-cap grouping outside the configured band. In the fixture, `XNSE:PEER` is
selected (same industry, currency and cap band) and `XNSE:OTHER` is excluded for
industry mismatch. The peer median P/E is 22.0, and peer min/max ranges are
carried separately into relative valuation. Direct news, events, sector and
macro inputs used for analysis are also emitted as source-bearing evidence
claims; no unverified inference is promoted to a fact.

Evidence: `test_sector_context_preserves_source_and_period`,
`test_peer_selection_uses_documented_criteria_and_keeps_exclusions` and
`test_macro_context_preserves_unit_period_and_relevance`.

## 10. Valuation analysis evidence

Relative valuation uses explicit peer multiple and per-share base metric:
`18 × 20 = 360` in the fixture, labelled `per_share`, with peer/assumption
provenance and limitations. When peer ranges are supplied, the estimate is also
bounded by their inclusive low/high, with the range method recorded. DCF requires explicit revenue growth, margin, tax,
capex, working-capital, discount-rate, terminal-growth and share assumptions;
the test verifies nine discount-rate/terminal-growth sensitivity cells. Missing
inputs produce no estimate rather than a fabricated value. Every result carries
structured assumptions, warnings and limitations, and
`is_objectively_verified_price: false`.

Evidence: `test_relative_valuation_keeps_assumptions_range_and_limitations`,
`test_dcf_valuation_has_explicit_assumptions_and_three_by_three_sensitivity` and
`test_valuation_is_not_invented_without_assumptions_or_statements`.

## 11. Evidence-verification results

The verifier retains every eligible record with source ID/name, publication and
retrieval timestamps, source tier, verification, content hash, instrument,
period and data version. Verified/partially verified/unverified/conflicting/
stale/unavailable statuses are supported. Primary official sources can verify;
independent sources need two independent corroborators; AI/derived interpretation
cannot verify itself.

Conflicting sourced values for the same profile field are preserved with both
evidence IDs, the field becomes `conflicting_evidence`, no value is chosen, and
the run becomes `partial_evidence`. AI-generated summaries remain unverified and are not independent sources, preventing circular agent amplification. Direct caller news, events, sector and macro inputs are converted into evidence records, so sections cannot cite a dangling evidence ID. Peer inputs preserve publication/reporting-period timestamps and are excluded
when either is after the cutoff or the reporting period is missing; price inputs
are excluded when older than the configured evidence age.

Evidence: `test_evidence_verification_prefers_primary_and_never_promotes_ai`,
`test_conflicting_evidence_is_preserved_and_lowers_completion` and
`test_missing_news_and_statements_are_reported_not_fabricated`.

## 12. Investment-thesis examples

The fixture produces a structured thesis with company/business context,
positive and negative factors, forecast context, read-only portfolio context,
supporting/contradictory evidence, assumptions, unresolved questions and a
stance. In the primary fixture the stance is positive-leaning; the exact stance
is not asserted as BUY/SELL because it is evidence-dependent. The conclusion
states that it is not an execution authorization. Confidence is reported as
four separate dimensions plus `combined_probability: null`,
`is_calibrated: false` and an explicit no-collapse note.

With no eligible statements, news, events or profile evidence, the stance is
`insufficient_evidence`. A thesis factor is omitted if it cannot cite evidence;
the event remains visible in the event section instead. Event and sector factors
also require verified or partially verified evidence; an unverified rumour or
classification-only sector snapshot cannot move the stance. The thesis horizon
adds its own focus and caveat, and completeness counts missing profile fields as
well as missing structured sections.

Evidence: `test_thesis_is_evidence_linked_and_separates_confidence_dimensions`
and `test_insufficient_evidence_thesis_is_explicit`.

## 13. Historical leakage-prevention evidence

All evidence carries a publication timestamp and is excluded when published
after the cutoff or unavailable at the as-of time. Statement periods ending
after the cutoff are excluded. Price snapshots after the cutoff or older than
the configured evidence-age limit are excluded from ratios and valuation.
News and corporate-event announcements after the cutoff are excluded. TTM uses
only the selected point-in-time quarters. Currency conversion requires an FX
timestamp at or after period end and not after as-of.

Evidence: `test_publication_cutoff_excludes_future_statement_and_news`,
`test_historical_leakage_is_prevented_by_as_of_and_publication_times` and
`test_quarterly_ttm_requires_four_contiguous_quarters`.

## 14. Forecasting integration

`features` returns `pi19-features-v1` values with `available_at`, source,
evidence IDs, `research_only: true`,
`approved_for_production_forecasting: false` and
`requires_pi04_evaluation: true`. Production use and automatic retraining are
false. No unverified narrative is injected as a numerical feature. The
forecast context reports expected-return mean, uncertainty and model
disagreement separately.

Evidence: `test_pi04_features_are_versioned_point_in_time_and_not_production_approved`
and the thesis confidence test.

## 15. Multi-agent integration

The handoff contract is `pi19-research-handoff-v1` and provides packages for
market research, fundamental analysis, technical analysis, prediction
intelligence, market regime, portfolio intelligence and trading decision. Each
package includes evidence references, structured facts/metrics and
`cannot_assert` boundaries. The circular-evidence rule says an agent
interpretation is not independent verification. Trading-decision output is a
decision input only and requires NM-08 and NM-10; it cannot submit orders.

Evidence: `test_pi17_handoff_is_structured_and_preserves_evidence_references`.

## 16. Portfolio-optimization integration

The `pi18_inputs` handoff includes financial health, sector, valuation,
event-risk events, research uncertainty and business concentration, plus
`advisory_only: true` and `cannot_override_constraints: true`. Portfolio context
is read-only, cannot change allocation and requires the PI-18 optimizer and
NM-08 approval. A thesis change never triggers an automatic reallocation.

Evidence: `test_pi18_handoff_is_read_only_and_cannot_override_constraints`.

## 17. API and frontend integration

24 authenticated routes implement status, contracts, health, create, run,
cancel, refresh, list/history, latest, detail, company, financials, events, news,
sector, peers, valuation, thesis, evidence, claims, features, handoff and
monitor. They are registered in `api/app.py`, `ApiSurface`,
`ROUTE_CONSUMERS` and regenerated `docs/API.md`; the route-consumer contract
marks them `no-render` because the React workspace is not a legacy static panel.
Unknown runs return 404, malformed instruments return 422, and a duplicate
research ID returns 409 instead of a misleading service-unavailable error.

The Financial Research page (`web/src/pages/FinancialResearch.tsx`) is wired into
the Trading Intelligence navigation and the App route switch. It covers company
overview, statement/ratio trends, corporate events, news, sector/macro, peers,
valuation assumptions/sensitivity/limitations, thesis/uncertainty, evidence
explorer, claims and integration handoff, with persistent advisory/NM-08/NM-10
banners.

Verification: `test_api_surface_and_route_consumers_register_every_pi19_route`,
`test_frontend_page_is_wired_into_navigation_and_route_switch`, the actual
application HTTP tests and `npm run build`.

## 18. Security and isolation evidence

- All external text is untrusted. Instruction-like content is detected, retained
  as data, marked unverified and excluded from thesis factors. It cannot change
  prompts, permissions, tools or execution.
- Source URLs must be absolute HTTP(S) without embedded credentials; source
  allowlists are enforced.
- The research store contains only `pr_runs`, `pr_evidence`, `pr_claims`,
  `pr_sections`, `pr_theses` and `pr_events`; no portfolio/position/ledger/order/
  risk tables.
- The research package contains no risk, execution, portfolio or ledger imports
  and exposes no submit/approve/portfolio/ledger/autonomy mutation methods.
- Research cannot submit orders, mutate portfolios or ledgers, bypass NM-08,
  change autonomy, activate policies, connect to brokers or enable live trading.
- Resource controls: bounded evidence/statements/news/peers/macro, bounded
  request context, concurrency semaphore, timeout, cancellation and idempotency.
- Monitor is a targeted, rate-limited refresh check; there is no background
  polling loop.

Evidence: `test_prompt_injection_is_retained_as_untrusted_data_not_executed`,
`test_allowed_sources_are_enforced`, `test_persistence_uses_only_isolated_research_tables`,
`test_research_package_has_no_financial_mutation_imports`,
`test_research_service_exposes_no_execution_or_portfolio_mutation_methods` and
`test_idempotency_and_bounded_context_resource_limits`.

## 19. Actual test counts and results

Executed on the working tree after the final fixes:

| Check | Result |
| --- | --- |
| Focused PI-19 suite (`tests/test_novamarket_pi19.py`) | **50 passed** |
| PI-12, PI-17, PI-18, features and forecasting regressions | **211 passed, 35 subtests passed** |
| Full NovaMarket API contract/regression file | **62 passed, 325 subtests passed** |
| Ruff on the research package and PI-19 tests | **All checks passed** |
| mypy on the research package and modified NovaMarket service | **Success: no issues in 10 source files** |
| `generate_api_reference.py --check` | **docs/API.md: in sync** |
| React/Vite production build | **built successfully** (1608 modules) |

The PI-19 suite includes deterministic financial fixtures, missing/stale/
conflicting/restated data, point-in-time cutoffs, resource limits, source
allowlists, persistence isolation, API registration, frontend wiring and two
real-application HTTP tests (status/contract/health, create/run/detail/thesis/
evidence, the real empty-directory context loader, 404/409/422 paths, expiry,
stale/excluded evidence and malformed optional context).

## 20. Defects fixed

1. A research-service indentation error was corrected before execution.
2. Cash-flow growth was reading income-statement rows; it now reads annual
   cash-flow statements specifically.
3. Profile-field disagreements were not grouped as competing evidence; they now
   become `conflicting_evidence` and lower the run to `partial_evidence`.
4. Thesis event/sector factors could cite an empty evidence list; factors
   without evidence are now omitted, and derived valuation factors cite the
   statement evidence that supplied their inputs.
5. `ResearchError` was missing from the public research export used by the HTTP
   error mapping; the export is restored.
6. The existing market-route contract test was extended with the 24 PI-19
   routes, preserving the no-live-execution assertion rather than weakening it.
7. Undated news, corporate events, macro releases and forecasts were silently
   assigned the research cutoff. They are now excluded with explicit reasons;
   only source-supplied timestamps can establish availability.
8. Refresh rebuilt the request but dropped the original caller `input_context`
   and then replaced it with an empty loader result. The original context is now
   preserved, including `source_notes`, while fresh loader data still overlays it.
9. Peer valuation ranges were computed and then discarded. They now reach
   relative valuation and bound the estimate inclusively.
10. Direct caller news, events and sector snapshots produced sections whose
    evidence IDs did not exist. They are now converted into source-bearing
    evidence records, so thesis/news/event/sector references resolve.
11. Synthetic statement evidence used a different ID from `statement_id`,
    producing a dangling thesis citation. The evidence ID is now the statement
    ID.
12. Price mappings ignored their own currency and as-of timestamp (using the
    request cutoff instead). Both are now preserved in the financial section.
13. Uncertainty and completeness undercounted unverified evidence and omitted
    profile gaps, and the thesis lacked horizon-specific focus. All three are
    corrected.
14. The working-capital line was missing from DCF inputs; it is now accepted and
    reflected in the working-capital formula while remaining optional.
15. A price older than the configured evidence-age limit could still feed P/E and
    valuation. It is now excluded with a stale-price warning.
16. Monitor ignored a latest `stale_data` run, so it could report no research and
    leave stale output unrefreshed. Stale runs are now eligible for targeted
    refresh.
17. A malformed optional price or forecast timestamp aborted the whole run. Both
    are now rejected as excluded evidence/warnings, leaving the rest of the run
    usable.
18. A peer without a reporting period silently defaulted to the cutoff. Such
    peers are now excluded with a reason.
19. Macro observations were shown but not represented in the evidence explorer;
    they now produce source-bearing macro claims.
20. Event and sector factors could be driven by unverified records. They now
    require verified or partially verified evidence, so rumours and
    classification-only sectors cannot move the stance.
21. Creating a duplicate research ID returned HTTP 503. It now returns 409.
22. Runs had no expiry marker and company identity had no point-in-time caveat.
    Runs now carry a horizon-derived `valid_until` and `expired` on detail; the
    company section states which identity fields are current registry metadata.

## 21. Remaining limitations

- One free statement source is bundled: `YahooFundamentalsAdapter`
  (`novamarket/filings/`, documented in `docs/FILINGS.md`) reads Yahoo's public
  fundamentals timeseries and company profile with no key, no subscription and
  no caller-supplied fixtures. It is an aggregate of exchange filings
  (`verification=unverified`, `unknown` tier), publishes the reporting period
  end but no filing time (so each statement's publication time is its first
  observation, never the period end and never back-dated), enforces a 3 s
  minimum interval, a 120-request daily ceiling and an 8-hour cache, and reports
  every refusal, empty series and missing field instead of inventing one. The
  official NSE/BSE filing APIs are closed to this build (HTTP 403), so a primary
  filing feed and paid filings remain unbundled; missing statements and profile
  fields are reported, never fabricated.
- News is limited to caller/adaptor evidence and the existing untrusted NM-04
  radar; there is no paid news subscription or general web scraper.
- Sector performance is measured from the analysed stored-bar universe and is
  marked partially verified.
- DCF projections use caller assumptions; the model does not forecast revenue or
  margins itself, and its estimate is not an intrinsic-value verification.
- Research expiry is represented by a horizon-derived `valid_until` and a
  computed `expired` flag on detail; there is no separate archive/restore
  endpoint. Retention is bounded by the store's `pr_*` row limits.
- Macro observations use the caller's point-in-time revised value and revision
  status; a full revision history (original plus every revision) is not stored
  by PI-19.
- News and event impact is represented by deterministic sentiment, event status,
  source verification and explicit uncertainty, not by an empirically estimated
  event-return model. Corporate-action adjustment contracts are not linked into
  the research output.
- Evidence is re-read through the caller/loader on refresh; PI-19 does not keep a
  cross-run external-document cache beyond the persisted research run/context.
- Calendar timestamps such as statement period end are stored at 00:00 UTC.
- The React page was compiled but not clicked through or visually inspected in
  this run; the HTTP endpoints and build were verified, not the rendered layout.
- No exhaustive full-suite run was repeated: only the PI-19 suite and the
  affected PI-12/PI-17/PI-18/feature/forecast/API areas were run, per the phase
  instruction to avoid broad unrelated audits.

## 22. Final acceptance verdict

**PASS (scoped, with disclosed partial areas).**

The PI-19 deliverables are implemented and verified: PI-01–PI-18 remain PASS;
existing research infrastructure was reused; the research coordinator, company
intelligence, statement/ratio analysis, corporate events, news, sector/peer
comparison, valuation, macro context, evidence verification, thesis,
multi-horizon support, uncertainty, refresh/versioning, point-in-time controls,
expiry marking, forecasting/multi-agent/portfolio handoffs, the 24-route API,
the frontend workspace, rate/resource limits, prompt-injection defenses and
financial-state isolation are present. NM-08 and NM-10 remain authoritative, and
the actual NovaMarket application serves and executes a research run through
HTTP, including an empty-directory context-loader path that reports low evidence
instead of fabricating a filing.

The three explicitly partial areas are: (1) research expiry/retention is enforced
through `valid_until`/`expired` and bounded `pr_*` rows, but there is no separate
archive endpoint; (2) event-impact and corporate-action adjustment linkage are
qualitative/contextual rather than an empirical impact model; and (3) no free
structured filing or news provider is bundled, so those inputs require an
explicit caller/adaptor context. These are disclosed at the API, UI and report
level rather than hidden, and none grants execution or portfolio authority.

Focused verification: **50 PI-19 tests passed**; **211 passed with 35 subtests**
in the PI-12/PI-17/PI-18/feature/forecast regressions; **62 passed with 325
subtests** in the full NovaMarket API file; Ruff and mypy clean; API reference in
sync; React/Vite production build successful. No PI-20 work was started. Nothing
was staged or committed.
