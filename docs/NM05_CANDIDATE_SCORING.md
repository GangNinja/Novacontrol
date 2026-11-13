# NM-05 — Candidate Scoring

**Methodology version:** `nm05-score-1.0`.

The model answers one question: *how much attention does this instrument's
current, evidenced situation deserve?* It does not say whether the instrument is
attractive, does not predict a return, and has no direction. A high score is
**discovery priority**, not a buy signal.

## 1. Factors and weights

| factor | weight | normalized input | saturates at |
| --- | --- | --- | --- |
| `market_activity` | 0.22 | absolute % close change across the stored bars | 5 % |
| `liquidity` | 0.18 | `log10(median(close × volume) + 1)` | 1e9 traded value |
| `volatility` | 0.15 | population stdev of bar-to-bar returns | 4 % dispersion |
| `event_relevance` | 0.18 | sum of mapped NM-04 event weights | 1.0 |
| `sector_relevance` | 0.08 | sum of sector-level event weights | 1.0 |
| `freshness` | 0.11 | age of the newest usable observation vs `stale_after_hours` | 72 h → 0 |
| `discovery_reason` | 0.08 | value of the reason that produced the candidate | 1.0 |

Weights sum to **1.0** and are published in `scoring.WEIGHTS` and in
`engine.contract()["scoring"]`, so every contribution is auditable.

### Why these weights

* Activity and events answer **why now** and carry 40 % between them.
* Liquidity and volatility (33 %) decide whether the activity is measurable and
  noteworthy at all.
* Freshness (11 %) is a gate: old data caps the score regardless of the rest.
* The discovery reason (8 %) is deliberately the smallest factor — a pre-filter
  may nudge a candidate but can never carry one.

## 2. Event weighting

`event_weight = importance × verification × relationship`

| importance | weight | verification | weight | relationship | weight |
| --- | --- | --- | --- | --- | --- |
| low | 0.25 | officially_confirmed | 1.0 | direct | 1.0 |
| moderate | 0.5 | multi_source_corroborated | 0.9 | indirect | 0.6 |
| high | 0.75 | single_source | 0.5 | uncertain | 0.3 |
| critical | 1.0 | conflicting_evidence | 0.4 | | |
| | | unverified | 0.15 | | |
| | | retracted / expired | 0.0 | | |

So an officially confirmed critical direct event contributes 1.0, while an
unverified rumour of the same importance contributes 0.15, and a retracted event
contributes nothing. The sum is clamped to 1.0. Events older than
`event_window_days` (14) are ignored.

Sector events go to `sector_relevance` only; a sector-level story never appears
as a direct instrument event.

## 3. Missing data and data quality

A factor with no usable input is **missing** (`value: null`, `contribution: 0`)
and subtracts **half its weight** from the total. Missing is never zero: an
instrument with no volume history is *unmeasured*, not illiquid.

| quality | when | cap |
| --- | --- | --- |
| `GOOD` | quote or ≥2 bars, fresh, nothing missing | 100 |
| `PARTIAL` | at least one factor missing | 60 |
| `STALE` | newest observation older than `stale_after_hours` | 35 |
| `INSUFFICIENT` | no quote and fewer than 2 bars | 15 |

The caps are enforced twice: the score is clamped in the model **and**
`CandidateEvaluation.__post_init__` refuses a stale evaluation above 35 or an
insufficient one above 15. A data outage therefore cannot produce a high ranking.

## 4. Tiers and confidence

| tier | score |
| --- | --- |
| `priority` | ≥ 70 |
| `review` | ≥ 50 |
| `monitor` | ≥ 30 |
| `background` | < 30 |

`confidence` is the fraction of total factor weight that had usable input. It is
reported separately from the score and from `data_quality`; a high score built on
few factors shows low confidence.

## 5. Explainability payload

Every evaluation persists and serves:

* `score`, `tier`, `confidence`, `data_quality`;
* `factors[]` — name, normalized value, weight, contribution, a human reason
  (“median traded value 1,070,000 over 3 bars”) and the `missing` flag;
* `missing_fields[]`, `notes[]`, `event_ids[]`;
* `methodology_version`, `evaluated_at`, and a `digest` of the inputs;
* `is_trading_recommendation: false` and `is_price_prediction: false`.

The contributions plus missing penalties reconstruct the score; the tests assert
`contribution == value × weight × 100` for every present factor.

## 6. Reproducibility

`digest_inputs` hashes a canonical JSON document containing the instrument,
bars, quote, events, reason and policy. The same inputs produce the same digest
and the same score; a changed quote produces a different digest. Evaluations are
append-only, so the history of what the model saw and decided is never rewritten.

## 7. Configurability

The weights and thresholds are code-level constants with a version. Changing a
weight means a new `METHODOLOGY_VERSION` and a new evaluation record — never a
silent edit to stored scores. `DiscoveryPolicy` holds every threshold
(`add_threshold`, `remove_threshold`, `downgrade_delta`, `upgrade_delta`,
`remove_after_cycles`, `stale_after_hours`, `event_window_days`, caps) and is
exposed in `status()`, so a later phase can evaluate the model against a policy
without changing the schema.
