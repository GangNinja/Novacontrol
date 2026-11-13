# NM-08 — Risk Policies

Policies are versioned documents per horizon plus one aggregate policy. Defaults
are **simulation defaults**, not proven investment limits.

## Policy set

| Id | Applies to |
| --- | --- |
| `short_term` | Short-term portfolio proposals and monitoring |
| `medium_term` | Medium-term portfolio proposals and monitoring |
| `long_term` | Long-term portfolio proposals and monitoring |
| `aggregate` | Combined exposure across all open portfolios |

## Default limits (v1)

| Limit | Short | Medium | Long | Aggregate |
| --- | --- | --- | --- | --- |
| Max position weight | 25% | 18% | 15% | 20% |
| Max sector weight | 40% | 30% | 35% | 45% |
| Max invested fraction | 90% | 92% | 95% | 92% |
| Min cash reserve | 10% | 8% | 5% | 8% |
| Max drawdown | 12% | 20% | 30% | 15% |
| Max volatility (annualized) | 35% | 28% | 25% | 30% |
| Min daily traded value | 25,000 | 50,000 | 75,000 | 100,000 |
| Max turnover | 50% | 35% | 20% | 30% |
| Max data age | 24h | 72h | 168h | 168h |
| Max forecast age | 12h | 72h | 168h | 168h |
| Max forecast uncertainty | medium | high | high | high |

Restricted instruments and restricted sectors are explicit tuples; empty means no
additional restriction. `max_position_weight`, `max_sector_weight`,
`min_cash_reserve`, `max_drawdown_pct`, `max_volatility_pct`, stale data and
restricted instruments are **hard** constraints. Volatility above the limit and
turnover above the limit are **soft** (restrict). Drawdown above the limit and
aggregate sector concentration require **review**.

## Versioning

`RiskPolicy.version` starts at `nm08-risk-1.0` and is replaced on update by
`nm08-risk-1.0+<sha256-prefix>` over the canonical policy body. Every decision
stores the exact applied version. `update_policy` appends a history row with actor,
timestamp, changed fields and previous version; unknown fields are rejected.

## Evaluation order

1. Data freshness, state freshness and missing inputs.
2. Restricted instruments and sectors.
3. Projected position weight.
4. Projected sector weight.
5. Invested fraction and cash reserve.
6. Drawdown.
7. Volatility.
8. Liquidity.
9. Forecast reliability.
10. Aggregate sector exposure.
11. Turnover.
12. Event risk.

The first hard breach decides `REJECTED`; otherwise insufficient data decides
`INSUFFICIENT_DATA`; a review constraint decides `REQUIRES_REVIEW`; a soft breach
decides `RESTRICTED`; only a clean evaluation is `APPROVED`.
