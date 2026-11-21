# PI-16 Completion Report — Adaptive Learning and Market-Regime Intelligence

## Scope delivered

PI-16 adds an isolated research package at
`src/novacontrol/novamarket/adaptive/` and the React Adaptive Intelligence
workspace. It consumes existing evidence without duplicating regime detection,
forecasting, policy evaluation, training or risk.

Implemented:

- PI-04 regime snapshots preserving primary/secondary labels, uncertainty
  (`confidence=None` when the source is uncertain), feature quality and versions.
  Unknown or aliased labels are not rewritten into a different regime.
- Regime-specific policy evaluation records with policy/data/config versions and
  explicit sufficient/insufficient evidence states.
- Deterministic change detection: PSI, two-sample KS with asymptotic p-value,
  Jensen-Shannon divergence, Page-Hinkley, CUSUM, Fisher-z relationship change,
  rolling error ratio and action-distribution shift. CUSUM now contributes to the
  concept-drift decision, not only to stored diagnostics.
- Persistent data/concept drift, temporary/persistent degradation and
  regime-transition classifications with explicit insufficient-data states.
  Windows with no sufficiently sampled feature report `insufficient_evidence`
  instead of `no_drift`.
- Suitability research over regime return, risk-adjusted return, drawdown,
  volatility, forecast uncertainty, stability, costs, invalid actions, sample
  size and freshness. Return and drawdown evidence are mandatory; missing
  material evidence caps a result at `conditionally_suitable`.
- Hysteresis and cooldown-controlled recommendations with severe-escalation
  bypass, acknowledgement and retraining recommendations. Acknowledged records
  stay acknowledged, cooldown is anchored to the last action change rather than
  reset by every poll, and materially stronger evidence may release suppression.
- Regime-aware, point-in-time experience retrieval and annotations. Historical
  queries reject evidence timestamped or available after `as_of`; future
  annotations and future evaluation windows are rejected.
- Explicit bounded reassessment through
  `TrainingManager.evaluate_regime_window`, reusing PI-14 evaluation and PI-15
  artifacts, including 2–20 chronological, non-overlapping walk-forward windows
  with per-fold transition budgets, per-fold cached evidence and baseline
  comparison. It never trains, promotes or activates a policy.
- Out-of-sample reassessment scoping (`adaptive/scope.py`). Every reassessment
  window is classified against the policy artifact's own
  `dataset_provenance["splits"]`: a window overlapping the training split, or the
  validation split that selected the checkpoints, is refused with the offending
  split, its interval and the overlap fraction. `allow_in_sample: true` records
  the assessment instead, and the flag rides into the record, each walk-forward
  fold, the aggregate and the stored regime evidence. Suitability selects only
  evidence that is not flagged in-sample and refuses an explicit in-sample
  `evaluation_id`; an artifact with no recorded splits reports
  `out_of_sample: null` (unverified) and is never claimed clean. Reassessment
  evidence is keyed to the policy's own configuration hash, so out-of-sample
  evidence can actually feed suitability.
- Reassessment lifecycle recovery: a `RUNNING` job left by process restart becomes
  `FAILED` with an interruption reason; completed evidence for a different policy
  artifact becomes `stale_evidence` instead of being silently reused; insufficient
  fold evidence reports `insufficient_evidence`.
- Configuration continuity: service startup restores the most recently persisted
  `AdaptiveConfig` (custom cooldown, record limit and thresholds survive a
  restart) instead of overwriting the stored record with defaults. An explicitly
  passed configuration still wins, and an unreadable stored record falls back to
  defaults with the reason in `last_error` rather than failing startup.
- Isolated `adp_*` SQLite tables, bounded record counts, idempotent event IDs,
  persistence and status/health/contract/dashboard surfaces. Record-limit and
  configuration-limit errors are explicit; idempotent replays still work at the
  limit.
- 31 authenticated `/market/adaptive/*` API routes, API-surface and route-consumer
  declarations, and workspace navigation.
- The workspace shows regime state, stored regime evaluations, suitability,
  market/data/concept drift, degradation, recommendations, retraining and
  reassessment history. It can create and run an explicit reassessment; it uses
  stored PI-14 evidence for suitability rather than inventing a fresh evaluation
  timestamp from hand-entered metrics.

No automatic policy promotion, activation, retraining, broker or live-trading
path was added. NM-08 remains the risk authority and NM-10 remains the
paper-execution authority.

## Audit corrections in this verification pass

- Fixed bounded-store behavior so an idempotent replay at the record limit
  returns the existing record instead of failing, and capped configuration and
  stateful upsert tables.
- Made configuration parsing reject unknown fields and zero thresholds that could
  produce division-by-zero or meaningless detector behavior.
- Fixed CUSUM being computed but ignored by concept-drift classification.
- Fixed suitability silently treating explicit `0.0` invalid-action rates as
  missing, selecting an older stored evaluation over a newer one, accepting
  mismatched evaluation configuration hashes, and claiming suitability without
  drawdown/risk evidence.
- Fixed acknowledged recommendations being reopened and cooldown being reset on
  every identical poll.
- Fixed interrupted reassessment recovery, stale-artifact reuse, whole-range cache
  reuse for walk-forward jobs, and version labeling after artifact changes.
- Fixed the real PI-15/PI-14 integration boundary: PI-14 reports the evaluator
  adapter version as `policy_version`, which the adaptive recorder compared to
  the trained artifact version and rejected. Reassessment now explicitly records
  the immutable PI-15 artifact version while leaving PI-14's adapter metadata in
  its own evaluation payload. A real trained-DQN integration test pins this.
- Fixed newest-regime lookup using insertion order rather than source timestamp,
  and dashboard regime lookup requiring a feature service when explicit
  annotations exist.
- Mapped adaptive resource exhaustion to HTTP 409 instead of 500.
- Replaced the workspace's fabricated “evaluated now” metrics with stored-evidence
  assessment, added concept-drift and reassessment controls, and exposed stored
  regime evaluations.
- Fixed the stale reassessment empty state that still described the workspace as
  read-only after the create-and-run control was added.
- Made reassessment genuinely out-of-sample: overlapping windows were accepted
  silently and their evidence counted as ordinary suitability input. They are now
  refused (HTTP 422 naming the split) or recorded only under an explicit
  `allow_in_sample` flag that suitability never uses.
- Fixed reassessment evidence being keyed to PI-14's evaluation configuration
  hash instead of the policy's own. The stale-evidence check therefore rejected
  every real reassessment, so reassessment evidence could never feed suitability;
  a real PI-15 artifact now produces usable out-of-sample evidence.
- Added a Scope column (out-of-sample / in-sample / unverified) to the stored
  evidence and reassessment tables so an in-sample or unverifiable assessment is
  visible instead of implied.
- Fixed suitability mis-reading real PI-14 evidence. PI-14 publishes most
  metrics as `{value, unit, sufficient, observations}` objects, and the adaptive
  layer treated them as bare numbers: a percent metric could be scaled twice, a
  metric PI-14 had marked insufficient was still counted, and volatility and
  invalid-action rate were reported "not supplied" although PI-14 supplies them
  as `financial.volatility` / `annualized_volatility` and
  `decision.invalid_action_frequency`. Metric reading is now unit- and
  sufficiency-aware with those fallbacks; nothing is invented when PI-14 leaves
  a value null.
- Fixed startup discarding the operator's persisted configuration: every restart
  saved defaults over the stored record, so a custom cooldown or record limit
  silently reverted. Startup now restores the latest persisted configuration,
  and a stored configuration that cannot be parsed falls back to defaults without
  aborting startup (its reason is reported).

## Evidence and test results

| Check | Result |
| --- | --- |
| `tests/test_novamarket_pi16.py` | **41 passed** |
| `tests/test_novamarket_pi16_reassessment_scope.py` | **15 passed** |
| `tests/test_novamarket_pi16.py` restart tests | **4 passed** (custom cooldown + record limit survive restart, newest configuration wins, explicit argument overrides, unreadable record falls back) |
| Live exercise of the Adaptive workspace (`:5173` against `:8011`, real trained PI-15 policy) | suitability `conditionally_suitable` (score 0.554) from out-of-sample evidence; degradation `persistent_degradation` with return/drawdown/volatility/sharpe triggers; recommendation `reevaluate_policy` (medium evidence) recorded; retraining recommendation recorded with a 2026-01-05 → 2026-06-30 window; reassessment `completed` at 18 transitions, out-of-sample |
| Live restart (`:8011`, real research DB) | custom config (`cooldown_seconds` 1234, `max_records` 750) applied, API restarted, status reported the same values; defaults restored afterwards |
| PI-15 + PI-16 suites together | **75 passed** |
| `python -m mypy src/novacontrol/novamarket/adaptive` | clean, 6 source files |
| Live API (`:8011`, real trained PI-15 policy) | in-sample windows refused **422** (training and validation overlap both named); `allow_in_sample` recorded `in_sample: true`; test-split window recorded `out_of_sample: true`, `scope_verified: true`; suitability returned `conditionally_suitable` from out-of-sample evidence with `in_sample_excluded: 2` |
| `npm run build` in `web` | passed (TypeScript + Vite) |
| Affected regression: features, ensemble, evaluation, PI-13, PI-14, PI-15, PI-16 | **230 passed, 3 subtests passed** |
| `tests/test_novamarket_api.py` | **61 passed, 325 subtests passed** |
| `python -m mypy src` | clean, 662 source files |
| `python -m mypy src --platform win32` | clean, 662 source files |
| Real PI-15 trained policy through PI-14/PI-16 reassessment | completed, 28 transitions, three baselines, artifact version recorded |
| `ruff check` on the adaptive package and focused tests | clean |
| `npm run build` in `web` | passed (TypeScript + Vite) |
| `scripts/generate_api_reference.py --check` | in sync, **747 routes** |
| `git diff --check` | clean |

The focused tests cover initialization/contracts, PI-04 uncertainty preservation,
regime evaluation, source-time ordering, suitability evidence gates and version
mismatch, market-change persistence, data/concept drift including CUSUM
integration, future-information rejection, degradation persistence, hysteresis and
cooldown expiry, acknowledgement retention, recommendations/retraining,
regime-aware retrieval, reassessment caching/bounds/walk-forward/stale-artifact/
restart recovery, store-limit idempotency, explicit not-found states, detectors,
JSON serialization, and static/runtime financial isolation (no finance or
execution imports; storage tables are only `adp_*`). API tests use the real
`create_app()` factory and isolated data directory.

## Safety review

- All adaptive state is in `adp_*` tables in the existing isolated research SQLite
  file; no production finance table is read or written.
- Config rejects automatic promotion/training settings and unknown fields.
- Suitability never returns authorization; `suitable` is explicitly a research
  label.
- Recommendation creation and acknowledgement do not mutate a policy or execute
  anything.
- Reassessment evaluation is explicit, bounded, versioned and non-training.
- Future observation, outcome, availability and evaluation timestamps are
  rejected; experience queries filter on availability at `as_of`.
- No activation, promotion, order, broker or live-execution route exists under
  `/market/adaptive/`.

## Remaining limitations and unverified requirements

- There is no automatic walk-forward scheduler. Callers/UI submit explicit
  historical windows; each fold has a transition cap and the job checks elapsed
  time, but a single in-flight evaluator call is not preemptively cancelled.
- Reassessment execution is verified with both a deterministic evaluator and a
  freshly trained PI-15 DQN artifact evaluated over an explicit historical
  window. This found and fixed a real boundary bug: PI-14's `policy_version` is
  the adapter version, while adaptive evidence must be keyed to the immutable
  PI-15 artifact version. The integration test now pins that mapping.
- Scope verification depends on the artifact recording its splits. An artifact
  whose `dataset_provenance` has no split entries is reported
  `out_of_sample: null` with `scope_verified: false`: it is not refused (its data
  is simply unknown) and it is never described as clean. Such evidence, and
  legacy stored evidence recorded before scoping existed, stays eligible for
  suitability while the suitability record carries the unverified scope, so no
  claim is made that cannot be supported. Rejecting unverified artifacts instead
  would silently invalidate existing PI-16 evidence.
- The focused fixture is software evidence, not a profitable strategy. No
  profitability or generalization claim is made.
- The full repository test suite was not run. The Adaptive Intelligence page was
  exercised in Chromium against the real API through the Vite proxy: market-change
  detection produced a 5-signal potential regime transition, data drift wrote and
  refreshed a persisted row, concept drift returned `no_drift`, and regime
  analysis returned 200. The runtime had no trained PI-15 policies, so policy-only
  controls (suitability, degradation, recommendations, reassessment) were verified
  as correctly disabled but were not exercised through the browser.
- PI-13 remains single-instrument, PI-15 DQN is the only trained algorithm, and
  bit-for-bit cross-hardware determinism is not claimed.
- This work was not staged, committed, pushed or released, and PI-17 was not
  started.
