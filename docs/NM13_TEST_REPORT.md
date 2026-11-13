# NM-13 — Test Report

All results below were produced in the `novamarket` workspace with the project
interpreter:

```text
PYTHONPATH=src ../.venv/Scripts/python.exe ...
```

This is a verification-pass report. It records targeted phase and dependency
tests, not a claim that the full repository suite was rerun after the final
edits.

## 1. NM-13 learning suite

`tests/test_novamarket_learning.py` — **30 passed** in 4.66s.

| Area | Test | What it proves |
| --- | --- | --- |
| Evidence | `test_accepted_evidence_keeps_provenance_and_is_idempotent` | origin, reliability label, digest, duplicate replay |
| Evidence | `test_missing_sample_and_future_data_are_rejected` | sample floor and future-window leakage |
| Evidence | `test_stale_and_in_sample_evidence_are_graded` | freshness ceiling and low-confidence in-sample evidence |
| Evidence | `test_invalid_kind_is_refused` | unsupported provenance is refused |
| Evidence | `test_missing_outcomes_and_malformed_observations_are_refused` | a missing return/Sharpe outcome and a non-integer observation count are refused |
| Evidence | `test_paper_import_preserves_realized_pnl_fees_and_slippage` | NM-10 `position_effect.realized_delta`, `total_fees`, `slippage_amount` and provenance |
| Evidence | `test_shadow_import_keeps_instrument_and_horizon_breakdowns` | NM-12 `by_instrument` / `by_horizon` survive import and snapshot aggregation |
| Evidence | `test_invalid_or_reversed_timestamps_are_refused` | malformed ISO timestamps raise a validation error; start-after-end is rejected |
| Monitoring | `test_deterioration_and_divergence_are_reported_without_averaging` | return deterioration, volatility/signal/risk/instability anomalies, cross-origin divergence |
| Monitoring | `test_insufficient_window_withholds_metrics` | no fabricated metrics on a small sample |
| Experiment | `test_real_nm11_evaluation_records_training_and_validation` | real NM-11 replay, out-of-sample result, costs, walk-forward, sensitivity, regime, risk, idempotent creation |
| Experiment | `test_parameter_bounds_and_windows_are_validated` | declared bounds, window ordering, future rejection, explicit auto approval |
| Experiment | `test_cancelled_experiment_cannot_run` | terminal cancellation |
| Governance | `test_manual_approval_promotion_and_rollback` | manual approval, two promotions, active version, rollback, preserved version snapshot |
| Governance | `test_auto_policy_with_shadow_validates_before_approval` | shadow validation runs before automatic approval, not after it |
| Governance | `test_auto_policy_without_shadow_leaves_candidate_pending` | missing NM-12 leaves a completed experiment with a pending candidate instead of failing it |
| Governance | `test_post_promotion_monitor_recommends_rollback` | promoted-version degradation creates one recommendation without changing the active version |
| Governance | `test_invalid_rollback_is_refused` | unknown, same-active and reason-less rollback |
| Governance | `test_automatic_approval_requires_explicit_policy` | automatic approval is refused by default |
| Governance | `test_explicit_auto_policy_approves_without_promoting` | explicit policy approves; auto-promotion stays false |
| Governance | `test_risk_policy_violation_rejects_a_candidate` | risk rejection blocks approval |
| Shadow | `test_shadow_validation_pass_then_approval` | candidate and baseline sessions, pass, then separate approval |
| Shadow | `test_insufficient_shadow_evidence_blocks_approval` | `INSUFFICIENT_EVIDENCE` cannot be approved |
| Shadow | `test_failed_shadow_validation_rejects` | candidate trailing baseline is rejected |
| Reliability | `test_concurrency_limit_serialises_direct_runs` | `max_concurrency=1` serialises two direct evaluations |
| Reliability | `test_cancellation_during_evaluation_does_not_complete` | a cancellation during the final evaluation is not overwritten by `completed` |
| Reliability | `test_a_failed_promotion_record_restores_the_previous_config` | failed version write applies the previous configuration and leaves the candidate approved |
| Reliability | `test_recovery_fails_interrupted_work` | restart recovery fails queued/evaluating work |
| Isolation | `test_learning_database_is_its_own_file` | separate `novamarket_learning.sqlite3` |
| Isolation | `test_learning_database_is_in_memory_without_a_directory` | deterministic in-memory mode |

The real evaluation tests use deterministic `_Market` bars and the real NM-11
`BacktestEngine`; governance tests replace only the evaluation result so
approval, shadow and rollback can be exercised independently of which candidate
happens to win on a synthetic series.

### Observed result on the deterministic 180-bar fixture

| Candidate | Validation return | Baseline return | Trades | Walk-forward folds / consistency | Fees / slippage | Verdict |
| --- | ---: | ---: | ---: | --- | --- | --- |
| fast=3, slow=8 | 2.90% | 3.42% | 2 | 5 / 0.20 | 9.08 / 18.43 | rejected |
| fast=4, slow=10 | 2.09% | 3.42% | 2 | 5 / 0.20 | 9.11 / 18.15 | rejected |

Both candidates were rejected because validation return did not beat the
baseline by the required 0.002, Sharpe fell (7.520 -> 6.372 / 5.125), and
walk-forward consistency was below 0.50. Overfitting was not flagged. These are
synthetic-fixture results, not a claim about live strategy performance.

## 2. HTTP lifecycle

`tests/test_novamarket_api.py::NovaMarketLearningApiTests` — **1 passed**.

It creates a real strategy, records evidence with provenance, verifies duplicate
replay, lists and fetches the evidence, monitors all three origins, creates an
experiment, checks its one generated candidate, confirms approve/shadow/promote
are refused from `generated`, cancels the experiment, confirms it cannot run,
checks an empty version history, and checks a missing candidate is 404. It also
asserts the learning surface reports no execution connection.

Contract tests pin:

* **204** market API routes, including the 26 learning routes; NM-14 adds the
  separate `/market/workspace` HTML page and `/market/overview` API;
* **47** market capabilities, including the four learning capabilities;
* **167** implemented events;
* the exact `/market/*` path set contains no live-trading route.

## 3. Dependency / integration verification

Final affected integration run after the last edits:

```text
pytest tests/test_novamarket.py tests/test_novamarket_api.py \
  tests/test_novamarket_shadow.py tests/test_novamarket_backtesting.py -q
```

**212 passed, 275 subtests passed** in 58.95s.

The broader relevant run, before the last importer/monitor refinements, also
included strategies, execution and risk:

```text
pytest tests/test_novamarket.py tests/test_novamarket_api.py \
  tests/test_novamarket_shadow.py tests/test_novamarket_backtesting.py \
  tests/test_novamarket_strategies.py tests/test_novamarket_execution.py \
  tests/test_novamarket_risk.py -q
```

**321 passed, 275 subtests passed** in 70.55s. The API/execution/risk subset was
also rerun alone after the paper importer change: **112 passed**, 211 subtests.

The full NovaMarket suite was **696 passed, 2 skipped, 339 subtests** before
this verification pass. It was not rerun after the final edits, per the request
not to run the full suite unless necessary.

## 4. Static checks

| Check | Result |
| --- | --- |
| `mypy src` | 547 source files, no issues |
| `mypy src --platform win32` | 547 source files, no issues |
| `python scripts/generate_api_reference.py --check` | `docs/API.md: in sync` (575 API routes) |
| `git diff --check` | no whitespace errors |

## 5. Defects found and repaired in this pass

1. **Automatic shadow approval was ordered incorrectly.** The auto path called
   `approve_candidate` before shadow validation, but approval refuses a policy
   that requires a passed shadow. Automatic policies with `require_shadow` could
   therefore fail an already-completed experiment. Shadow validation now runs
   first; a missing NM-12 service leaves the candidate pending and returns a
   clear `automatic_approval=skipped` result instead of raising.
2. **Paper evidence was not reading NM-10 outcomes.** The importer looked for a
   `fees` key (the fill field is `total_fees`) and a fill-level `realized_pnl`
   that NM-10 does not emit, while ignoring `position_effect.realized_delta` and
   `slippage_amount`. Paper evidence now records realized P&L, total fees,
   slippage and win/trade counts; a window with no realized outcome is rejected.
3. **Missing outcomes could be accepted.** The eligibility gate checked for the
   presence of a metric key, not a usable numeric value, so `return_pct: null`
   could pass. It now requires a numeric return or Sharpe.
4. **Malformed and reversed timestamps.** A bad ISO string raised a raw
   `ValueError`; a `window_start` after `window_end` was accepted. The former is
   now a 422-class `LearningValidationError`; the latter is rejected with a
   reason.
5. **Cancellation could be overwritten.** Cancellation was checked at the start
   of each candidate, not before final completion, so cancelling during the last
   evaluation could produce `completed`. There is now a check before completion.
6. **Declared anomaly kinds were unused.** Monitoring now emits measured
   `volatility_spike`, `signal_shift`, `risk_increase` and `instability` alerts.
7. **Shadow breakdowns were dropped.** NM-12 `by_instrument` / `by_horizon`
   analytics now survive import and are weighted into snapshot metrics.
8. **Approval write was not atomic.** Candidate state and the approval audit row
   are written in one learning-database transaction.

## 6. Remaining issues and unverified requirements

* Per-instrument and per-horizon performance is supported when evidence carries
  breakdowns (notably NM-12). The NM-10 importer records aggregate realized P&L,
  fees and slippage, not a synthesized per-instrument attribution; NM-11
  evidence remains aggregate.
* Instrument coverage and a minimum market-regime coverage are visible in
  evidence/regimes but are not hard rejection gates. Sample count and the
  explicit freshness/timestamp/outcome checks are hard gates.
* The `SHADOW_VALIDATION` candidate state is declared but shadow validation is
  synchronous and does not persist a separate row in that state.
* Structured logging and explicit CPU/memory ceilings were not added. The
  controls are bounded candidate counts, bounded concurrency and persisted
  progress/error state.
* No dedicated NM-13 UI panel exists. Dashboard, status, health, evidence,
  experiment, approval and version endpoints are the integration surface.
* Automatic rollback is not configured; the monitor creates a
  `ROLLBACK_RECOMMENDED` alert for approval.
* Promotion/rollback span two stores. Compensation is tested; a failed restore
  is surfaced in `last_error`, not made atomic.
* No live market-data provider or live shadow observation feed was exercised.
  The earlier Yahoo attempt returned HTTP 429. The strategy applier is tested
  with an in-memory recorder, not a live broker.

## 7. Final verification

The final targeted phase and dependency checks are green. The completion report
records the core status and the remaining unverified requirements; the prior
full-suite count is not presented as a post-fix result.
