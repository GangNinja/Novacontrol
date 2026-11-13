# NovaMarket — Integrated Architecture & Acceptance

**Status:** The roadmap is implemented through **NM-14 (Integrated Workspace &
Final Acceptance)**. Final acceptance is **PASS (9 November 2026; performance recheck 10 November 2026; internal
acceptance scope)** — 25/25 routes, 470/470 current controls and 10/10 performance checks (480 baseline
reconciled), with external certifications not claimed. The current record is
[`NM14_FINAL_ACCEPTANCE_REPORT.md`](NM14_FINAL_ACCEPTANCE_REPORT.md); the earlier
[`NM14_ACCEPTANCE.md`](NM14_ACCEPTANCE.md) PARTIAL result is historical and
superseded. The historical NM-01 record below is retained as the architecture
contract that later phases built on.

NM-01 was implemented and verified (PASS, 2026-11-01; see §13).
NM-02 (Market Data Reliability) is implemented and verified separately; its
architecture, provider matrix and acceptance record live in
`docs/NOVAMARKET_NM02.md`. NM-03 (NovaCoin Wallet & Deterministic Ledger) is
implemented and verified; see `docs/NM03_ARCHITECTURE.md`,
`docs/NM03_LEDGER_DESIGN.md`, `docs/NM03_API_CONTRACTS.md` and
`docs/NM03_TEST_REPORT.md`. NM-04 (Evidence-Backed Market Event Radar) is
implemented and verified against fixtures; see `docs/NM04_ARCHITECTURE.md`,
`docs/NM04_EVENT_SCHEMA.md`, `docs/NM04_SOURCE_REGISTRY.md`,
`docs/NM04_VERIFICATION_RULES.md`, `docs/NM04_CLASSIFICATION_MODEL.md` and
`docs/NM04_TEST_REPORT.md`. NM-05 (Autonomous Asset Discovery & Watchlists) is
implemented and verified against fixtures; see `docs/NM05_ARCHITECTURE.md`,
`docs/NM05_ASSET_DISCOVERY.md`, `docs/NM05_CANDIDATE_SCORING.md`,
`docs/NM05_WATCHLIST_LIFECYCLE.md`, `docs/NM05_INTEGRATION_CONTRACTS.md` and
`docs/NM05_TEST_REPORT.md`. NM-06 through NM-13 are implemented: probabilistic
forecasting, three-horizon portfolios, strategies, paper execution, backtesting,
shadow mode and controlled adaptive learning. NM-14 adds the integrated
workspace, live overview, activity/audit bridge and final acceptance record.
NM-13's design, governance and evidence rules are in
`docs/NM13_ADAPTIVE_LEARNING.md`; its executed tests are in
`docs/NM13_TEST_REPORT.md`. The standalone workspace is at
`GET /market/workspace`; it is intentionally separate from the NovaControl root
UI and does not decide final product placement.
**Contract version:** `1.0.0` (`MARKET_CONTRACT_VERSION`)
**Subsystem:** `novamarket` (runtime module, event source, state-store namespace, capability namespace)

NovaMarket is the financial-intelligence subsystem of NovaControl. NM-01 is its
foundation phase: it establishes the subsystem boundary, the versioned contracts,
the validated lifecycle, the configuration and permission model, the event
vocabulary, data ownership and the integration through which NovaControl manages
it. NM-02 onward implement the market features behind these contracts.

NM-01 **does not** fetch a market price, create a portfolio, run a forecast, place
a paper order or connect a broker. Those are later phases; this document says
which one, so "not implemented" is never mistaken for "forgotten".

---

## 1. Roadmap (canonical, not renamed or reordered)

| Phase | Title | State in this build |
| --- | --- | --- |
| NM-01 | Architecture, Contracts & Integration | **Implemented** |
| NM-02 | Market Data Reliability | **Implemented** |
| NM-03 | NovaCoin Wallet & Deterministic Ledger | **Implemented** |
| NM-04 | Evidence-Backed Market Event Radar | **Implemented** |
| NM-05 | Autonomous Asset Discovery & Watchlists | **Implemented** |
| NM-06 | Measured Probabilistic Forecasting | Implemented |
| NM-07 | Autonomous Three-Horizon Portfolio Manager | Implemented |
| NM-08 | Independent Risk Engine | Implemented |
| NM-09 | Trading Strategies | Implemented |
| NM-10 | Realistic Paper Execution | Implemented (simulated only) |
| NM-11 | Reproducible Backtesting | Implemented |
| NM-12 | Shadow Mode | Implemented |
| NM-13 | Controlled Adaptive Learning | Implemented |
| NM-14 | Integrated Workspace & Final Acceptance | **Implemented — PASS (2026-11-09)** |

The table lives in `src/novacontrol/novamarket/architecture.py` as `ROADMAP`, is
served by `GET /market/contracts`, and is pinned by tests.

---

## 2. Where NovaMarket lives

```
src/novacontrol/novamarket/
    __init__.py       public exports
    contracts.py      versions, errors, lifecycle states and machine
    architecture.py   roadmap, 12 domain boundaries, storage/ownership declarations
    money.py          Money (Decimal INR/NC), MarketQuote, PriceHistory, MarketDataProvider
    horizons.py       day / swing / long-term profiles and their validation
    config.py         NovaMarketConfig, ResourceLimits, SecretReference
    permissions.py    operation matrix and the subsystem's own authorization gate
    events.py         typed event publisher + future-event extension contract
    service.py        NovaMarketService: lifecycle, status, health, capabilities
    module.py         NovaMarketModule: the RuntimeModule adapter
```

NovaMarket is a **subsystem of NovaControl**, not a second application. It does
not create an event bus, a runtime, a permission layer, a capability registry, a
database engine or an audit trail. It composes the existing ones.

**Contract ownership.** NovaMarket owns its contracts and its package. NovaControl
core owns the shared services those contracts ride on — the event bus, the
runtime, the permission layer, the capability registry and the state store. The
HTTP routes are owned by the API module that already owns every other route; no
NovaMarket route reaches around it.

### Dependency map (reused, not duplicated)

| NovaMarket needs | NovaControl service it uses |
| --- | --- |
| Runtime lifecycle | `novacontrol.core.runtime.EventDrivenRuntime` + `core.interfaces.RuntimeModule` |
| Events | `novacontrol.core.events.EventBus`, `EventType` typed payload validation |
| Persistence | `novacontrol.persistence.json_store.JsonStateStore` (namespace `novamarket`) |
| Permissions | `novacontrol.reliability.permissions.PermissionManager` |
| Capabilities | `novacontrol.intelligence.intent.CapabilityRegistry` |
| Health vocabulary | `novacontrol.core.diagnostics.HealthState` |
| HTTP | the existing FastAPI factory in `novacontrol/api/app.py` |
| Audit | the existing `novacontrol.audit` layer (financial audit records are NM-14) |
| Risk levels/scopes | `novacontrol.core.security.RiskLevel` / `PermissionScope` |

NovaMarket imports **no** desktop, browser, phone, NovaForge, model or trading
module. The architecture test scans the package's imports to keep it that way.

---

## 3. Domain boundaries

Twelve boundaries are declared in `DOMAIN_BOUNDARIES`; each names its
responsibility, the data it owns, its interface, the phase that implements it and
whether it executes actions. In NM-01 **every boundary declares
`executes_actions=False`** except the implemented NM-10 paper-execution boundary,
which can only write simulated order and fill records,
which this build does not implement.

| Domain | Owns | Interface | Phase | Executes |
| --- | --- | --- | --- | --- |
| `market_data` | quotes, historical prices | `MarketDataProvider` / `PriceHistory` | NM-02 | no |
| `asset_discovery` | asset metadata | `AssetDiscoveryContract` | NM-05 | no |
| `market_intelligence` | market events | `MarketEventRadarContract` | NM-04 | no |
| `forecasting` | forecasts | `ForecastContract` | NM-06 | no |
| `portfolio_management` | portfolios | `PortfolioContract` | NM-07 | no |
| `risk_management` | risk limits | `RiskEngineContract` | NM-08 | **no, by design** |
| `strategy_management` | strategies | `StrategyContract` | NM-09 | no |
| `paper_execution` | paper orders, execution records | `PaperExecutionContract` | NM-10 | yes (simulated only) |
| `backtesting` | backtest results | `BacktestContract` | NM-11 | no |
| `accounting_ledger` | wallets, transactions, ledger entries | `LedgerContract` | NM-03 | no |
| `adaptive_learning` | learning state | `AdaptiveLearningContract` | NM-13 | no |
| `observability_audit` | audit records | `MarketAuditContract` | NM-14 | no |

The independent risk engine owns limits and may veto; it never owns the action it
evaluates. A test enforces that statement.

### Data ownership and storage

`STORAGE_BOUNDARIES` declares, per data family, the owning domain, the storage
strategy and whether it is append-only:

- **Append-only:** market quotes, historical prices, market events, forecasts,
  ledger entries, paper orders, execution records, backtest results and audit
  records.
- **Mutable-with-history:** asset metadata, portfolios, strategies, risk limits,
  learning state.
- **NM-01's own:** the bounded lifecycle history (in memory) and the validated
  configuration snapshot (`novamarket` namespace in the existing JSON store).

**Relationships.** The declarations are deliberately about ownership, not
foreign keys: a quote belongs to one asset, a market event cites the quotes and
sources it was derived from, a forecast belongs to one asset and horizon, a
portfolio belongs to one horizon and its ledger entries use that horizon's
`ledger_namespace`, and an execution record cites the paper order it fills.
Those references are the future phase's schema work; NM-01 fixes WHO owns each
family and which families are immutable, so a later schema cannot quietly give
two domains the same data.

**Migration strategy.** There is one schema-versioned contract (the persisted
configuration, `MARKET_SCHEMA_VERSION = 1`) and one rule for history: append-only
families are never rewritten in place. A future family is added under its
declared namespace/phase, and a shape change to an existing family is a
versioned migration owned by the phase that introduces it — not a silent
reinterpretation. A stored configuration from a newer schema or contract is
refused (ERROR), not coerced; no automatic destructive migration exists.

No second database engine is introduced. NM-02 may move quotes behind a
repository, but the ownership and append-only statements stay in
`architecture.py` and the tests that read them.

---

## 4. Financial invariants (implemented, not aspirational)

### NovaCoin and INR

`novacontrol.novamarket.money` implements:

- `Currency.INR` as the only reporting currency;
- `Money`, a frozen value object carrying a `Decimal` amount and a currency;
- `Money.nc("12.50") == Money.inr("12.50")` — **1 NC = ₹1**, an accounting
  identity (`NOVA_COIN_INR_RATE = Decimal("1")`), not a market rate;
- two-decimal quantization with `ROUND_HALF_EVEN` on construction and on every
  arithmetic result;
- **binary floats refuse**: `Money(0.1)` raises `TypeError`, so
  `Money(0.1) + Money(0.2) == Money(0.3)` is exact decimal arithmetic;
- mixed currencies raise instead of being silently converted.

### Timestamped prices with provenance

`MarketQuote` cannot exist without:

- `asset` and `source`;
- `observed_at` (the source's time) and `retrieved_at` (this process's time),
  **both timezone-aware** — a naive datetime is refused;
- a `Money` price.

`age()` and `is_stale(max_age=…)` answer freshness from those timestamps.

`PriceHistory` is append-only: there is no update or delete, appending the same
`(asset, source, observed_at)` identity is a `DuplicatePriceError` rather than an
overwrite, and reaching the configured quote cap raises `HistoryLimitError`
instead of silently dropping an observation. It is the NM-01 reference
implementation; NM-02 replaces the storage behind the same contract.

### Three investment horizons

`horizons.py` (`HORIZON_PROFILES`) defines day trading, swing trading and
long-term investing with **distinct**:

- maximum holding windows (23 hours / 30 days / 3650 days, strictly increasing);
- overnight rules (day trading forbids overnight exposure);
- maximum position fractions (10% / 20% / 35%);
- stop-loss fractions (1% / 5% / 15%);
- rebalance cadences (intraday / daily / quarterly);
- **ledger namespaces** (`portfolio.day`, `portfolio.swing`,
  `portfolio.long_term`) so the three can never share one ledger by accident.

`validate_horizon_profiles()` checks the distinctions as data.

### No real-money trading

- The config's only operational mode is `observe`; `paper` is refused with
  "reserved for NM-10"; `live` is refused outright.
- `MarketOperation.EXECUTE_LIVE_TRADE` exists in the permission matrix so the
  risk of it can be stated, but it is **not granted**, never declared in the
  shared permission layer and never registered as a capability or route.
- `GET /market/contracts` reports `real_money_trading: false`.
- A mandatory initialization check refuses to start if the permission model ever
  grants live trading.

---

## 5. Lifecycle

`LifecycleMachine` is the only mutator of state. Illegal transitions raise
`LifecycleError`; asking for the current state returns an unchanged entry (so
idempotent callers need no guard); every accepted change is recorded with a
reason and timestamp in a bounded (100-entry) history.

```
DISABLED ──► INITIALIZING ──► READY ──► RUNNING ──► DEGRADED
   ▲              │             │         │           │
   │              └──► ERROR ◄──┘         └──► SHUTTING_DOWN
   └──────────────────────────────────────────┘
```

Documented transitions (`ALLOWED_TRANSITIONS`):

| From | To |
| --- | --- |
| `disabled` | `initializing` |
| `initializing` | `ready`, `error` |
| `ready` | `running`, `degraded`, `disabled`, `error`, `shutting_down` |
| `running` | `degraded`, `disabled`, `error`, `shutting_down` |
| `degraded` | `running`, `disabled`, `error`, `shutting_down` |
| `error` | `initializing`, `disabled`, `shutting_down` |
| `shutting_down` | `disabled`, `error` |

Behaviour:

1. `initialize()` validates mandatory checks (configuration, architecture,
   contract version, no live-trading grant, injected dependency validators),
   registers capabilities, declares permissions, then reaches **READY**.
2. Any mandatory failure withdraws partial contributions, records the reason and
   reaches **ERROR** — never READY.
3. `enable()` initializes if needed and attaches the event control channel,
   reaching **RUNNING**; without a bus it reaches **DEGRADED** and says so.
   NM-01 starts **no worker**: the lifecycle owns one control subscription, so a
   reinitialisation has no worker to duplicate and re-creates exactly that one
   subscription.
4. `disable()` withdraws capabilities, permission declarations and subscriptions,
   persists `enabled=false`, and reaches **DISABLED**.
5. `shutdown()` is the orderly stop: `SHUTTING_DOWN → DISABLED`, idempotent, and
   it does **not** change the operator's enabled choice.
6. `reinitialize()` disables first (removing everything), then initializes, then
   restores the prior enabledness — so no duplicate subscription, capability or
   permission declaration survives a retry.
7. `auto_start()` (the runtime path) never raises: a failure leaves **ERROR** and
   NovaControl boots normally.

---

## 6. Configuration contract

`NovaMarketConfig` (`novamarket/config.py`), persisted in the `novamarket`
namespace and served by `GET /market/config`:

| Field | Type | Default | Validation |
| --- | --- | --- | --- |
| `enabled` | boolean | `true` | must be a boolean |
| `mode` | string | `observe` | only `observe`; `paper` reserved for NM-10; `live` refused |
| `data_providers` | list[string] | `[]` | short references, unique, max 8, no URLs/credentials |
| `credentials_ref` | string | `""` | `env:NAME` or `store:NAME`; **never a value** |
| `stale_after_seconds` | integer | `900` | 1…604800 |
| `resources` | object | see below | strict keys and bounds |
| `log_level` | string | `INFO` | DEBUG/INFO/WARNING/ERROR/CRITICAL |
| `emit_events` | boolean | `true` | publication switch, contract still validated |
| `contract_version` | string | `1.0.0` | must equal the running build |
| `schema_version` | integer | `1` | must equal the running build |

Resource limits: `max_assets` 1…10 000, `max_quotes_per_asset` 1…100 000,
`max_concurrent_fetches` 1…16.

Rules:

- unknown fields are **refused** (a typo cannot be silently ignored);
- a field whose *name* looks like a credential (`api_key`, `secret`, `token`,
  `password`, …) is refused with instructions to use `credentials_ref`;
- a partial update changes only the fields it names; unknown nested resource
  fields are still refused;
- a stored snapshot from a newer schema/contract is refused and leaves the
  subsystem in ERROR rather than silently resetting to defaults;
- `to_dict()` contains the reference, never a resolved secret.

---

## 7. Permission model

`MarketPermissionModel` (`permissions.py`) is the subsystem's own gate:

| Operation | Risk | Granted in NM-01 | Phase |
| --- | --- | --- | --- |
| `market.status.read` | LOW | yes | NM-01 |
| `market.data.read` | LOW | yes (no collection implemented yet) | NM-01/NM-02 |
| `market.config.manage` | MEDIUM, confirm | yes | NM-01 |
| `market.wallet.read` | LOW | yes | NM-03 |
| `market.wallet.move` | MEDIUM | yes (simulated NC) | NM-03 |
| `market.ledger.correct` | HIGH, confirm | yes (simulated NC) | NM-03 |
| `market.config.sensitive.read` | HIGH, `secret:read` | **no** — nothing stored | NM-03 |
| `market.portfolio.simulate` | MEDIUM, confirm | **no** | NM-07 |
| `market.trade.paper` | HIGH, confirm | **yes** | NM-10 (simulated only) |
| `market.trade.live` | CRITICAL, destructive, irreversible | **no, never** | — |

Only granted operations are declared into the shared `PermissionManager`, and
exactly those names are withdrawn at disable/shutdown. Denied operations keep
their risk in the matrix so the contract shows *why* each is refused, but they
are not declared (absence is the stronger boundary) and no route, tool or
capability can reach them. The management routes are authenticated by the
application's existing `require_auth`; NM-01 has no consequential financial
action, so no operation needs a second approval prompt. `market.config.manage`
still declares MEDIUM/confirmation for the layer that will gate future writes.

---

## 8. Event contract

Implemented events are typed `EventType` members, validated by the bus:

| Event | Required payload |
| --- | --- |
| `novamarket.initializing` | `contract_version` |
| `novamarket.initialized` | `contract_version`, `capabilities` |
| `novamarket.ready` | `lifecycle` |
| `novamarket.enabled` | `lifecycle` |
| `novamarket.disabled` | `reason` |
| `novamarket.configuration_requested` | `configuration` |
| `novamarket.configuration_changed` | `changed` (field names, never values) |
| `novamarket.capabilities_registered` | `capabilities`, `count` |
| `novamarket.health_changed` | `health`, `previous` |
| `novamarket.warning` | `code`, `detail` |
| `novamarket.error` | `error_type`, `error` |
| `novamarket.shutdown` | `reason` |

`MarketEventPublisher` is the only door events leave through. It:

- refuses unregistered names (no uncontrolled publication);
- validates implemented payloads through the bus and future payloads through the
  declared spec;
- **refuses to publish a future event** — `novamarket.data.available` is declared
  for NM-02 and is not emitted by NM-01;
- never lets a publication failure break the lifecycle (it records
  `last_event_error` and increments `event_failures`).

The control channel is real: publishing `novamarket.configuration_requested` with
`{"configuration": {...}}` applies the same validated update as
`POST /market/config`, and the reply keeps the request's correlation id.

### Extension path

`FUTURE_EVENT_SPECS` declares the payload contract and owning phase for:
`novamarket.data.available`, `.data.unavailable`, `.data.freshness_changed`
(NM-02); `.event.detected` (NM-04); `.asset.discovered` (NM-05);
`.portfolio.updated` (NM-07); `.risk.breach` (NM-08);
`.paper.order_filled` is now the implemented `novamarket.order.*` and
`novamarket.execution.*` vocabulary; `.backtest.completed` (NM-11) remains reserved;
`.shadow.divergence` (NM-12); `.learning.updated` (NM-13).

NM-03 promoted nine of its own names into the implemented vocabulary — the six
`novamarket.wallet.*` events and the three `novamarket.ledger.*` events — before
publishing any of them.

---

## 9. NovaControl integration and API

NovaMarket is constructed by `NovaControlApplication`, registered in the one
runtime as `NovaMarketModule`, included in `status()["novamarket"]`, reported by
the diagnostics roster as **NovaMarket**, and flushed by `persist()`.

The management API (all authenticated; NM-14 adds the standalone workspace):

| Route | Purpose |
| --- | --- |
| `GET /market/status` | lifecycle, health, config, capabilities, dependencies, counters, transitions |
| `GET /market/contracts` | versioned contracts, roadmap, domains, storage, horizons, permissions, events |
| `GET /market/config` | resolved configuration, validity and schema |
| `POST /market/config` | validate/apply; invalid input → 422 with the reason |
| `POST /market/enable` | initialise and start; failure → 500 with the reason while status shows ERROR |
| `POST /market/disable` | withdraw contributions and stand down |

NM-14 adds `GET /market/overview` and the standalone `GET /market/workspace`
page. The overview is composed from the real subsystem state; the page contains
no sample prices, balances or performance numbers. Financial mutations remain
explicit API calls, and the interface does not decide whether NovaMarket should
ultimately live inside NovaControl or as a separate application.

NM-02 added the validated market-data routes behind the same lifecycle
(`GET /market/data/status`, `GET /market/data/providers`,
`GET|POST /market/instruments`, `POST /market/instruments/lookup`,
`POST /market/data/ingest`, `GET /market/data/quote/{instrument_id}`,
`GET /market/data/history/{instrument_id}`, `GET /market/data/bars/{instrument_id}`). They read the
stored, validated observations and never synthesize a price; see
`docs/NOVAMARKET_NM02.md`.

Isolation: `NovaMarketModule.start` cannot fail NovaControl's boot, a disable
touches only NovaMarket's own namespace/capabilities/permissions/subscription,
and `novamarket/` imports no other feature subsystem.

---

## 10. Testing strategy

`tests/test_novamarket.py` and `tests/test_novamarket_api.py` cover:

- **Architecture:** package import graph (including a cycle check), 12
  boundaries, roadmap, no duplicate lifecycle/event infrastructure,
  `RuntimeModule` conformance.
- **Contracts:** money/NC/INR arithmetic, float refusal, timestamped quotes,
  append-only history, horizon distinctions, config validation and secret
  refusals, permission matrix.
- **Lifecycle:** every allowed transition, illegal transitions, READY/ERROR,
  recovery, idempotent enable/initialize, reinitialization without duplicate
  subscriptions/capabilities, disable withdrawal, shutdown.
- **Integration:** application detection, runtime registration, start/stop,
  status/health, capability and permission registration, real event delivery,
  configuration persistence across restart.
- **Isolation:** NovaMarket in ERROR does not stop `app.start()`; disabling leaves
  the brain/runtime and NovaForge state intact; NovaMarket imports no feature
  subsystem.
- **HTTP:** every route, 422 validation, 500 on initialization failure,
  ApiSurface/consumer agreement. The routes declare their authentication through
  the same `Depends(require_auth)` as the rest of the API.

Run focused:

```bash
PYTHONPATH=src python -m pytest tests/test_novamarket.py tests/test_novamarket_api.py -q
```

---

## 11. Known limitations (NM-01, by design)

- **Superseded by NM-02:** market data is now collected and stored durably. In
  the NM-01 contract alone, `market.read` is reported unavailable even when a
  provider name is configured, because a configuration reference is not an
  implementation; NM-02 supplies that implementation (see
  `docs/NOVAMARKET_NM02.md`).
- **Superseded by NM-03:** wallets and an append-only double-entry NovaCoin
  ledger now exist (see `docs/NM03_ARCHITECTURE.md`). This section's "no wallet,
  no ledger" statement describes the NM-01 contract alone. Still absent: portfolio,
  forecast, event radar, strategy, risk engine, paper execution, backtesting,
  shadow mode and adaptive learning.
- No sensitive financial configuration exists; only a secret *reference* is
  stored.
- No real-money trading; no broker integration; no order route.
- `PriceHistory` is an in-memory reference implementation with a hard cap; NM-02's
  durable repository is the one the API reads from.
- In NM-01 alone, health is computed from subsystem records, not live provider
  probes. NM-02 adds real probes (`GET /market/data/providers?probe=true`).

## 12. Decisions deferred to later phases

- ~~The durable market-data schema/repository (NM-02)~~ — delivered; see
  `docs/NOVAMARKET_NM02.md`.
- ~~The ledger storage and posting rules (NM-03)~~ — delivered as an
  append-only double-entry SQLite ledger in `novamarket/wallet/`; see
  `docs/NM03_LEDGER_DESIGN.md`.
- The risk engine's limits (NM-08) — it may tighten the horizon profiles but must
  not execute what it evaluates.
- The four execution/learning safety decisions (NM-10…NM-13), each requiring its
  own approval and verification path.
- ~~The final NovaMarket interface/platform and the durable financial audit
  trail~~ — delivered by NM-14; the workspace is standalone and the audit trail
  reuses `novacontrol.audit`.

---

## 13. Verification record — NM-01 acceptance (2026-11-01)

Worktree `novamarket`, branch `novamarket/nm-01`, base commit `afa5ff6`.
The main working tree (with the independent NovaForge workstream) was not
modified.

### Checks run

| Check | Command | Result |
| --- | --- | --- |
| Focused NM-01 | `pytest tests/test_novamarket.py tests/test_novamarket_api.py -q` | **102 passed**, 66 subtests, 8.99s |
| Dependency / integration | `pytest tests/test_application.py tests/test_api.py tests/test_api_reference_sync.py tests/test_diagnostics.py tests/test_events.py tests/test_event_correlation.py tests/test_lifecycle_events.py tests/test_optimization.py tests/test_web_api.py tests/test_web_platform.py -q` | **293 passed**, 891 subtests, 103.21s |
| Types | `mypy src` | 464 source files, **no issues** |
| API reference | `python scripts/generate_api_reference.py --check` | `docs/API.md: in sync` |
| Live HTTP (temporary uvicorn on 127.0.0.1:8002) | `/market/status`, `/market/contracts`, `POST /market/config`, `/market/disable`, `/market/enable` | status `running`/`ok`, 14 phases, 12 domains, 12 events, `market.read` unavailable even with a provider named, `real_money_trading=false`, disable removed the one subscription, enable restored it, invalid credential reference 422 without echoing the value |
| Full-suite baseline (before this pass's repairs) | 150 test files in three groups | 6,063 passed, 1 skipped, 1 pre-existing failure; 3,884 subtests |

The single full-suite failure is
`tests/test_evaluation.py::ServiceTests::test_retention_removes_old_rows_from_every_store`.
It fixes "now" at 2026-09-30 while retention reads the real clock, so both rows
age out. It fails identically on the untouched base commit `afa5ff6`; it is not
caused by NovaMarket and no NovaMarket test depends on it.

### Defects found and repaired during this verification pass

1. `market.read` became `available` as soon as a provider NAME was configured,
   even though NM-01 has no provider. It is now always unavailable in NM-01,
   with the NM-02 reason; a test configures a provider and asserts the table
   still refuses to advertise it.
2. `status()` and `contracts()` hard-coded `real_money_trading: false`. Both now
   derive the flag from `MarketPermissionModel.authorize(EXECUTE_LIVE_TRADE)`, so
   a future model change is reported truthfully instead of hidden by a constant.
3. `persist()` documented "a failed write never raises" but did not catch. It now
   records `last_persist_error` and a `persist_failures` counter, degrades health,
   and lets the enable/disable/config operation succeed.
4. An unreadable state store could escape the service constructor. Construction
   now records the failure as `config_error` and defaults; initialisation reaches
   ERROR instead of stopping NovaControl's boot.
5. A failed `reinitialize()` used to leave the persisted `enabled` choice false
   (because it disables first). It now restores the operator's enabled choice
   before re-raising, so the next boot still retries and reports the failure.
6. A future event publisher accepted a payload field named `source`, which the
   bus envelope owns; that produced a `TypeError` and a silently dropped event.
   `source` is now refused with an explanatory error.
7. Configuration errors for provider references and secret-reference names
   echoed the rejected value. They no longer do, so a mis-pasted credential is
   not copied into an API error, event or log.
8. `contracts()` duplicated the lifecycle history limit; it now reads
   `LIFECYCLE_HISTORY_LIMIT`.
9. Horizon validation checked the profile count but not the composition; it now
   requires exactly one profile per horizon.
10. Added the missing architecture test for intra-package import cycles, an
    application-level test that the real NovaControl bus carries
    `novamarket.enabled` with source `novamarket`, and a generated check of every
    declared lifecycle transition (allowed targets succeed, all others raise).

### Remaining issues / unverified requirements

- **Pre-existing** `test_retention_removes_old_rows_from_every_store` failure
  (base commit, date-dependent, unrelated); not repaired here.
- NM-01 deliberately implements no market data, ledger, portfolio, forecast,
  event radar, strategy, risk engine, paper execution, backtest, shadow mode or
  adaptive learning; those are NM-02…NM-13.
- The durable financial audit trail is delivered by NM-14, using the shared
  `novacontrol.audit` logger; NM-01's historical statement is superseded.
- No secret resolution: only `env:` / `store:` references are stored. There is no
  NovaControl secret store to integrate with, and inventing one was out of scope.
- The standalone NM-14 workspace is read-only for financial state; mutations
  remain explicit API calls.
- `PriceHistory` is an in-memory reference implementation; NM-02 owns the durable
  repository.
- Health is computed from subsystem records, not live provider probes (there is
  no provider yet).
- The management API is authenticated; no second confirmation prompt is
  implemented for configuration changes even though `market.config.manage` is
  declared MEDIUM/confirmation in the permission matrix. NM-01 has no
  consequential financial action, so no operation currently reaches a gateway.
- The full suite was not re-run after the final repairs (per instruction); the
  focused suite and the dependency/integration set above were re-run green. The
  repairs touched only `novamarket/`, its tests, and documentation.

### NM-02 readiness

Delivered. NM-02 implements `MarketDataProvider`-compatible adapters, a durable
append-only quote/bar repository behind the existing contract, credential
resolution for `credentials_ref`, and provider-backed freshness/health. The
declared `novamarket.data.*` events were promoted into
`EventType`/`EVENT_PAYLOAD_FIELDS` before any of them was published, exactly as
this section required. The NM-02 acceptance record, including NM-03 readiness,
is in `docs/NOVAMARKET_NM02.md`.

### NM-03 readiness

Delivered. NM-03 implements wallets and an append-only, double-entry NovaCoin
ledger in `novamarket/wallet/` on the NM-02 database file, with atomic transfers,
persisted idempotency, reversal-only corrections and an independent reconciler
that reports but never repairs. The nine `novamarket.wallet.*` /
`novamarket.ledger.*` events were promoted into `EventType` /
`EVENT_PAYLOAD_FIELDS` before any was published, three permission operations were
granted (`market.wallet.read`, `market.wallet.move`, `market.ledger.correct`)
while `market.trade.live` stays refused, and 13 routes were added to
`ApiSurface` / `ROUTE_CONSUMERS` and to the regenerated `docs/API.md`. NM-04 can
read wallets and post balanced movements through `WalletEngine` without touching
storage. The acceptance record, including NM-04 readiness, is in
`docs/NM03_TEST_REPORT.md`.
