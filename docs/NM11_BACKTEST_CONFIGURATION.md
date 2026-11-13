# NM-11 Backtest Configuration

`BacktestConfig` is immutable (`frozen=True`) and has a deterministic SHA-256
`config_hash`. It records:

- name, strategy catalog id, strategy version and full parameter snapshot;
- instruments, start/end, evaluation frequency, horizon and dataset
  id/version;
- initial capital, order notional, benchmark and random seed;
- risk-policy version and the NM-08 policy snapshot used for the replay;
- NM-10 `ExecutionConfig` (fees, slippage, participation and freshness);
- maximum position weight, gross exposure, order type, limit/stop offsets and
  `order_ttl_bars`.

Validation rejects an empty name, an unknown strategy catalog evaluator, blank
strategy version, empty/blank or duplicate instruments, start not before end,
unsupported frequencies (only `1d`), non-positive capital or notional, invalid
weights, a non-positive TTL and unsupported order types. Strategy parameters
are validated by the NM-09 catalog validator before a run exists.

Order types are normalised to `market`, `limit`, `stop` or `stop_limit`. Limit
and stop prices are derived from the signal close by `limit_offset_bps` and
`stop_offset_bps`; the exact values are recorded on each simulated order.

The configuration is written with the run. A changed configuration hashes
differently, and a completed row cannot be updated or deleted (SQLite
triggers). An identical configuration returns the existing record instead of
creating a duplicate row; `force` creates a distinct run with the same
reproducibility hash.
