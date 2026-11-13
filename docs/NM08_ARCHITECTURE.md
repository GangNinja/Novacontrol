# NM-08 — Independent Risk Engine Architecture

NM-08 is a decision layer, not a portfolio manager. It reads the state produced by
NM-01…NM-07, evaluates a proposed action against versioned policies, and returns an
enforceable decision. It never constructs portfolios, moves cash, or places orders.

## Position in the system

```text
NM-07 proposal / strategy request / future paper order
                    |
                    v
        RiskEngine.assess(action, portfolio, proposal, instrument, notional)
                    |
   +----------------+----------------+----------------+
   |                |                |                |
 market data     portfolio state   forecasts       events/radar
 (NM-02)         (NM-07 + NM-03)   (NM-06)         (NM-04)
   |                |                |                |
   +----------------+----------------+----------------+
                    v
          metrics -> policies -> decision
                    |
        APPROVED / REJECTED / RESTRICTED /
        REQUIRES_REVIEW / INSUFFICIENT_DATA
                    |
          persisted decision + alerts + events
                    |
          RiskEngine.apply_decision (only for approved)
```

## Package layout

| Module | Responsibility |
| --- | --- |
| `risk/models.py` | Enums, metric results, policies, decisions, alerts, overrides, scenarios, deterministic ids. |
| `risk/metrics.py` | Pure metric functions with explicit data gates (`sufficient`, `reason`). |
| `risk/policies.py` | `RiskSnapshot`, ordered constraint evaluation, breach classification. |
| `risk/store.py` | SQLite schema, append-only decision history, alerts, overrides, policy versions, stress runs. |
| `risk/engine.py` | Orchestration: state capture, metric assembly, decisions, exposure, stress, governance, apply gate. |

## Independence guarantees

- Decisions are computed from observed state, never copied from NM-07's own
  constraints or NM-06's predicted return.
- A missing mandatory input becomes `INSUFFICIENT_DATA`; it never becomes approval.
- `apply_decision` re-reads the portfolio state version and re-checks the decision
  outcome before delegating to NM-07. A stale or rejected decision cannot apply.
- NM-07's proposals remain proposals: only an approved NM-08 decision reaches
  `PortfolioEngine.apply`.

## Failure behaviour

Market-data, forecast and radar seams are optional. A failing seam degrades the
affected metrics to "insufficient", which propagates to the decision. The engine
does not catch a failed mandatory check and continue as if it passed.

## Scope boundary

NM-08 does not implement strategies, paper execution, backtesting, adaptive
learning, or real brokerage. It exposes stable contracts for those later phases.
