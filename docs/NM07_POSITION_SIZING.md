# NM-07 — Position Sizing

`allocate(selected, policy)` turns a ranked candidate list into target weights.
It does not place trades; it returns an `AllocationPlan` with weights, the
investable fraction and the notes that explain every adjustment.

## Algorithm

1. **Usable set** — candidates that passed the gates, capped at `max_positions`.
2. **Strength** — each name gets the horizon's strength: blended score, risk
   budget, or inverse volatility. A non-positive total falls back to equal
   weights rather than dividing by zero.
3. **Investable fraction** — `1 - min_cash_fraction`. The cash reserve is never
   sized as a position.
4. **Per-position cap** — any weight above `max_position_weight` is clipped and
   the excess is redistributed to names with room. Repeated up to eight passes;
   the loop stops when nothing is over the cap or no capacity remains.
5. **Sector cap** — sector totals above `max_sector_weight` are trimmed
   proportionally and redistributed outside the overflowing sector, up to three
   passes.
6. **Dust removal** — weights below `min_position_weight` are dropped and the
   remainder is renormalised to the investable fraction, re-applying the
   position cap and rescaling if the total would exceed the budget.
7. **Quantisation** — weights are rounded to four decimal places
   (`0.0001`). If rounding pushed the total above the investable fraction, the
   largest weight absorbs the difference.
8. **Residual** — whatever is left stays in cash and is reported as a note.

## Invariants

- `sum(weights) <= investable_fraction`.
- Every weight is either zero or at least `min_position_weight`, unless the cap
  itself forces a smaller remainder.
- No weight exceeds `max_position_weight`.
- No sector total exceeds `max_sector_weight` when at least one name outside the
  sector can absorb the excess.
- The cash reserve is preserved by construction, and checked again before an
  apply.

## Worked example

Short horizon, 10,000 NC, two eligible names, 25% cap and 10% cash reserve:

```
investable = 0.90 × 10,000 = 9,000
each name  = 0.45 → capped at 0.25
weights    = {ALPHA: 0.25, BETA: 0.25}
invested   = 5,000
cash       = 5,000 (50%)
```

The remaining 5,000 cannot be invested because only two names passed the gates
and each is capped. The engine reports that as cash rather than inventing
positions or loosening the cap. This is why a long-horizon book can legitimately
hold a large cash balance when evidence is thin.

## Diversification

Concentration is reported from positions and sectors, never guessed. A new
position records the sector and asset class carried by the discovery row or the
market instrument metadata; a held name that is no longer a candidate falls back
to its instrument metadata. When neither exposes a sector, the position records
`unknown` and the concentration report says so. Correlation data is not
available in this phase, so no diversification score is fabricated; the
limitation is stated instead. NM-08 owns correlation and risk-factor analysis.
