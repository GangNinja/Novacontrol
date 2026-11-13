# NM-08 — Risk Metrics

Every metric returns `MetricResult(value, sufficient, reason, sample_size,
observed_at, assumptions)`. When the minimum data is absent, `value` is `None`,
`sufficient` is false, and `reason` names the missing requirement. No caller may
treat an unavailable metric as zero risk.

## Implemented metrics

| Metric | Definition | Minimum data |
| --- | --- | --- |
| Annualized volatility | Population standard deviation of simple period returns × √252 | 20 returns |
| Maximum drawdown | `max(1 - value / running_peak)` over the close series | 2 closes |
| Current drawdown | `1 - latest / running_peak` | 2 closes |
| Historical VaR (95%) | Loss at the empirical 5th percentile of returns | 20 returns |
| Historical CVaR | Mean of the worst observations at or beyond the VaR quantile | 20 returns |
| Sharpe ratio | `(mean excess return / std) × √252`, risk-free rate 0 | 20 returns |
| Sortino ratio | `(mean excess return / downside deviation) × √252` | 20 returns |
| Beta | `cov(asset, benchmark) / var(benchmark)` on aligned returns | 20 aligned returns |
| Correlation | Pearson correlation of aligned returns | 20 aligned returns |
| Concentration | Herfindahl-Hirschman index of position weights | one weight |
| Capital utilization | Invested value / total portfolio value | a valued portfolio |
| Turnover | Traded notional / average portfolio value | a proposal + valuation |
| Liquidity | Median daily traded value (`close × volume`) | 5 bars |

Beta and correlation are computed only when a benchmark return series is supplied; otherwise they are reported as insufficient rather than assumed. Annualization uses 252 trading days. All calculations use `Decimal` internally and
quantize reported values to six places. Historical metrics describe observed data;
they are not forecasts or guarantees.

## Data sources

- NM-02 supplies price observations and bars; only usable reconciliations are used.
- NM-07 supplies valued holdings, weights, sectors and the state version.
- NM-06 supplies forecast uncertainty, model state and forecast age.
- NM-04 supplies verified and unverified instrument events.

## Corrections and gaps

- Stale market data is a hard policy breach (rejection), not a metric.
- A missing benchmark leaves beta/correlation insufficient rather than assuming 1.
- Bid-ask spread is not used: the current data contract does not provide a reliable
  spread, so liquidity is based on traded value and volume.
- Scenario impacts are separate hypothetical estimates and are never mixed into
  historical metrics.
