# NM-13 — Controlled Adaptive Learning

**Status:** implemented and tested. The subsystem learns from NM-10, NM-11 and
NM-12 evidence, but it never executes a trade, moves a wallet, changes a ledger
rule or edits an NM-08 risk limit. `IMPLEMENTED_PHASE` is `NM-13`.

The code lives in `src/novacontrol/novamarket/learning/` and persists to its own
SQLite database, `novamarket_learning.sqlite3`. It reuses the existing NM-11
`BacktestEngine` and `regime_analysis`, the NM-09 strategy catalogue and
`validate_config`, the NM-08 `RiskPolicy`, and the NM-12 `ShadowService`. No
second strategy, risk, execution or shadow engine was introduced.

## 1. Boundary

`LearningService` exposes only research, governance and version records:

* no method creates an NM-10 order, fill or reconciliation;
* no method mutates a wallet, ledger, portfolio or risk policy;
* promotion and rollback call one injected `strategy_applier`; without an
  applier the operation is refused instead of writing a cosmetic record;
* `status()`, `contract()` and `health()` report
  `execution_connected=false`, `executes_trades=false`, `real_money=false`.

The four operations are `market.learning.read` (LOW),
`market.learning.run` (MEDIUM, filesystem write),
`market.learning.manage` (MEDIUM, confirmation required) and
`market.learning.approve` (HIGH, confirmation required). They add four
capabilities, bringing the market capability registry to 47.

## 2. Evidence sources and provenance

Evidence is one row per observation set, with an explicit origin:

| Kind | Origin | Reliability label |
| --- | --- | --- |
| `backtest` | NM-11 reproducible historical replay | historical simulation with explicit costs; not live performance |
| `paper` | NM-10 simulated order and fill records | simulated paper execution; no real money moved |
| `shadow` | NM-12 hypothetical decisions and outcomes | hypothetical shadow observation; no order was created or filled |

The row stores `source_ref`, strategy id and version, catalogue id, the exact
configuration, its digest, window start/end, observation count, metrics,
regimes, in-sample/out-of-sample flags, received time, confidence, eligibility
reasons, provenance and a content digest. `is_live_performance` is always
false and the dashboard / contract repeat that fact.

`record_evidence` also accepts a source reference without metrics and imports
the corresponding existing service record: an NM-11 backtest result through
`backtest_loader`, NM-10 paper orders and fills through `paper_loader` or an
NM-12 session through `shadow_loader`. The imported payload keeps its source
kind; paper and shadow records are never rewritten as backtests.

The NM-10 importer reads each fill's `position_effect.realized_delta`, falling
back to an explicit `realized_pnl` only when present. It sums `total_fees` and
`slippage_amount` and records realized P&L, fees, slippage, trade count and win
rate. A paper window with fills but no realized outcome is rejected instead of
being recorded as a zero-return strategy. The NM-12 importer preserves the
session's `by_instrument` and `by_horizon` analytics; the monitor aggregates
their `mean_return_pct` values (weighted by resolved outcomes) into snapshot
metrics rather than discarding them.

### Eligibility and leakage prevention

Before an observation can influence monitoring or an experiment it is checked
for:

* a strategy id and the exact strategy version;
* at least `min_sample_size` observations (default 3);
* a return, total return, mean return or Sharpe outcome;
* a timezone-aware timestamp, with a future `window_end` rejected because it
  would leak future information;
* freshness within 90 days by default (`evidence_max_age_days`);
* duplicates, by the content digest; a repeat returns the original row and
  `duplicate=true` instead of appending a second observation;
* in-sample-only evidence, which is downgraded to `low_confidence` rather than
  accepted as sole promotion evidence;
* small samples, downgraded to `low_confidence` with explicit reasons.

Rejected rows are still persisted with their reasons, so a refusal to learn is
auditable. Experiment windows additionally enforce
`train_start < train_end <= validation_start < validation_end` and
`validation_end <= now`. Candidate evaluation only reads bars inside those
windows; the validation window is never used to choose the candidate.

## 3. Performance monitoring

`monitor(strategy_id, window_days=...)` builds one snapshot per origin instead
of averaging the three. Each snapshot reports observation count, evidence
count, reliability, return, Sharpe, Sortino, drawdown, win rate, profit factor,
average gain/loss, volatility, exposure, trades and signals when the sample is
sufficient. If it is not, metrics are withheld and the snapshot says why.

Deterioration compares the current window with the preceding window and
reports `degraded` when return falls by more than 1.0 percentage point,
drawdown worsens by more than 3 points, win rate falls by more than 10 points,
or volatility rises by more than 5 points. Measured anomalies also produce the
specific `volatility_spike`, `signal_shift`, `risk_increase` and `instability`
alert kinds, each with both windows' values. Signal shift requires at least five
signals in each window and a 25% change; exposure increase uses 10 points;
instability is drawdown worsening plus a 10-point win-rate drop. A change in
regime coverage is reported separately. A spread above 5 percentage points
between origins raises `source_divergence`; the message states that the results
disagree and were not averaged. Every alert has a kind, severity, strategy,
metrics, evidence refs and an acknowledgement path.

When a strategy has an active promoted version and the monitor labels any origin
`degraded`, it creates one open `rollback_recommended` alert carrying the active
version, its recorded candidate/baseline validation returns and the per-origin
returns. The active strategy is not changed: the alert is a recommendation for
the approval path. Automatic rollback is deliberately not configured; if a
future policy adds it, it must declare explicit thresholds and safeguards first.

## 4. Experiments

`create_experiment` records the strategy, baseline configuration and version,
instruments, training/validation windows, objective, method, seed, evidence
references, risk-policy version, approval policy, minimum trade/observation
counts, candidate bounds, status, timestamps and results.

Methods are:

* `grid` — a deterministic lattice over the declared bounds;
* `random` — seeded random search over the same bounds;
* `walk_forward` — seeded one-parameter-at-a-time candidates, each with an
  interpretable sensitivity trail.

`PARAMETER_BOUNDS` declares bounds for all six catalogue strategies. An explicit
candidate is validated against the catalogue and the same bounds; a `trend_sma`
candidate must have `fast_period < slow_period`. No parameter outside the
declared bounds is perturbed, and at most 25 candidates are generated.

Experiment creation is idempotent: a digest over the strategy, baseline,
candidates, windows, instruments, objective, method, seed and policy returns the
existing experiment with `replayed=true`. Experiment and candidate transitions
are checked by `ensure_experiment_transition` and
`ensure_candidate_transition`; an illegal move raises instead of overwriting a
terminal state.

`run_experiment` executes under the bounded semaphore; `start_experiment`
queues a background task under the same limit. Cancellation is cooperative and
also cancels a running task. A cancelled experiment cannot run. On restart,
`recover()` marks interrupted `queued`/`evaluating` experiments `failed` and
candidate rows `evaluation_failed`; it does not delete their results.

## 5. Candidate evaluation

Each candidate is evaluated by a thread-private NM-11 engine because SQLite
connections are thread-bound. The engine runs:

1. a training replay with the candidate configuration;
2. a separate validation replay;
3. baseline training and validation replays with the approved configuration;
4. walk-forward folds on the validation window;
5. a one-parameter sensitivity probe;
6. NM-11 `regime_analysis` over the validation equity curve.

NM-11 applies transaction costs and slippage, so the result records
`costs_included.total_fees` and `costs_included.total_slippage`. The comparison
contains training and validation metrics for the candidate and baseline,
out-of-sample flag, overfitting gap and flag, walk-forward folds and
consistency, sensitivity range, regime coverage, risk compliance and reasons.

A candidate is rejected when any of these fail:

* validation has fewer observations or trades than required;
* the validation return does not improve on the baseline by 0.002;
* drawdown is materially worse without proportional return;
* Sharpe worsens by more than 0.25;
* training return exceeds validation by more than 0.15 (or training is
  positive while validation is non-positive);
* walk-forward produces no folds or consistency below 0.50;
* sensitivity range exceeds 0.20;
* regime analysis is unavailable;
* the candidate configuration violates the NM-08 policy.

The returned `score` combines return, Sharpe and drawdown, but eligibility is
never taken from the score. An evaluation exception becomes a
`evaluation_failed` candidate with the error in `reasons`, not a crash.

## 6. Governance

Candidate states are the required set:

```text
GENERATED -> EVALUATING -> EVALUATION_FAILED
                        -> REJECTED
                        -> PENDING_APPROVAL -> APPROVED -> SHADOW_VALIDATION
                                                        -> PROMOTED -> ROLLED_BACK
```

`ARCHIVED` is terminal. A generated candidate cannot be approved, promoted or
shadow-validated. Approval requires `pending_approval`, risk compliance and,
when the experiment policy says `require_shadow`, a passed shadow validation.
Manual approval is the default. Automatic approval is refused unless the
experiment declares `approval_policy.mode=auto` and `auto_approve=true`;
`auto_promote` additionally requires that explicit policy, but the default is
false, so even an automatic approval does not change the active strategy.

Rejection records the actor, reason and time. Every approval rejects automatic
requests that were not explicitly enabled. Promotion requires an approved,
risk-compliant candidate and passes through the one strategy applier.

## 7. Shadow validation

Where policy requires it, `shadow_validate_candidate`:

* creates a controlled NM-12 session for the candidate and a second session for
  the baseline, with the same instruments and observations;
* never replaces the approved baseline;
* runs the requested cycles and compares candidate/baseline decision counts,
  mean hypothetical return and risk approval ratios;
* requires the configured minimum decisions for both sessions;
* returns `INSUFFICIENT_EVIDENCE` when the sample is too small;
* fails when the candidate trails the baseline by more than the tolerance
  (default 1 percentage point) or its risk approval ratio falls by more than
  0.15;
* records the passed candidate session as `shadow` evidence with
  `hypothetical=true`.

A validation pass leaves the candidate `pending_approval`, so approval remains
a separate decision. A failed validation rejects the candidate. Shadow
validation is unavailable, not silently skipped, when no NM-12 service is
configured.

## 8. Risk governance

The NM-08 `RiskPolicy` remains authoritative. Learning loads policy values but
never writes them. Candidate risk compliance checks position/sector/invested
weights, cash, drawdown, volatility, liquidity, turnover and aggregate sector
weight, and rejects a candidate even if its return is high. `approve_candidate`
re-checks the stored compliance result; promotion does too. No learning method
can override a risk rejection, disable a check, alter ledger rules or increase
capital allocation.

## 9. Versioning and rollback

Every promotion writes a `StrategyVersionRecord` with a deterministic version
id, strategy and catalogue id, parent version, full configuration snapshot,
creation time, description, experiment and candidate references, evidence
references, evaluation, approval and promotion records and status. The active
version pointer is explicit. Old version rows are never rewritten; rollback
only changes the pointer and appends a `RollbackRecord` with from/to version,
reason, actor and time.

Rollback refuses an unknown version, a version belonging to another strategy,
a non-promoted target, the currently active version, a missing reason, an
invalid target configuration or a missing applier. It applies the target
through the same applier, records the rollback, moves the active pointer and
marks the replaced candidate `rolled_back` in one learning-database
transaction. If the applier changed the strategy and the database transaction
then fails, the previous configuration is re-applied; the failure is raised and
the restore attempt is logged in `last_error`. Promotion has the same
compensation path. Failed candidates and experiment results are never deleted.

## 10. Reliability

* background execution is bounded by `max_concurrency` (default 2);
* both queued and direct runs acquire the same semaphore;
* cancellation sets a cooperative flag and cancels the task;
* restart recovery fails interrupted work explicitly;
* evidence and experiment reads are bounded by `limit`;
* mutation failures surface as `LearningValidationError` (422),
  `LearningNotFoundError` (404) or `LearningStateError` (409) over HTTP;
* event publication failures never roll back committed learning state;
* learning has its own store and does not block the market services.

## 11. API and events

The 26 routes under `/market/learning` are:

```text
GET  /market/learning/status
GET  /market/learning/contracts
GET  /market/learning/dashboard
GET  /market/learning/health
POST /market/learning/evidence
GET  /market/learning/evidence
GET  /market/learning/evidence/{evidence_id}
POST /market/learning/monitor
GET  /market/learning/alerts
POST /market/learning/alerts/{alert_id}/acknowledge
POST /market/learning/experiments
GET  /market/learning/experiments
GET  /market/learning/experiments/{experiment_id}
POST /market/learning/experiments/{experiment_id}/start
POST /market/learning/experiments/{experiment_id}/run
POST /market/learning/experiments/{experiment_id}/cancel
GET  /market/learning/experiments/{experiment_id}/candidates
GET  /market/learning/candidates/{candidate_id}
POST /market/learning/candidates/{candidate_id}/evaluate
POST /market/learning/candidates/{candidate_id}/approve
POST /market/learning/candidates/{candidate_id}/reject
POST /market/learning/candidates/{candidate_id}/shadow-validate
POST /market/learning/candidates/{candidate_id}/promote
GET  /market/learning/strategies/{strategy_id}/versions
GET  /market/learning/strategies/{strategy_id}/active-version
POST /market/learning/strategies/{strategy_id}/rollback
```

They are declared in `api/app.py`, `ApiSurface`, `ROUTE_CONSUMERS` and
`docs/API.md`. The market surface is 204 routes; NM-14 adds the standalone
workspace page and `GET /market/overview` aggregation endpoint.

Sixteen `novamarket.learning.*` events are implemented:

```text
evidence.recorded, evidence.rejected, monitor.completed, alert.raised,
alert.acknowledged, experiment.created, experiment.started,
experiment.completed, experiment.failed, experiment.cancelled,
candidate.evaluated, candidate.rejected, candidate.approved,
candidate.shadow_validated, candidate.promoted, strategy.rolled_back
```

They use the existing event bus and typed payload contracts; no second bus was
created. The implemented event contract now has 167 events.

## 12. Limitations

* Optimisation is bounded grid/random/walk-forward search, not unrestricted
  model training. The implementation does not claim a machine-learning model.
* Shadow validation depends on observations supplied to NM-12; no live provider
  or real order feed is implied.
* Regime labels are descriptive, not predictive.
* Paper and shadow evidence are not live performance.
* The strategy applier is an external seam. Promotion/rollback compensation
  restores the previous configuration if the learning database write fails, but
  it cannot make the two databases one atomic transaction; a failed restore is
  reported in `last_error` rather than hidden.
* NM-14 adds a read-only Adaptive Learning section to the standalone workspace
  (`GET /market/workspace`). It shows live learning status and experiments; it
  does not add financial or execution controls.
* Per-instrument and per-horizon performance is supported for evidence that
  carries those breakdowns (notably NM-12 analytics). The NM-10 importer keeps
  aggregate realized P&L and costs but does not synthesize a per-instrument
  attribution from fills, and NM-11 results remain aggregate.
* Instrument coverage and a minimum market-regime coverage are recorded but are
  not hard rejection gates; only sample count and the explicit evidence checks
  are hard gates.
* Shadow validation runs synchronously from `pending_approval` or `approved`.
  The declared `SHADOW_VALIDATION` state is not persisted as a separate row.
* Structured logging and explicit CPU/memory ceilings were not added; the
  controls are bounded candidate counts, bounded concurrency and persisted
  progress/error state.
* Automatic rollback is not configured. Post-promotion monitoring creates a
  `rollback_recommended` alert for approval instead.

See `docs/NM13_TEST_REPORT.md` for executed tests and `docs/API.md` for the
generated route reference.
