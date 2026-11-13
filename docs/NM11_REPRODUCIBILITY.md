# NM-11 Reproducibility

A run stores its full configuration JSON, config hash, bar-data hash, the bars
themselves, trades, orders, risk decisions, signals, equity series, metrics,
warnings and data-quality record, plus a SHA-256 `repro_hash`. Run-local trade,
order, signal, decision and backtest identifiers are excluded from the hash, so
two identical simulations produce the same hash despite different run ids.

`BacktestEngine.verify()` loads the persisted configuration and bars and runs
the replay again from scratch. It compares the recomputed hash with the stored
one and returns both hashes plus the verification basis. Any alteration to the
configuration, bars or recorded economics changes the hash. SQLite triggers
refuse updates/deletes on completed runs and on comparison records, so the
stored evidence cannot be silently edited.

The store keeps a separate `backtest_runs` row per attempt (keyed by a run id,
with a per-backtest `(backtest_id, repro_hash)` uniqueness constraint); two
forced replays are distinct runs that must agree on the hash. Comparison and
parameter-sensitivity outputs are persisted with their own immutable
`comparison_id`.

Software version, Python version and platform are recorded with the result.
`random_seed` is configuration metadata; the replay itself is deterministic and
does not sample.

## Tests

- two independent stores produce the same hash and both verify;
- a completed run survives reopening the store;
- completed rows and comparison rows cannot be updated or deleted;
- force produces a new run id with the same hash;
- an in-flight replay observes cancellation.

## Limitations

- Verification proves internal determinism, not that the same provider would
  return the same history today.
- No dependency lockfile hash is recorded in the result.
