# NovaMarket — Stage C Forecast Readiness Report

Date: 2026-11-27. Paper-only. No broker order, live execution or model promotion
was performed by this work; nothing here closes Stage C.

## 1. What "ready" means here

A forecast is **ready for the decision path** only when all four are true:

1. a real model artifact exists whose training history is genuine and at or above
   `ForecastPolicy.min_training_bars` (140 daily bars);
2. the model is registered in the NM-06 lifecycle and one model is `active` for
   that horizon;
3. PI-12 can **select** it for the decision horizon label (`1d/3d/5d/30d/90d`);
4. a stored forecast for that horizon reaches the risk bridge without being
   filtered out by a vocabulary mismatch.

## 2. SC-R08-01 — horizon vocabulary — FIXED

The NM-06 `Horizon` values and PI-12's decision labels are different
vocabularies. Before the fix, a stored `one_week` forecast could never satisfy
`models(horizon="5d")`, so even a trained model was invisible to the decision
path.

One canonical map now lives in `novamarket/decision/forecast.py`
(`HORIZON_CANONICAL` + `canonical_horizon`):

| Decision label | NM-06 horizon |
|---|---|
| `5d` | `one_week` |
| `3d` | `few_sessions` |
| `30d` | `one_month` |
| `90d` | `three_months` |

- `RISK_HORIZON_MAP["5d"]` is `one_week` (it pointed at `few_sessions`).
- The PI-12 model lookup queries the store **unfiltered** and then filters
  canonically; a store-level filter on the wrong vocabulary returned nothing.
- The risk bridge resolves both the interval form (`5d`) and the label form
  (`one_week`), while intraday horizons remain excluded.
- `1d` remains the un-mapped decision label; it is not an NM-06 horizon and is
  never invented.

## 3. NSE — READY and proven end to end

| Item | Value |
|---|---|
| Exchange / instrument | XNSE / `XNSE:RELIANCE` |
| Genuine daily bars | **248** (≥ 140 minimum, unchanged) |
| Training result | `TRAINED`; selected model **`bootstrap`**, state `active` |
| Registered models (horizon `one_week`) | `bootstrap` active, `ewma` validated, `historical_mean` validated, `random_walk` validated, `drift` experimental, `zero_return` experimental |
| Generated forecast | `fc_42d151e421dd9f79a566`, horizon `one_week` |
| `PI-12.models(horizon="5d")` | returns **all 6** models |
| Bridge horizons | `["one_week"]` |
| Stored forecast horizons | `["one_week"]` |

This is the first end-to-end proof in Stage C that a real NSE model, its stored
forecast and the PI-12/risk-bridge consumers agree on one horizon. Evidence:
`tmp/stage_c_remediation_evidence/r08_train_xnse.json`.

Limits that remain honest:

- `validity` on the generated forecast is `null` — no matured outcome window was
  observed in this session, so no forecast accuracy is claimed.
- Six candidate models were registered; five are not `active`. That is the
  lifecycle working, not a failure.
- No calibration or promotion decision is implied by this report.

## 4. BSE — BLOCKED by genuine history (not lowered)

| Item | Value |
|---|---|
| Exchange / instrument | XBOM / `XBOM:RELIANCE` |
| Genuine daily bars | **108 rows across 101 distinct sessions** (100 official bhavcopy rows + 8 Yahoo rows; 6 sessions carry both), 2026-07-01 … 2026-11-26 |
| Minimum | 140 (`min_training_bars`, unchanged) |
| Deficit | 32 sessions |
| Training attempted | **No** — refused before training |

No BSE model exists, so no BSE forecast or BSE forecast-driven decision can be
produced. The 140-bar minimum was **not** lowered, no synthetic bar was created
and no NSE price was substituted for a BSE price. The two stores stay isolated.

Consequence for C2: the BSE **decision path** can run on 108 daily rows across
101 sessions (the SMA strategy needs 14 closes), but its **forecast** leg cannot
be verified until enough BSE history exists.

## 5. Where the forecast sits in the paper decision flow

```
daily bars (experience store, deduped one-close-per-session)
        │
        ├── strategy (SMA 14)  ──► signal
        └── forecast context (5m short_term / 1d otherwise)
                                   │
                          NM-06 model registry
                                   │  canonical horizon
                          PI-12 models(horizon="5d")
                                   │
                          forecast_id ──► risk bridge ──► decision
                                                            │
                                            paper proposal / order (NM-08/NM-10)
```

Nothing in this chain places a broker order. NM-08 risk limits and NM-10 paper
execution are unchanged by this work.

## 6. Readiness verdict

| Exchange | Model | Selected | Stored forecast | PI-12 selectable | Ready for a decision run |
|---|---|---|---|---|---|
| NSE | yes | yes | yes | yes | **YES** (subject to R-09 interpretation) |
| BSE | no (108 < 140) | — | — | — | **NO — blocked by history** |

Stage C is **not** closed by this readiness. What is ready is the NSE forecast
plumbing plus one genuine stored forecast; no forecast outcome quality, no BSE
forecast and no live-session behaviour is claimed.

## 7. What would unblock BSE

Any one of these, each an operator decision:

1. more genuine official BSE sessions (32 more weekdays) — the only path that
   keeps the 140-bar minimum;
2. an operator-approved reduction of `min_training_bars` — explicitly **not**
   done here and not recommended without a separate validation plan;
3. accepting C2 without a BSE forecast leg — an acceptance-scope decision, not
   an engineering change.
