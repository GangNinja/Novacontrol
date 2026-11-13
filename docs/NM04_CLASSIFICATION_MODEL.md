# NM-04 — Classification Model

Importance answers "how much does this event matter to the market it belongs
to", on a **0–100 scale**, with every contribution shown. It does not answer
"which way prices will move". There is no direction, target, expected return or
recommendation field anywhere in the model.

## 1. Bands

| Band | Score |
| --- | --- |
| `critical` | ≥ 75 |
| `high` | ≥ 55 |
| `moderate` | ≥ 35 |
| `low` | ≥ 0 |

`band(score)` is the only place these thresholds live; the contract endpoint
publishes the same numbers, so a client and the server cannot disagree.

## 2. Factors

`classify(...)` scores an event from explainable inputs and returns a
`Classification`:

| Field | Meaning |
| --- | --- |
| `score` | 0–100, the sum of the factor contributions |
| `importance` | `band(score)`, after the model's reliability caps |
| `confidence` | 0.0–1.0, how well the event is evidenced — a separate axis |
| `factors` | each factor's `name`, `value`, `weight`, `contribution` and `reason` |
| `explanation` | one sentence naming the dominant factor |

The factor weights are published as `FACTOR_WEIGHTS` and served through the
radar contract, so the model can be inspected without reading the source.
Recency decays with age (`recency_value`: 1.0 within six hours down to 0.1 after
a week, 0.35 when the publication time is unknown) and a future-dated publication
is treated as an anomaly rather than as freshness. Verification strength is a
factor, so a corroborated or officially confirmed event outranks an otherwise
identical single-source one.

The model also **caps** a band by the strength of the evidence: repetition from
one origin cannot lift an event into `critical`, and the cap does not rewrite the
score, so a reader can see both "the raw score" and "the band the evidence
supports".

## 3. Confidence is not importance, and neither is verification

Three independent axes exist, and none is derived from another:

* `verification` — what the evidence supports (`NM04_VERIFICATION_RULES.md`);
* `confidence` — how complete and specific the evidence is, 0.0–1.0;
* `importance` — the explainable market significance band.

A high confidence score cannot turn a rumour into `officially_confirmed`, and a
`critical` event is not implicitly verified.

## 4. What the model refuses to express

* No price, price range, target, expected return, direction, entry/exit level,
  position size or recommendation.
* No ranking of instruments for purchase; mappings explain involvement, not
  attractiveness.
* `is_price_prediction` and `is_trading_recommendation` are hard `false` on every
  event payload, and the contract states the same guarantee.

## 5. Worked example

An official central-bank rate decision, published minutes ago, market-wide,
with one official source, scores on the official-verification, recency,
subcategory-weight and scope factors and lands in `high`/`critical`. The same
headline from a secondary publisher scores lower: its verification factor is
weaker, and the band cap reflects that even before the reader looks at the
explanation.
